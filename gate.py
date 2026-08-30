#!/usr/bin/env python3
"""Turn the left-hand mouse into an Onshape navigation device.

The physical device is grabbed exclusively (EVIOCGRAB) so X11 never sees it, and its
events are translated onto a virtual uinput clone only while onshape.com is frontmost.

Translation, while the gate is open:
    motion                  -> pan    (synthesised as a held Ctrl + right-drag)
    right button + motion   -> rotate (Ctrl is dropped; the same drag carries on)
    wheel                   -> zoom   (passed through unchanged)
    left button             -> clear the selection (taps space; the click is swallowed)

The gate opens when both of these agree:
  * X11 says the focused window belongs to Google Chrome  (tracked via `xprop -root -spy`)
  * the Chrome extension says the focused window's active tab is on onshape.com

Either signal alone is insufficient: X11 cannot see a tab's URL, and the extension's
MV3 service worker can be suspended while Chrome sits in the background.
"""

import ctypes
import glob
import json
import os
import sys
import re
import select
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import evdev
from evdev import UInput, ecodes

# Settings live in a "key = value" file written by setup.sh. Everything here is
# resolved lazily in main() so the module stays importable (test_translator.py execs
# this file) on a machine with no config yet.
_CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "onshape-trackball",
)
CONFIG_PATH = os.path.join(_CONFIG_DIR, "config")

# Superseded by CONFIG_PATH; still read so an older install keeps working.
LEGACY_DEVICE_PATH = os.path.join(_CONFIG_DIR, "device")

VIRTUAL_NAME = "Onshape-gated Mouse"
MODIFIER_NAME = "Onshape-gated Modifier"

# python-evdev stamps every uinput device with the same vendor/product/phys, so
# libinput lumps them into one LIBINPUT_DEVICE_GROUP and X exposes only the first —
# the keyboard half then silently never arrives. Distinct ids keep them separate.
VIRTUAL_VENDOR = 0x6f73
VIRTUAL_PRODUCT_MOUSE = 0x0001
VIRTUAL_PRODUCT_MODIFIER = 0x0002
PORT = 47653

# Panning is Ctrl + right-drag; plain right-drag rotates. Ctrl is held on a second
# virtual device and the right button on the mouse one.
#
# Onshape's other pan gesture, middle-drag, is deliberately not an option: repeated
# presses pair into a double middle-click, which Onshape reads as Zoom to Fit.

# Ctrl must land before the button and lift after it. The other order leaves a moment
# of plain right-drag, which Onshape reads as rotate.
# The button release and the Ctrl release travel through two independent uinput
# devices, so nothing but this gap enforces their order — and out of order means a
# moment of plain right-drag, which Onshape rotates on.
#
# Sized by measurement, not guesswork. At 2ms: 26 rotate episodes in 25s of panning.
# At 8ms: still 18, clustered at 16-20ms, i.e. one per stroke teardown with the gap
# consistently too short. Set well past that spread.
#
# Costs nothing noticeable: it delays only the Ctrl release at the end of a stroke,
# never the button press or any motion, and a recentre no longer pays it at all.
MODIFIER_SETTLE = 0.030

# What the gated mouse's left button does. Onshape clears the whole selection on
# space, which is more useful from a navigation mouse than a left click would be: the
# cursor is penned in the middle of the view, so a real click would just select
# whatever geometry happens to be under it.
#
# The click is swallowed, not passed through, for that same reason. "none" restores
# an ordinary left click.
LEFT_CLICK_KEY = "space"
LEFT_CLICK_CODES = {
    "space": ecodes.KEY_SPACE,
    "esc": ecodes.KEY_ESC,
    "escape": ecodes.KEY_ESC,
    "none": None,
}

WHEEL_AXES = tuple(
    code for code in ("REL_WHEEL", "REL_HWHEEL", "REL_WHEEL_HI_RES", "REL_HWHEEL_HI_RES")
    if hasattr(ecodes, code)
)
WHEEL_CODES = tuple(getattr(ecodes, code) for code in WHEEL_AXES)

LEFT_CLICK_CODE = LEFT_CLICK_CODES[LEFT_CLICK_KEY]

# A pan stroke ends after this long without motion, so the synthetic button is never
# left down. Panning then feels like trackpad strokes: push, pause, push again.
# Overridden by pan_idle_release_ms in the config file; this is the fallback.
DEFAULT_PAN_IDLE_RELEASE_MS = 150
PAN_IDLE_RELEASE = DEFAULT_PAN_IDLE_RELEASE_MS / 1000.0

