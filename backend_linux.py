"""Linux/X11 backend: evdev + uinput for input, libX11 for the cursor, xprop for focus.

This is the original `gate.py` platform code, moved out behind the interface in
`backend.py` and otherwise unchanged. Anything that looked like a behaviour change
while moving it is a bug, not a cleanup.
"""

import ctypes
import glob
import os
import re
import select
import subprocess
import sys
import time

import evdev
from evdev import UInput, ecodes

import codes

# The vendored constants are what the translator emits; if evdev ever disagreed the
# daemon would press the wrong button. Checked once, here, rather than trusted.
codes.assert_matches_evdev(ecodes)

NAME = "linux"

VIRTUAL_NAME = "Onshape-gated Mouse"
MODIFIER_NAME = "Onshape-gated Modifier"

# python-evdev stamps every uinput device with the same vendor/product/phys, so
# libinput lumps them into one LIBINPUT_DEVICE_GROUP and X exposes only the first —
# the keyboard half then silently never arrives. Distinct ids keep them separate.
VIRTUAL_VENDOR = 0x6F73
VIRTUAL_PRODUCT_MOUSE = 0x0001
VIRTUAL_PRODUCT_MODIFIER = 0x0002

MOUSE_GLOB = "/dev/input/by-id/*-event-mouse"
BY_ID = "/dev/input/by-id"
CHROME_WM_CLASSES = ("google-chrome", "chromium")
MOTION_THRESHOLD = 30  # accumulated |REL| units, enough to ignore sensor jitter

RESTART_HINT = "systemctl --user restart onshape-mouse-gate.service"
DEVICE_HINT = "device = /dev/input/by-id/...-event-mouse"


def config_dir():
    return os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
        "onshape-trackball",
    )


def log(msg):
    print(f"[gate] {msg}", flush=True)


def declare_dpi_aware():
    """X11 hands out one coordinate space already, so there is nothing to declare."""
    return "x11 (single coordinate space)"


# ------------------------------------------------------------------ enumeration


def _names_by_node():
    """event node -> human name, straight from procfs (no permissions needed)."""
    names, name = {}, None
    try:
        with open("/proc/bus/input/devices") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("N: Name="):
                    name = line.split("=", 1)[1].strip('"')
                elif line.startswith("H: Handlers="):
                    for handler in line.split("=", 1)[1].split():
                        if handler.startswith("event"):
                            names[handler] = name or handler
                elif not line:
                    name = None
    except OSError:
        pass
    return names


def enumerate_mice():
    """Stable by-id paths for every pointing device, with display names."""
    names = _names_by_node()
    found = []
    for path in sorted(glob.glob(os.path.join(BY_ID, "*-event-mouse"))):
        node = os.path.basename(os.path.realpath(path))
        found.append((path, names.get(node, "unknown device")))
    return found


def detect_mouse(timeout):
    """Path of the mouse that gets moved, or None if none does in time."""
    opened = []
    for path, name in enumerate_mice():
        try:
            opened.append((evdev.InputDevice(path), path, name))
        except PermissionError:
            print(f"cannot read {path} (not in the 'input' group?)", file=sys.stderr)
        except OSError as exc:
            print(f"cannot open {path}: {exc}", file=sys.stderr)

    if not opened:
        return None

    fdmap = {dev.fd: (dev, path, name) for dev, path, name in opened}

    # Drain anything already queued so a stale event cannot decide this for us.
    for dev, _p, _n in opened:
        try:
            while dev.read_one() is not None:
                pass
        except OSError:
            pass

    travelled = {}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select(list(fdmap), [], [], 0.2)
        for fd in ready:
            dev, path, _name = fdmap[fd]
            try:
                for event in dev.read():
                    if event.type == ecodes.EV_REL and event.code in (
                        ecodes.REL_X, ecodes.REL_Y
                    ):
                        travelled[path] = travelled.get(path, 0) + abs(event.value)
                        if travelled[path] >= MOTION_THRESHOLD:
                            return path
            except OSError:
                continue
    return None


# ------------------------------------------------------------------ capture


class GatedDevice:
    """The grabbed mouse. evdev events already carry the vendored code values."""

    def __init__(self, path):
        self._dev = _wait_for_device(path)
        self._dev.grab()
        self.name = self._dev.name
        self.template = self._dev

    def events(self):
        return self._dev.read_loop()

    def close(self):
        for close in (self._dev.ungrab, self._dev.close):
            try:
                close()
            except Exception:
                pass


