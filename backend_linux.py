"""Linux/X11 backend: evdev for exclusive capture, xprop for focus.

Translated output is not part of this interface: every gesture the translator
decides on goes out over gate.py's own WebSocket channel to the extension,
identically on both platforms, so there is nothing platform-specific left to do
here for it — no uinput clone device, no libX11 cursor query/warp.
"""

import glob
import os
import re
import select
import subprocess
import sys
import time

import evdev
from evdev import ecodes

import codes

# The vendored constants are what the translator emits; if evdev ever disagreed the
# daemon would press the wrong button. Checked once, here, rather than trusted.
codes.assert_matches_evdev(ecodes)

NAME = "linux"

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