# Outside this range the feature stops behaving like a pan stroke at all: too low
# and a stroke ends between mouse reports, too high and the button hangs around
# long after you stop.
PAN_IDLE_MIN_MS = 20
PAN_IDLE_MAX_MS = 2000

# How often the idle check runs, so a release lands within PAN_TICK of the deadline.
PAN_TICK = 0.05

# Panning drags the real cursor, so a long sweep runs out of screen and the pan dies.
# Recentring warps the pointer back to the middle of the window when it nears an edge,
# which makes panning effectively unlimited.
RECENTER = True
RECENTER_MARGIN = 20

# The usable view rect comes from the extension's content script, which probes the
# page with elementFromPoint to find the region that genuinely belongs to the 3D view
# — the canvas minus whatever Onshape stacks on top of it. Goes stale if the script
# stops reporting (tab closed, extension reloaded), at which point we fall back to the
# whole Chrome window.
CANVAS_STALE_AFTER = 5.0

# The warp must not land while the pan button is down: Onshape would read the jump as
# one enormous pan. So the button is lifted, X is given a moment to settle, then it is
# pressed again. Only paid at an edge, not per motion event.
RECENTER_SETTLE = 0.012

# XQueryPointer is a server round-trip; a 1000Hz mouse would make that per-event cost
# real, so the edge check is throttled.
RECENTER_CHECK_INTERVAL = 0.03

# Both mice drive one shared X11 pointer, so a held pan gesture applies to whatever the
# *other* mouse does: its motion pans, and its wheel reaches the page with the button
# still down rather than as a clean scroll. Watching the other mice read-only and
# dropping the gesture the moment one of them stirs keeps them independent.
PAN_YIELD = True

# How far the mouse must travel before a pan actually starts. Measured as net
# displacement, not distance travelled, so jitter that wanders out and back never
# trips it — only a deliberate push in some direction does.
#
# Applies to panning alone. Rotating, zooming and the left button are unaffected: the
# cursor keeps tracking your hand throughout, it simply is not panning yet.
PAN_DEADZONE = 10

# A press and release with nothing in between is a click, and Ctrl + right-click
# opens Chrome's context menu. Every release we emit is preceded by at least this
# much displacement, so the gesture always reads as a drag.
MIN_DRAG_PX = 8

# How often the cached window rect is re-read, to survive a move or resize that
# happens without any focus change.
GEOMETRY_REFRESH = 2.0

# Yielding to another mouse releases the pan, but the next twitch of the gated mouse
# would re-press it immediately — so a burst of wheel-zoom turns into a press/release
# ping-pong. Staying released briefly after a yield lets a zoom finish in peace.
YIELD_COOLDOWN = 0.15

MOUSE_GLOB = "/dev/input/by-id/*-event-mouse"

# The extension pushes on every real transition and heartbeats every 30s via
# chrome.alarms. If we go this long with nothing at all, assume it died and fail closed.
STALE_AFTER = 120.0

CHROME_WM_CLASSES = ("google-chrome", "chromium")
MOTION_AXES = (ecodes.REL_X, ecodes.REL_Y)


def log(msg):
    print(f"[onshape-mouse] {msg}", flush=True)


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


POINTER = Pointer()