def _wait_for_device(path):
    warned = False
    while not os.path.exists(path):
        if not warned:
            log(f"waiting for {path} to appear (mouse unplugged?)")
            warned = True
        time.sleep(1)
    return evdev.InputDevice(path)


def open_gated_device(identifier):
    return GatedDevice(identifier)


# ------------------------------------------------------------------ output


class VirtualOutput:
    """A uinput clone of the grabbed mouse. Same surface the stub in the tests has."""

    def __init__(self, template):
        self._ui = UInput.from_device(
            template, name=VIRTUAL_NAME, vendor=VIRTUAL_VENDOR,
            product=VIRTUAL_PRODUCT_MOUSE, phys="onshape-gate/mouse")

    def write(self, etype, code, value):
        self._ui.write(etype, code, value)

    def write_event(self, event):
        self._ui.write_event(event)

    def syn(self):
        self._ui.syn()

    def close(self):
        self._ui.close()


class _KeyOutput:
    def __init__(self, ui):
        self._ui = ui

    def write(self, etype, code, value):
        self._ui.write(etype, code, value)

    def write_event(self, event):
        self._ui.write_event(event)

    def syn(self):
        self._ui.syn()

    def close(self):
        self._ui.close()


def modifier_output():
    """A separate keyboard-only virtual device for Ctrl.

    Kept apart from the mouse clone so X classifies each cleanly rather than getting
    one hybrid device.
    """
    # Advertise a normal keyboard key block, not just KEY_LEFTCTRL. libinput refuses
    # to classify a single-key device as a keyboard, so X silently never adds it and
    # the modifier goes nowhere. Only KEY_LEFTCTRL is ever emitted.
    keys = list(range(ecodes.KEY_ESC, ecodes.KEY_F12 + 1))
    assert ecodes.KEY_LEFTCTRL in keys
    try:
        return _KeyOutput(UInput({ecodes.EV_KEY: keys}, name=MODIFIER_NAME,
                                 vendor=VIRTUAL_VENDOR,
                                 product=VIRTUAL_PRODUCT_MODIFIER,
                                 phys="onshape-gate/modifier"))
    except OSError as exc:
        log(f"cannot create the Ctrl device ({exc})")
        return None


# ------------------------------------------------------------------ pointer


class Pointer:
    """XQueryPointer / XWarpPointer via ctypes, so recentring needs no python-xlib.

    Only the device read loop touches this, so the unsynchronised Xlib connection is
    safe: no XInitThreads needed.
    """

    def __init__(self):
        self.ok = False
        self._dpy = None
        try:
            self._x = ctypes.CDLL("libX11.so.6")
        except OSError as exc:
            log(f"libX11 unavailable ({exc}); pan recentring disabled")
            return

        x = self._x
        x.XOpenDisplay.restype = ctypes.c_void_p
        x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x.XDefaultRootWindow.restype = ctypes.c_ulong
        x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x.XQueryPointer.restype = ctypes.c_int
        x.XQueryPointer.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_uint),
        ]
        x.XWarpPointer.restype = ctypes.c_int
        x.XWarpPointer.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
            ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint,
            ctypes.c_int, ctypes.c_int,
        ]
        x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]

        display = os.environ.get("DISPLAY")
        self._dpy = x.XOpenDisplay(display.encode() if display else None)
        if not self._dpy:
            log(f"cannot open X display {display!r}; pan recentring disabled")
            return
        self._root = x.XDefaultRootWindow(self._dpy)
        self.ok = True

    def position(self):
        if not self.ok:
            return None
        root_ret = ctypes.c_ulong(); child_ret = ctypes.c_ulong()
        rx = ctypes.c_int(); ry = ctypes.c_int()
        wx = ctypes.c_int(); wy = ctypes.c_int()
        mask = ctypes.c_uint()
        got = self._x.XQueryPointer(
            self._dpy, self._root,
            ctypes.byref(root_ret), ctypes.byref(child_ret),
            ctypes.byref(rx), ctypes.byref(ry),
            ctypes.byref(wx), ctypes.byref(wy), ctypes.byref(mask),
        )
        return (rx.value, ry.value) if got else None

    def warp(self, x_pos, y_pos):
        if not self.ok:
            return
        self._x.XWarpPointer(self._dpy, 0, self._root, 0, 0, 0, 0,
                             int(x_pos), int(y_pos))
        self._x.XSync(self._dpy, 0)