class Translator:
    """Owns the virtual device and the pan state machine.

    Every write to the virtual device goes through this lock, because the idle-release
    timer and the gate can both end a pan stroke concurrently with the read loop.
    """

    def __init__(self, ui, modifier=None):
        self._ui = ui
        self._modifier = modifier
        self._ctrl_down = False
        self._right_emitted = False
        self._lock = threading.Lock()
        self._held = set()          # real buttons we have forwarded as pressed
        self._panning = False       # is the pan gesture currently held?
        self._last_motion = 0.0
        self._right_down = False
        self._last_edge_check = 0.0
        self.recenters = 0
        self.yields = 0
        self._last_press_pos = None
        self._last_press_time = 0.0
        self._travel_x = 0.0
        self._travel_y = 0.0
        self.drag_nudges = 0
        self._yield_until = 0.0
        self.presses_recentred = 0
        self.left_taps = 0

    # --- callers must hold self._lock -------------------------------------------

    def _ensure_inside_window(self, position):
        """A pan press must land inside the Chrome window. Pressing outside it clicks
        whatever else is there, which takes focus away and closes the gate.

        Motion keeps flowing while a stroke is between presses — including through a
        deferred press, when _panning is false and edge recentring is therefore not
        running — so by press time the cursor can genuinely be outside the window.
        """
        geometry = GATE.view_rect()
        if geometry is None or position is None or not POINTER.ok:
            return position

        win_x, win_y, win_w, win_h = geometry
        edge = 8
        if (win_x + edge <= position[0] <= win_x + win_w - edge
                and win_y + edge <= position[1] <= win_y + win_h - edge):
            return position

        centre = (win_x + win_w // 2, win_y + win_h // 2)
        POINTER.warp(*centre)
        self.presses_recentred += 1
        return centre

    def _press_pan(self):
        now = time.monotonic()
        position = POINTER.position() if POINTER.ok else None
        position = self._ensure_inside_window(position)

        self._emit_pan_down()
        self._last_press_pos = position
        self._last_press_time = now

    def _within_deadzone(self):
        return (self._travel_x * self._travel_x + self._travel_y * self._travel_y
                < PAN_DEADZONE * PAN_DEADZONE)

    def _reset_deadzone(self):
        self._travel_x = 0.0
        self._travel_y = 0.0

    def _start_pan(self):
        if self._panning:
            return

        now = time.monotonic()
        if now < self._yield_until:
            return          # another mouse is mid-gesture; stay out of its way

        if PAN_DEADZONE > 0 and self._within_deadzone():
            return          # not a deliberate push yet

        self._press_pan()
        self._panning = True
        self._reset_deadzone()

    def _handle_right_button(self, event):
        """The pan already holds the right button, so a physical press must not press
        it twice. Dropping Ctrl instead turns the very same drag
        into Onshape's native rotate. Caller holds self._lock."""
        pressed = event.value != 0

        if pressed:
            self._right_down = True
            if self._panning:
                if self._ctrl_down:
                    time.sleep(MODIFIER_SETTLE)
                    self._ctrl(0)
                self._panning = False
                return              # button is already down; swallow the duplicate
            if not self._right_emitted:
                self._ui.write(ecodes.EV_KEY, ecodes.BTN_RIGHT, 1)
                self._right_emitted = True
                self._ui.syn()
            return

        self._right_down = False
        self._end_pan(syn=False)
        if self._right_emitted:
            self._release_right_button()
            self._ui.syn()

    def _tap_key(self, code):
        """Tap a key on the modifier device. Caller holds self._lock."""
        if self._modifier is None:
            return
        self._modifier.write(ecodes.EV_KEY, code, 1)
        self._modifier.syn()
        self._modifier.write(ecodes.EV_KEY, code, 0)
        self._modifier.syn()
        self.left_taps += 1

    def _ctrl(self, value):
        if self._modifier is None:
            return
        self._modifier.write(ecodes.EV_KEY, ecodes.KEY_LEFTCTRL, value)
        self._modifier.syn()
        self._ctrl_down = bool(value)

    def _emit_pan_down(self):
        if not self._ctrl_down:
            self._ctrl(1)
            time.sleep(MODIFIER_SETTLE)
        if not self._right_emitted:
            self._ui.write(ecodes.EV_KEY, ecodes.BTN_RIGHT, 1)
            self._ui.syn()
            self._right_emitted = True

    def _nudge_for_drag(self):
        """Guarantee a little displacement before releasing a button.

        A press and release with nothing between them is a click, and Ctrl +
        right-click opens Chrome's context menu mid-pan. Moving first makes every
        gesture read as a drag instead. The move does pan the model by MIN_DRAG_PX,
        which is why it is kept small.
        """
        position = POINTER.position() if POINTER.ok else None

        if position is not None and self._last_press_pos is not None:
            dx = position[0] - self._last_press_pos[0]
            dy = position[1] - self._last_press_pos[1]
            if dx * dx + dy * dy >= MIN_DRAG_PX * MIN_DRAG_PX:
                return                      # already dragged far enough

        direction = 1
        geometry = GATE.view_rect()
        if position is not None and geometry is not None:
            win_x, _, win_w, _ = geometry
            if position[0] + MIN_DRAG_PX > win_x + win_w - 8:
                direction = -1

        self._ui.write(ecodes.EV_REL, ecodes.REL_X, direction * MIN_DRAG_PX)
        self._ui.syn()
        self.drag_nudges += 1

    def _release_right_button(self):
        """Every right-button release goes through here, so none of them can ever
        land as a bare click.

        The syn is essential, not tidiness: the modifier lives on a second uinput
        device with its own stream, and an unflushed button release would reach X
        *after* the Ctrl release that follows it — leaving a moment of plain
        right-drag, which Onshape rotates on.
        """
        if not self._right_emitted:
            return
        self._nudge_for_drag()
        self._ui.write(ecodes.EV_KEY, ecodes.BTN_RIGHT, 0)
        self._ui.syn()
        self._right_emitted = False

    def _emit_pan_up(self, keep_modifier=False):
        """keep_modifier is for the recentre, which lifts and re-presses the button
        mid-stroke. Dropping Ctrl there too was pure cost: it doubled the number of
        modifier transitions and opened a window where X could see the button still
        down with Ctrl already gone, which is a plain right-drag and rotates.
        """
        # Button first: dropping Ctrl while the button is still down would leave a
        # plain right-drag, which Onshape rotates on.
        #
        # Released unconditionally. This runs only while a pan owns the button
        # (_end_pan and the recentre both require _panning), and the hand-off to
        # rotate never comes through here — _handle_right_button drops Ctrl and clears
        # _panning itself. Skipping the release when _right_down looked true therefore
        # protected nothing, and if that flag ever went stale it left the button held
        # with Ctrl gone: a rotate lasting until something else happened to clear it.
        self._release_right_button()
        if self._ctrl_down and not keep_modifier:
            time.sleep(MODIFIER_SETTLE)
            self._ctrl(0)

    def _end_pan(self, syn):
        """Release the pan gesture. syn=False when the source's own SYN_REPORT is
        about to flush the same packet."""
        if not self._panning:
            return
        self._emit_pan_up()
        self._panning = False
        self._reset_deadzone()      # the next stroke earns its own dead zone
        if syn:
            self._ui.syn()

    def _track(self, code, value):
        if value == 1:
            self._held.add(code)
        elif value == 0:
            self._held.discard(code)

    # --- public ------------------------------------------------------------------

    def handle(self, event):
        with self._lock:
            etype, code, value = event.type, event.code, event.value

            if etype == ecodes.EV_KEY:
                if code == ecodes.BTN_RIGHT:
                    return self._handle_right_button(event)
                if code == ecodes.BTN_LEFT and LEFT_CLICK_CODE is not None:
                    if value:
                        # End the pan first: the tap must not land while Ctrl is still
                        # held, or it is Ctrl+space rather than space.
                        self._end_pan(syn=True)
                        self._tap_key(LEFT_CLICK_CODE)
                    return                  # swallow press and release alike
                if value:
                    # Any other button starts a real click; don't leave a pan running
                    # underneath it.
                    self._end_pan(syn=False)
                self._track(code, value)
                self._ui.write_event(event)
                return

            if etype == ecodes.EV_REL and code in WHEEL_CODES:
                # Ctrl + wheel is browser page zoom, so the modifier must be gone
                # before a wheel event reaches Chrome.
                if self._panning:
                    self._end_pan(syn=True)
                self._ui.write_event(event)
                return

            if etype == ecodes.EV_REL and code in MOTION_AXES:
                if not self._right_down:
                    if code == ecodes.REL_X:
                        self._travel_x += value
                    else:
                        self._travel_y += value
                    self._start_pan()
                    self._last_motion = time.monotonic()
                    self._ui.write_event(event)
                    self._recenter_if_near_edge()
                    return
                self._ui.write_event(event)
                return

            # Wheel, hi-res wheel, MSC_SCAN, SYN_REPORT: verbatim.
            self._ui.write_event(event)

    def _recenter_if_near_edge(self):
        """Keep the cursor inside the window so a sweep cannot run out of screen.

        The pan button is lifted around the warp: a jump with it held would be read
        as one enormous pan. Caller holds self._lock.
        """
        if not RECENTER or not POINTER.ok:
            return

        now = time.monotonic()
        if now - self._last_edge_check < RECENTER_CHECK_INTERVAL:
            return
        self._last_edge_check = now

        geometry = GATE.view_rect()
        if geometry is None:
            return
        win_x, win_y, win_w, win_h = geometry
        if win_w <= 0 or win_h <= 0:
            return

        # A small window must not end up with a safe region of zero.
        margin = min(RECENTER_MARGIN, win_w // 3, win_h // 3)

        position = POINTER.position()
        if position is None:
            return
        pointer_x, pointer_y = position

        inside = (win_x + margin <= pointer_x <= win_x + win_w - margin
                  and win_y + margin <= pointer_y <= win_y + win_h - margin)
        if inside:
            return

        centre_x = win_x + win_w // 2
        centre_y = win_y + win_h // 2

        if not self._panning:
            # No button is held, so there is nothing to protect and no press cycle to
            # pay for — just put the cursor back. This case matters more than it
            # sounds: motion keeps flowing between strokes and through the yield
            # cooldown, and without recentring here the cursor wanders clean out of
            # the view and over the feature tree.
            POINTER.warp(centre_x, centre_y)
            self.recenters += 1
            self._last_motion = time.monotonic()
            return

        self._emit_pan_up(keep_modifier=True)
        self._ui.syn()
        time.sleep(RECENTER_SETTLE)

        POINTER.warp(centre_x, centre_y)
        time.sleep(RECENTER_SETTLE)

        self._press_pan()
        self._ui.syn()
        self.recenters += 1

        # A recentre only happens mid-stroke, and it stalls this loop for two
        # RECENTER_SETTLE naps. Without refreshing the idle deadline, the timer can
        # fire straight afterwards and tear down the stroke we just restored — which
        # gets much more likely as PAN_IDLE_RELEASE approaches the stall duration.
        self._last_motion = time.monotonic()

    def yield_stroke(self):
        """Another pointing device stirred. Drop everything the gated mouse is
        holding, so the shared X11 pointer is not carrying it into someone else's
        gesture.

        Deliberately not conditioned on _panning. After the hand-off to rotate that
        flag is already False while the right button stays held, so keying off it
        meant the right mouse could cancel a pan but never a rotate — the button then
        stayed down until the physical button happened to come back up.
        """
        with self._lock:
            if not (self._panning or self._right_emitted or self._ctrl_down):
                return

            self._end_pan(syn=True)          # no-op when not panning
            self._release_right_button()     # this is what catches the rotate
            if self._ctrl_down:
                time.sleep(MODIFIER_SETTLE)  # button before Ctrl, as everywhere else
                self._ctrl(0)
            self._ui.syn()

            self._reset_deadzone()
            self.yields += 1
            self._yield_until = time.monotonic() + YIELD_COOLDOWN

    def note_while_closed(self, event):
        """Keep physical button state honest even while we're dropping events, so the
        gate reopening mid-hold doesn't leave us confused about the right button."""
        if event.type == ecodes.EV_KEY and event.code == ecodes.BTN_RIGHT:
            with self._lock:
                self._right_down = event.value != 0

    def tick(self):
        with self._lock:
            now = time.monotonic()

            if self._panning and now - self._last_motion > PAN_IDLE_RELEASE:
                self._end_pan(syn=True)
            elif not self._panning and now - self._last_motion > PAN_IDLE_RELEASE:
                # Let a stale nudge expire, so movement from a while ago cannot
                # combine with a fresh one to cross the dead zone.
                self._reset_deadzone()

    def release_all(self):
        """Gate closed. Lift anything we left down, real or synthetic."""
        with self._lock:
            # Cleared before the early return, not after it. Leaving it set here is
            # how it went stale: close the gate while the physical right button is
            # down and the flag survived for the rest of the session.
            self._right_down = False
            if not self._panning and not self._held and not self._right_emitted \
                    and not self._ctrl_down:
                return
            self._end_pan(syn=False)
            self._release_right_button()
            if self._ctrl_down:
                self._ctrl(0)
            for code in self._held:
                self._ui.write(ecodes.EV_KEY, code, 0)
            self._held.clear()
            self._right_down = False
            self._reset_deadzone()
            self._ui.syn()
            log("gate closed mid-gesture; released held buttons")

    def snapshot(self):
        with self._lock:
            return {
                "panning": self._panning,
                "right_button_down": self._right_down,
                "recenters": self.recenters,
                "pan_yields": self.yields,
                "presses_recentred": self.presses_recentred,
                "left_taps": self.left_taps,
                "ctrl_held": self._ctrl_down,
                "drag_nudges": self.drag_nudges,
            }


class Gate:
    def __init__(self):
        self._lock = threading.Lock()
        self._chrome_focused = False
        self._onshape = False
        self._last_push = 0.0
        self._open = False
        self._geometry = None
        self._window_id = None
        self._canvas = None
        self._canvas_at = 0.0
        self._canvas_diag = None
        self._canvas_diag_at = 0.0
        self.translator = None

    def _compute_locked(self):
        if not self._chrome_focused:
            return False
        if self._last_push == 0.0:
            return False
        if time.monotonic() - self._last_push > STALE_AFTER:
            return False
        return self._onshape

    def _recompute(self):
        with self._lock:
            new = self._compute_locked()
            changed = new != self._open
            self._open = new
            translator = self.translator
        if changed:
            log(f"gate {'OPEN' if new else 'closed'}")
            if not new and translator is not None:
                translator.release_all()

    def set_chrome_focused(self, focused, geometry=None, window_id=None):
        with self._lock:
            self._chrome_focused = focused
            self._geometry = geometry if focused else None
            self._window_id = window_id if focused else None
        self._recompute()

    def refresh_geometry(self):
        """The window can be moved or resized with no focus change, which would leave
        the cached rect wrong — and a wrong rect sends presses to the wrong place."""
        with self._lock:
            window_id = self._window_id
            focused = self._chrome_focused
        if not focused or not window_id:
            return
        geometry = window_geometry(window_id)
        if geometry is None:
            return
        with self._lock:
            if self._chrome_focused and self._window_id == window_id:
                self._geometry = geometry

    def geometry(self):
        with self._lock:
            return self._geometry

    def set_canvas(self, canvas, diag=None):
        with self._lock:
            self._canvas = canvas
            self._canvas_at = time.monotonic() if canvas else 0.0
            if diag is not None:
                self._canvas_diag = diag
                self._canvas_diag_at = time.monotonic()

    def view_rect(self):
        """The area the cursor should stay inside: the usable 3D view when the
        extension is reporting it, otherwise the whole Chrome window.

        It is deliberately not the canvas's own rect. Onshape lays controls over the
        canvas and, with the feature list collapsed, the canvas runs underneath the
        slide-out entirely. Those are ordinary DOM elements, so a right-button release
        over one opens a context menu.
        """
        with self._lock:
            if (self._canvas is not None
                    and time.monotonic() - self._canvas_at <= CANVAS_STALE_AFTER):
                return self._canvas
            return self._geometry

    def set_onshape(self, onshape):
        with self._lock:
            self._onshape = onshape
            self._last_push = time.monotonic()
        self._recompute()

    def is_open(self):
        with self._lock:
            return self._compute_locked()

    def snapshot(self):
        with self._lock:
            state = {
                "chrome_focused": self._chrome_focused,
                "onshape_tab": self._onshape,
                "seconds_since_extension_push": (
                    None if self._last_push == 0.0
                    else round(time.monotonic() - self._last_push, 1)
                ),
                "gate_open": self._compute_locked(),
                "window_geometry": self._geometry,
                "canvas_rect": self._canvas,
                "canvas_age": (None if self._canvas_at == 0.0
                               else round(time.monotonic() - self._canvas_at, 1)),
                "canvas_diag": self._canvas_diag,
                "canvas_diag_age": (None if self._canvas_diag_at == 0.0
                                    else round(time.monotonic() - self._canvas_diag_at, 1)),
            }
            translator = self.translator
        state["device"] = DEVICE_PATH
        state["pan_idle_release_ms"] = round(PAN_IDLE_RELEASE * 1000)
        state["pan_recenter"] = RECENTER and POINTER.ok
        state["pan_recenter_margin_px"] = RECENTER_MARGIN
        state["pan_yield_to_other_mice"] = PAN_YIELD
        state["pan_deadzone_px"] = PAN_DEADZONE
        state["left_click_key"] = LEFT_CLICK_KEY
        state["device_attached"] = translator is not None
        if translator is not None:
            state.update(translator.snapshot())
        return state


GATE = Gate()


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        if self.path != "/state":
            self.send_error(404)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            GATE.set_onshape(bool(body.get("onshape")))
            GATE.set_canvas(parse_rect(body.get("canvas")), body.get("diag"))
        except Exception as exc:
            self.send_error(400, str(exc))
            return
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        """Human-readable status, for debugging: curl localhost:47653/status"""
        if self.path != "/status":
            self.send_error(404)
            return
        payload = json.dumps(GATE.snapshot(), indent=2).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self._cors()
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def parse_rect(raw):
    """-> (x, y, w, h) or None. Comes off the network, so validate rather than trust."""
    if not isinstance(raw, dict):
        return None
    try:
        rect = tuple(int(raw[key]) for key in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        return None
    if rect[2] <= 0 or rect[3] <= 0:
        return None
    return rect


def serve():
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


def pan_timer():
    last_geometry_refresh = 0.0
    while True:
        time.sleep(PAN_TICK)
        translator = GATE.translator
        if translator is not None:
            translator.tick()

        now = time.monotonic()
        if now - last_geometry_refresh > GEOMETRY_REFRESH:
            last_geometry_refresh = now
            GATE.refresh_geometry()


WIN_ID = re.compile(r"(0x[0-9a-fA-F]+|\d+)\s*$")


def window_is_chrome(win_id):
    try:
        out = subprocess.run(
            ["xprop", "-id", win_id, "WM_CLASS"],
            capture_output=True, text=True, timeout=2,
        ).stdout.lower()
    except Exception:
        return False
    return any(c in out for c in CHROME_WM_CLASSES)


def parse_active(line):
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


def focus_from(line):
    """-> (chrome_is_focused, geometry_or_None, window_id_or_None)"""
    wid = parse_active(line)
    if not wid or wid == "0x0" or not window_is_chrome(wid):
        return False, None, None
    return True, window_geometry(wid), wid


def watch_focus():
    """Follow _NET_ACTIVE_WINDOW. `xprop -spy` streams a line per change, so this is
    event-driven rather than polled."""
    while True:
        try:
            first = subprocess.run(
                ["xprop", "-root", "_NET_ACTIVE_WINDOW"],
                capture_output=True, text=True, timeout=2,
            ).stdout
            GATE.set_chrome_focused(*focus_from(first))

            proc = subprocess.Popen(
                ["xprop", "-root", "-spy", "_NET_ACTIVE_WINDOW"],
                stdout=subprocess.PIPE, text=True,
            )
            for line in proc.stdout:
                GATE.set_chrome_focused(*focus_from(line))
        except Exception as exc:
            log(f"focus watcher restarting after error: {exc}")
        GATE.set_chrome_focused(False, None, None)
        time.sleep(2)


DEVICE_PATH = None


def read_config():
    """Parse the "key = value" config file. Missing file is not an error."""
    values = {}
    try:
        with open(CONFIG_PATH) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    except FileNotFoundError:
        pass
    return values


def read_legacy_device():
    """The pre-config-file layout: a file containing just the device path."""
    try:
        with open(LEGACY_DEVICE_PATH) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    except FileNotFoundError:
        pass
    return None


def resolve_device(config):
    """Command line wins, then the config file, then the legacy device file."""
    if len(sys.argv) > 1:
        return sys.argv[1]

    device = config.get("device") or read_legacy_device()
    if device:
        return device

    raise SystemExit(
        f"No mouse configured. Run setup.sh to choose one, or pass a device path.\n"
        f"Expected 'device = /dev/input/by-id/...' in {CONFIG_PATH}"
    )


def resolve_recenter(config):
    """-> (enabled, margin_px). A typo in a hand-edited file should not stop the
    daemon, so bad values warn and fall back."""
    raw = config.get("pan_recenter")
    if raw is None:
        enabled = RECENTER
    else:
        enabled = raw.strip().lower() in ("1", "true", "yes", "on")

    margin = RECENTER_MARGIN
    raw_margin = config.get("pan_recenter_margin_px")
    if raw_margin is not None:
        try:
            margin = int(float(raw_margin))
        except ValueError:
            log(f"pan_recenter_margin_px: '{raw_margin}' is not a number; "
                f"using {RECENTER_MARGIN}")
            margin = RECENTER_MARGIN
        else:
            clamped = max(0, min(600, margin))
            if clamped != margin:
                log(f"pan_recenter_margin_px: {margin} is outside 0-600; "
                    f"using {clamped}")
                margin = clamped
    return enabled, margin


def resolve_left_click(config):
    """-> (key_name, key_code_or_None)."""
    raw = (config.get("left_click_key") or "").strip().lower()
    if not raw:
        raw = LEFT_CLICK_KEY
    if raw not in LEFT_CLICK_CODES:
        log(f"left_click_key: '{raw}' is not recognised; using {LEFT_CLICK_KEY}")
        raw = LEFT_CLICK_KEY
    return raw, LEFT_CLICK_CODES[raw]


def resolve_deadzone(config):
    raw = config.get("pan_deadzone_px")
    if raw is None:
        return PAN_DEADZONE
    try:
        value = int(float(raw))
    except ValueError:
        log(f"pan_deadzone_px: '{raw}' is not a number; using {PAN_DEADZONE}")
        return PAN_DEADZONE
    clamped = max(0, min(500, value))
    if clamped != value:
        log(f"pan_deadzone_px: {value} is outside 0-500; using {clamped}")
    return clamped


def resolve_yield(config):
    raw = config.get("pan_yield_to_other_mice")
    if raw is None:
        return PAN_YIELD
    return raw.strip().lower() in ("1", "true", "yes", "on")


def resolve_pan_idle(config):
    """Seconds to hold the pan button after motion stops. A bad value is a typo in a
    hand-edited file, so warn and fall back rather than refusing to start."""
    raw = config.get("pan_idle_release_ms")
    if raw is None:
        return DEFAULT_PAN_IDLE_RELEASE_MS / 1000.0

    try:
        ms = float(raw)
    except ValueError:
        log(f"pan_idle_release_ms: '{raw}' is not a number; "
            f"using {DEFAULT_PAN_IDLE_RELEASE_MS}ms")
        return DEFAULT_PAN_IDLE_RELEASE_MS / 1000.0

    clamped = max(PAN_IDLE_MIN_MS, min(PAN_IDLE_MAX_MS, ms))
    if clamped != ms:
        log(f"pan_idle_release_ms: {ms:g}ms is outside "
            f"{PAN_IDLE_MIN_MS}-{PAN_IDLE_MAX_MS}ms; using {clamped:g}ms")
    return clamped / 1000.0


def make_modifier_device():
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
        return UInput({ecodes.EV_KEY: keys}, name=MODIFIER_NAME,
                      vendor=VIRTUAL_VENDOR, product=VIRTUAL_PRODUCT_MODIFIER,
                      phys="onshape-gate/modifier")
    except OSError as exc:
        log(f"cannot create the Ctrl device ({exc})")
        return None


def other_mice(gated_path):
    """Every pointing device except the one we grabbed (and our own virtual clone)."""
    gated = os.path.realpath(gated_path)
    return [p for p in sorted(glob.glob(MOUSE_GLOB))
            if os.path.realpath(p) != gated]


def watch_other_pointers(gated_path):
    """Read-only watch on the other mice; any activity ends the current pan stroke.

    Opened without EVIOCGRAB, so those mice keep working completely normally.
    """
    opened = {}
    while True:
        if not PAN_YIELD:
            time.sleep(5)
            continue

        for path in other_mice(gated_path):
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
                        translator = GATE.translator
                        if translator is not None:
                            translator.yield_stroke()
                        break
            except OSError:
                # Unplugged: drop it and let the next scan pick it back up.
                try:
                    dev.close()
                except Exception:
                    pass
                opened.pop(path, None)


def wait_for_device(path):
    warned = False
    while not os.path.exists(path):
        if not warned:
            log(f"waiting for {path} to appear (mouse unplugged?)")
            warned = True
        time.sleep(1)
    return evdev.InputDevice(path)


def main():
    global DEVICE_PATH, PAN_IDLE_RELEASE, RECENTER, RECENTER_MARGIN, PAN_YIELD
    global PAN_DEADZONE
    global LEFT_CLICK_KEY, LEFT_CLICK_CODE
    config = read_config()
    DEVICE_PATH = resolve_device(config)
    PAN_IDLE_RELEASE = resolve_pan_idle(config)
    RECENTER, RECENTER_MARGIN = resolve_recenter(config)
    PAN_YIELD = resolve_yield(config)
    PAN_DEADZONE = resolve_deadzone(config)
    LEFT_CLICK_KEY, LEFT_CLICK_CODE = resolve_left_click(config)

    threading.Thread(target=serve, daemon=True).start()
    threading.Thread(target=watch_focus, daemon=True).start()
    threading.Thread(target=pan_timer, daemon=True).start()
    threading.Thread(target=watch_other_pointers, args=(DEVICE_PATH,),
                     daemon=True).start()
    recentring = (f"recentre at {RECENTER_MARGIN}px"
                  if RECENTER and POINTER.ok else "recentring off")
    log(f"listening on 127.0.0.1:{PORT}, gating {DEVICE_PATH} "
        f"(pan by Ctrl+right-drag, dead zone {PAN_DEADZONE}px, "
        f"idle release {PAN_IDLE_RELEASE * 1000:.0f}ms, "
        f"{recentring})")

    while True:
        dev = wait_for_device(DEVICE_PATH)
        ui = None
        modifier = None
        try:
            ui = UInput.from_device(dev, name=VIRTUAL_NAME,
                                    vendor=VIRTUAL_VENDOR,
                                    product=VIRTUAL_PRODUCT_MOUSE,
                                    phys="onshape-gate/mouse")

            # Ctrl lives on this device, so panning cannot work without it.
            modifier = make_modifier_device()
            if modifier is None:
                raise SystemExit(
                    "cannot create the Ctrl device, so Ctrl+right-drag panning is "
                    "impossible; see the error above")
            translator = Translator(ui, modifier)
            GATE.translator = translator
            dev.grab()
            log(f"grabbed {dev.name} -> virtual '{VIRTUAL_NAME}'")
            for event in dev.read_loop():
                if GATE.is_open():
                    translator.handle(event)
                else:
                    translator.note_while_closed(event)
        except OSError as exc:
            log(f"device error ({exc}); waiting for it to come back")
            time.sleep(1)
        finally:
            GATE.translator = None
            for close in (dev.ungrab, dev.close):
                try:
                    close()
                except Exception:
                    pass
            for handle in (ui, modifier):
                if handle is not None:
                    try:
                        handle.close()
                    except Exception:
                        pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