# ------------------------------------------------------------------ focus

WIN_ID = re.compile(r"(0x[0-9a-fA-F]+|\d+)\s*$")


def _window_is_chrome(win_id):
    try:
        out = subprocess.run(
            ["xprop", "-id", win_id, "WM_CLASS"],
            capture_output=True, text=True, timeout=2,
        ).stdout.lower()
    except Exception:
        return False
    return any(c in out for c in CHROME_WM_CLASSES)


def _parse_active(line):
    m = WIN_ID.search(line.strip())
    return m.group(1) if m else None


def window_geometry(win_id):
    """Absolute rect of a window, via xwininfo. Only run on a focus change."""
    try:
        out = subprocess.run(
            ["xwininfo", "-id", win_id],
            capture_output=True, text=True, timeout=2,
        ).stdout
    except Exception:
        return None

    fields = {}
    for line in out.splitlines():
        line = line.strip()
        for key, label in (
            ("x", "Absolute upper-left X:"), ("y", "Absolute upper-left Y:"),
            ("w", "Width:"), ("h", "Height:"),
        ):
            if line.startswith(label):
                try:
                    fields[key] = int(line[len(label):].strip())
                except ValueError:
                    return None
    if len(fields) != 4:
        return None
    return (fields["x"], fields["y"], fields["w"], fields["h"])


def _focus_from(line):
    """-> (chrome_is_focused, geometry_or_None, window_id_or_None)"""
    wid = _parse_active(line)
    if not wid or wid == "0x0" or not _window_is_chrome(wid):
        return False, None, None
    return True, window_geometry(wid), wid


def watch_focus(callback):
    """Follow _NET_ACTIVE_WINDOW. `xprop -spy` streams a line per change, so this is
    event-driven rather than polled."""
    while True:
        try:
            first = subprocess.run(
                ["xprop", "-root", "_NET_ACTIVE_WINDOW"],
                capture_output=True, text=True, timeout=2,
            ).stdout
            callback(*_focus_from(first))

            proc = subprocess.Popen(
                ["xprop", "-root", "-spy", "_NET_ACTIVE_WINDOW"],
                stdout=subprocess.PIPE, text=True,
            )
            for line in proc.stdout:
                callback(*_focus_from(line))
        except Exception as exc:
            log(f"focus watcher restarting after error: {exc}")
        callback(False, None, None)
        time.sleep(2)


# ------------------------------------------------------------------ other mice


def _other_mice(gated_path):
    """Every pointing device except the one we grabbed (and our own virtual clone)."""
    gated = os.path.realpath(gated_path)
    return [p for p in sorted(glob.glob(MOUSE_GLOB))
            if os.path.realpath(p) != gated]


def watch_other_pointers(gated_path, on_activity, enabled):
    """Read-only watch on the other mice; any activity ends the current pan stroke.

    Opened without EVIOCGRAB, so those mice keep working completely normally.
    """
    opened = {}
    while True:
        if not enabled():
            time.sleep(5)
            continue

        for path in _other_mice(gated_path):
            if path in opened:
                continue
            try:
                dev = evdev.InputDevice(path)
            except OSError:
                continue
            if dev.name == VIRTUAL_NAME:      # never react to our own output
                dev.close()
                continue
            opened[path] = dev
            log(f"watching '{dev.name}' so it can interrupt a pan stroke")

        if not opened:
            time.sleep(2)
            continue

        fdmap = {dev.fd: (path, dev) for path, dev in opened.items()}
        try:
            ready, _, _ = select.select(list(fdmap), [], [], 2.0)
        except OSError:
            ready = []

        for fd in ready:
            path, dev = fdmap[fd]
            try:
                for event in dev.read():
                    if event.type in (ecodes.EV_REL, ecodes.EV_KEY):
                        on_activity()
                        break
            except OSError:
                # Unplugged: drop it and let the next scan pick it back up.
                try:
                    dev.close()
                except Exception:
                    pass
                opened.pop(path, None)
