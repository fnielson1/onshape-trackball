#!/usr/bin/env python3
"""Turn the left-hand mouse into an Onshape navigation device.

The physical device is grabbed exclusively so the desktop never sees it, and its
events are translated onto a synthetic device only while onshape.com is frontmost.

Translation, while the gate is open (default mapping; see PAN_REQUIRES_RIGHT_BUTTON):
    right button + motion   -> pan    (synthesised as a held Ctrl + right-drag)
    motion                  -> rotate (plain right-drag, synthesised to bracket it)
    wheel                   -> zoom   (passed through unchanged)
    left button             -> clear the selection (taps space; the click is swallowed)

The gate opens when both of these agree:
  * the focused window belongs to Google Chrome
  * the Chrome extension says the focused window's active tab is on onshape.com

Either signal alone is insufficient: the window manager cannot see a tab's URL, and
the extension's MV3 service worker can be suspended while Chrome sits in the
background.

Everything in this file is platform-neutral. The four things that are not — exclusive
capture, synthetic output, the cursor, and focus tracking — live behind `backend.py`,
which picks an implementation from sys.platform. Both test suites exec this file, so
it must stay importable on a machine with no driver, no display and no hardware.
"""

import hashlib
import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import backend
import codes as ecodes

# Settings live in a "key = value" file written by the setup script. Everything here
# is resolved lazily in main() so the module stays importable (test_translator.py
# execs this file) on a machine with no config yet.
_CONFIG_DIR = backend.config_dir()
CONFIG_PATH = os.path.join(_CONFIG_DIR, "config")

# Superseded by CONFIG_PATH; still read so an older install keeps working.
LEGACY_DEVICE_PATH = os.path.join(_CONFIG_DIR, "device")

PORT = 47653


# Editing this file leaves a daemon running code that no longer exists on disk, and
# until now nothing noticed: the installer's drift check compares config values, and
# a code edit touches none of them. So the daemon publishes a fingerprint of the
# source it actually started from and the installer compares it against the file.
#
# globals().get rather than a bare __file__ because this module is also exec'd
# without one — by test_translator.py and by the installer's own _gate_namespace,
# neither of which should blow up on import just to compute a hash.
SOURCE_PATH = os.path.abspath(globals().get("__file__") or "gate.py")


def source_hash(path=None):
    """Short SHA-256 of gate.py, or None when it cannot be read."""
    try:
        with open(path or SOURCE_PATH, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()[:12]
    except OSError:
        return None


# Captured at import, deliberately: computing this per request would re-read the
# edited file and report the new hash, so a stale daemon would look up to date and
# the check would never fire once.
SOURCE_HASH = source_hash()

# Panning is Ctrl + right-drag; plain right-drag rotates. Ctrl is held on a second
# virtual device and the right button on the mouse one.
#
# Onshape's other pan gesture, middle-drag, is deliberately not an option: repeated
# presses pair into a double middle-click, which Onshape reads as Zoom to Fit.

# Which of pan/rotate the physical right button being held maps to. The other
# gesture is whatever bare motion does instead.
#
# True (default): hold the button to pan, move without it to rotate — pan is the
#     deliberate, bracketed gesture; rotate is what bare motion does the rest of the
#     time, which is also the more frequent one.
# False: the original mapping — bare motion pans, holding the button rotates.
#
# Only this changes. Whichever gesture bare motion drives still needs PAN_DEADZONE,
# PAN_IDLE_RELEASE and recentring to invent a beginning and an end for it — a mouse
# never reports "I stopped moving" — and whichever gesture the button drives still
# gets those for free from the press and release themselves. The names below (
# _panning, _start_pan, PAN_DEADZONE, ...) describe *that* motion-driven half of the
# machinery, not literally "pan" — which gesture it ends up producing is exactly
# this setting.
PAN_REQUIRES_RIGHT_BUTTON = True

# Ctrl must land before the button and lift after it. The other order leaves a moment
# of plain right-drag, which Onshape reads as rotate.
# The button release and the Ctrl release travel through two independent devices, so
# nothing but this gap enforces their order — and out of order means a moment of
# plain right-drag, which Onshape rotates on.
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

# The static part of the edge margin. On its own it can only be right for one pan
# speed: the edge check is throttled (RECENTER_CHECK_INTERVAL), so the distance the
# cursor covers between two checks grows with how fast the ball is spun, while a
# fixed margin does not. A quick flick therefore clears 20px and lands on the feature
# tree before the next check runs. What actually bounds the overshoot is the travel
# term added in _recenter_if_near_edge; this stays small so that panning slowly is
# not penalized with a shrunken view and needless recentres.
RECENTER_MARGIN = 35

# The usable view rect comes from the extension's content script, which probes the
# page to find the region that genuinely belongs to the 3D view — the canvas minus
# whatever Onshape stacks on top of it. Goes stale if the script stops reporting (tab
# closed, extension reloaded), at which point there is no region and panning stops.
CANVAS_STALE_AFTER = 5.0

# How many contextmenu reports to keep for --status. Enough to cover a session's
# worth of "it just happened again" without the snapshot becoming a log file.
CONTEXT_MENU_HISTORY = 20

# The warp must not land while the pan button is down: Onshape would read the jump as
# one enormous pan. So the button is lifted, the desktop is given a moment to settle,
# then it is pressed again. Only paid at an edge, not per motion event.
RECENTER_SETTLE = 0.012

# Reading the cursor is a round-trip; a 1000Hz mouse would make that per-event cost
# real, so the edge check is throttled.
RECENTER_CHECK_INTERVAL = 0.02

# Both mice drive one shared pointer, so a held pan gesture applies to whatever the
# *other* mouse does: its motion pans, and its wheel reaches the page with the button
# still down rather than as a clean scroll. Watching the other mice read-only and
# dropping the gesture the moment one of them stirs keeps them independent.
PAN_YIELD = True

# How far the mouse must travel before a pan actually starts. Measured as net
# displacement, not distance travelled, so jitter that wanders out and back never
# trips it — only a deliberate push in some direction does.
#
# Applies only when bare motion is driving pan (PAN_REQUIRES_RIGHT_BUTTON = false) —
# never rotate, zoom or the left button. Rotate gets no dead zone of its own: the
# cursor keeps tracking your hand throughout regardless, and unlike pan it has no
# button press to bracket it with, so anything this ate would come straight out of a
# small deliberate rotate's own drag distance, with nothing to show for it.
#
# Overridden by pan_deadzone_px in the config file; this is the fallback.
PAN_DEADZONE = 10

# How far the *other* mouse must travel, net, before its motion is treated as
# deliberate and drops the gated mouse's pan or rotate. Below this, resting a hand on
# it or bumping it in passing does not interrupt the stroke.
#
# Motion only — a button press on the other mouse always yields immediately
# regardless of this value, since pressing a button is unambiguous. Like
# pan_deadzone_px, an idle other mouse forgets what it has accumulated rather than
# banking a stale nudge toward a later one.
#
# Does not apply while bare motion is driving rotate (the default mapping) — see
# yield_stroke's bare_motion_is_rotate. Under that mapping bare motion drives
# rotate, which gets no dead zone on the way in (see
# _start_pan's own is_pan check) and, symmetrically, none on the way out either.
PAN_YIELD_DEADZONE = 10

# A press and release with nothing in between is a click, and Chrome opens a context
# menu on one. That part is now covered for free: _release_right_button tags rotate's
# release with Ctrl, which extension/content.js checks first and unconditionally,
# ahead of distance — no motion needed.
#
# What still needs real, on-screen distance is Onshape itself. Confirmed by tracing a
# live repro: Onshape opens its own canvas menu straight out of its own mouseup
# handling — no `contextmenu` event involved, Ctrl included or not — purely on how the
# cursor moved between press and release. A gesture that survived the dead zone can
# still land here, because ROTATE_SCALE halves (by default) whatever distance reaches
# the screen, deliberately, to keep rotation from feeling hypersensitive.
#
# The exact threshold could not be pinned precisely — driving Onshape's canvas
# directly to bracket it hit a real confound: every accepted "drag" also rotates the
# camera, and after enough trials the cursor drifts off the part onto empty
# background, where the click/drag boundary behaves differently, so later readings in
# the same session cannot be trusted against earlier ones. What held up: real hardware
# never failed above 6.7px in the daemon's own release logs, and a real right-click
# with only 3.0px of travel did open Onshape's menu. This sits with margin above that
# window. Every pixel here is still a pixel Onshape rotates by, so it is not free,
# just far cheaper than the fix that came before it.
#
# Overridden by min_drag_px in the config file; this is the fallback.
MIN_DRAG_PX = 8

# The nudge above is split across this many samples rather than written as one lump
# sum, NUDGE_STEP_INTERVAL apart, so it looks like the tail of a real stroke rather
# than a teleport — real hardware never reports a lump sum for real motion, a rolling
# trackball sends dozens of small samples for any genuine movement. Live testing could
# not fully separate this from the camera-drift confound above, so treat it as a
# reasonable-and-harmless precaution rather than a proven necessity.
NUDGE_STEPS = 3
NUDGE_STEP_INTERVAL = 0.008

# How often the cached window rect is re-read, to survive a move or resize that
# happens without any focus change.
GEOMETRY_REFRESH = 2.0

# Yielding to another mouse releases the pan, but the next twitch of the gated mouse
# would re-press it immediately — so a burst of wheel-zoom turns into a press/release
# ping-pong. Staying released briefly after a yield lets a zoom finish in peace.
YIELD_COOLDOWN = 0.15

# The extension pushes on every real transition and heartbeats every 30s via
# chrome.alarms. If we go this long with nothing at all, assume it died and fail closed.
STALE_AFTER = 120.0

# Rotation is plain motion passed straight through to Onshape's orbit gesture, with
# no scaling anywhere between the physical mouse and the browser. A trackball's raw
# counts become orbit degrees 1:1, which reads as far more sensitive than an ordinary
# pointing device, where the same counts only move a cursor. Below 1 tones that down;
# above 1 speeds it up.
#
# Panning is deliberately not scaled here: it already has its own dedicated feel via
# PAN_DEADZONE and RECENTER_MARGIN, and slowing the cursor down mid-pan would just
# make the window take longer to cross for no benefit.
#
# Overridden by rotate_scale in the config file; this is the fallback.
DEFAULT_ROTATE_SCALE = 0.5
ROTATE_SCALE = DEFAULT_ROTATE_SCALE

# Outside this range the setting stops doing what it says: below the floor a normal
# push barely rotates the model at all, and above the ceiling it is no longer a scale
# so much as a typo.
ROTATE_SCALE_MIN = 0.05
ROTATE_SCALE_MAX = 5.0

MOTION_AXES = (ecodes.REL_X, ecodes.REL_Y)


def log(msg):
    # Tolerant of there being nowhere to write. Under pythonw both streams are None
    # until _open_log runs, and a logging call is never worth taking the daemon down.
    stream = sys.stdout or sys.stderr
    if stream is None:
        return
    try:
        print(f"[gate] {msg}", file=stream, flush=True)
    except Exception:
        pass


def _open_log():
    """Send both streams to a file when there is no console.

    The daemon is started directly by the scheduled task rather than through a shell,
    so there is no `>>` to redirect it — and pythonw gives a process no stdout at all.
    Without this a crash would leave nothing behind but a service that stopped.

    Only when the streams are genuinely absent: run from a console, it keeps the
    console.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    path = os.environ.get("ONSHAPE_GATE_LOG")
    if not path:
        base = (os.environ.get("LOCALAPPDATA") if sys.platform == "win32"
                else os.environ.get("XDG_STATE_HOME"))
        base = base or os.path.expanduser("~")
        path = os.path.join(base, "onshape-trackball", "gate.log")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        stream = open(path, "a", buffering=1, encoding="utf-8", errors="replace")
    except OSError:
        return
    sys.stdout = stream
    sys.stderr = stream


POINTER = backend.Pointer()


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
        self._edge_travel = 0.0     # distance sent since the last edge check
        self.recenters = 0
        self.yields = 0
        self._last_press_pos = None
        self._last_press_time = 0.0
        self._travel_x = 0.0
        self._travel_y = 0.0
        self._rotate_remainder_x = 0.0
        self._rotate_remainder_y = 0.0
        self._other_travel_x = 0.0
        self._other_travel_y = 0.0
        self._other_travel_at = 0.0
        self.drag_nudges = 0
        self._yield_until = 0.0
        self.presses_recentred = 0
        self.left_taps = 0
        self.last_release = None    # see _release_right_button

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
        # Carried per-stroke, same as the travel above: a fresh stroke starts rotate
        # scaling's sub-pixel carry from zero rather than resuming a leftover from
        # however long ago the last one ended.
        self._rotate_remainder_x = 0.0
        self._rotate_remainder_y = 0.0

    def _start_pan(self):
        if self._panning:
            return

        now = time.monotonic()
        if now < self._yield_until:
            return          # another mouse is mid-gesture; stay out of its way

        # Only the free gesture actually needs protecting from an accidental bump — a
        # pan that fires because a hand rested on the trackball is disruptive in a way
        # a stray twitch of rotate is not, and unlike pan, rotate has no press of its
        # own to bracket it with, so the dead zone was ever only standing in for one.
        # But every pixel it eats here is a pixel that never reaches Onshape: the
        # button does not go down until the dead zone clears, so a small deliberate
        # rotate that stops right after can leave almost nothing for Onshape's own
        # click-vs-drag check to see, and it reads the release as a click regardless
        # of what the browser's own contextmenu event does — see the Ctrl tag in
        # _release_right_button, which cannot reach that decision at all. So this only
        # ever applies when bare motion is driving pan; rotate is exempt entirely.
        is_pan = not PAN_REQUIRES_RIGHT_BUTTON
        if is_pan and PAN_DEADZONE > 0 and self._within_deadzone():
            return          # not a deliberate push yet

        # A pan presses the right button somewhere on screen, so it must not start
        # without a region known to be 3D view and nothing else. Without one, that
        # press lands wherever the cursor happens to be — a toolbar button, the
        # feature tree — and the release opens a context menu.
        if GATE.view_rect() is None:
            return

        self._press_pan()
        self._panning = True
        self._reset_deadzone()

    def _handle_right_button(self, event):
        """The motion-driven stroke already holds the right button, so a physical
        press must not press it twice — it hands off instead, adding or dropping
        Ctrl to match whichever gesture the button now drives. Caller holds
        self._lock."""
        pressed = event.value != 0

        if pressed:
            self._right_down = True
            if self._panning:
                self._panning = False
                if PAN_REQUIRES_RIGHT_BUTTON:
                    if not self._ctrl_down:
                        self._ctrl(1)
                        time.sleep(MODIFIER_SETTLE)
                elif self._ctrl_down:
                    time.sleep(MODIFIER_SETTLE)
                    self._ctrl(0)
                return              # button is already down; swallow the duplicate
            if PAN_REQUIRES_RIGHT_BUTTON and not self._ctrl_down:
                self._ctrl(1)
                time.sleep(MODIFIER_SETTLE)
            self._repress_right_button()
            return

        self._right_down = False
        self._end_pan(syn=False)
        if self._right_emitted:
            self._release_right_button()
            if self._ctrl_down:
                time.sleep(MODIFIER_SETTLE)
                self._ctrl(0)
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
        # Ctrl only when bare motion is the pan trigger; with PAN_REQUIRES_RIGHT_BUTTON
        # this motion-driven stroke is rotate instead, which wants plain right-drag.
        if not PAN_REQUIRES_RIGHT_BUTTON and not self._ctrl_down:
            self._ctrl(1)
            time.sleep(MODIFIER_SETTLE)
        self._repress_right_button()

    def _repress_right_button(self):
        """Just the button half of a press, with Ctrl left exactly as it is.

        Used for the free gesture's own first press (Ctrl was just settled by
        _emit_pan_down, above) and for a recentre's re-press on *either* gesture —
        there, Ctrl (if any) survived the lift untouched, so re-deciding it from
        PAN_REQUIRES_RIGHT_BUTTON would be wrong for whichever gesture the button
        itself is currently driving.
        """
        if not self._right_emitted:
            self._ui.write(ecodes.EV_KEY, ecodes.BTN_RIGHT, 1)
            self._ui.syn()
            self._right_emitted = True

    def _nudge_for_drag(self):
        """Guarantee a little displacement before releasing a button.

        A press and release with nothing between them is a click, and Ctrl +
        right-click opens Chrome's context menu mid-pan. Moving first makes every
        gesture read as a drag instead. The move does pan or rotate the model too,
        which is why only the shortfall is added — not a flat MIN_DRAG_PX on top of
        whatever real motion already happened. A stroke sitting a couple of pixels
        under the line does not deserve the full nudge; that would read as a stray
        jump right when a small, deliberate rotate is trying to land on an angle.

        Returns (measured_px, nudged) for the release record: how far the cursor had
        actually travelled since the press, and whether we had to make up the
        difference. A context menu arriving just after a release with a small
        measured_px is the signature of a drag that the page read as a click.
        """
        position = POINTER.position() if POINTER.ok else None

        measured = None
        nudge_px = MIN_DRAG_PX
        dx = 0.0
        if position is not None and self._last_press_pos is not None:
            dx = position[0] - self._last_press_pos[0]
            dy = position[1] - self._last_press_pos[1]
            measured = (dx * dx + dy * dy) ** 0.5
            if measured >= MIN_DRAG_PX:
                return measured, False      # already dragged far enough
            # +1 rather than the bare shortfall: the nudge only moves along X while
            # `measured` is the true diagonal, so a stroke that was mostly vertical
            # needs a little more than the arithmetic difference to actually clear
            # the line once combined with whatever X displacement already existed.
            nudge_px = int(MIN_DRAG_PX - measured) + 1

        # Continue in whichever direction the stroke was already heading, so the
        # nudge reads as a bit more of the same rotate rather than a flick the other
        # way — a rotate to the left must not get finished off with a nudge to the
        # right. Only when the existing motion was ambiguous (essentially no X
        # component) does this default to the right, for the edge check below to
        # override if needed.
        direction = -1 if dx < 0 else 1

        geometry = GATE.view_rect()
        if position is not None and geometry is not None:
            win_x, _, win_w, _ = geometry
            if direction > 0 and position[0] + nudge_px > win_x + win_w - 8:
                direction = -1
            elif direction < 0 and position[0] - nudge_px < win_x + 8:
                direction = 1

        # Split rather than written as one lump sum, so it looks like the tail of a
        # real stroke rather than a teleport — see NUDGE_STEPS.
        steps = min(NUDGE_STEPS, max(1, nudge_px))
        base = nudge_px // steps
        remainder = nudge_px - base * steps
        for i in range(steps):
            step_px = base + (1 if i < remainder else 0)
            if step_px == 0:
                continue
            self._ui.write(ecodes.EV_REL, ecodes.REL_X, direction * step_px)
            self._ui.syn()
            if i < steps - 1:
                time.sleep(NUDGE_STEP_INTERVAL)
        self.drag_nudges += 1
        return measured, True

    def _release_right_button(self):
        """Every right-button release goes through here, so none of them can ever
        land as a bare click.

        Pan's release already carries Ctrl for free — it has been down throughout —
        and extension/content.js checks exactly that, unconditionally and ahead of
        anything it infers from distance: `event.ctrlKey` is the *first* thing
        suppressionReason() looks at. Rotate never holds Ctrl during the drag — that
        is what makes it rotate rather than pan — so its release had nothing to give
        the page the same certainty, and fell back to the nudge below as the only
        signal available. That fallback works, but every pixel it adds is a pixel
        Onshape rotates by, which reads as a stray flick right when a small,
        deliberate rotate is trying to land on an angle.

        So rotate's release borrows pan's own trick instead: Ctrl goes down just for
        the instant the button lifts, then comes back up. No motion is sent while it
        is up, so this changes nothing about the drag Onshape already saw — only
        what the release itself looks like to the page, which is now indistinguishable
        from pan's — for the browser's own menu.

        Onshape's own canvas menu is a separate problem this cannot touch: it opens
        straight out of Onshape's own mouseup handling, no `contextmenu` event
        involved, and does not care about Ctrl either way — only how far the cursor
        actually moved on screen between press and release, which ROTATE_SCALE has
        usually shrunk well below what a real hand movement would suggest. The nudge
        below is what still has to cover that: real, if modest, on-screen distance,
        because that is the only thing Onshape's own check reads.

        The syn is essential, not tidiness: the modifier lives on a second device
        with its own stream, and an unflushed button release would arrive *after*
        the Ctrl release that follows it — leaving a moment of plain right-drag,
        which Onshape rotates on.
        """
        if not self._right_emitted:
            return

        tag_ctrl = not self._ctrl_down
        if tag_ctrl:
            self._ctrl(1)
            time.sleep(MODIFIER_SETTLE)

        measured, nudged = self._nudge_for_drag()

        # Kept so a context-menu report arriving from the page can be lined up against
        # the release that most likely caused it. This is the daemon's half of that
        # story; the page supplies what the menu actually opened on.
        position = POINTER.position() if POINTER.ok else None
        self.last_release = {
            "at": time.monotonic(),
            "moved_px": None if measured is None else round(measured, 1),
            "nudged": nudged,
            "at_pos": position,
            "in_view_rect": _inside(GATE.view_rect(), position),
        }

        self._ui.write(ecodes.EV_KEY, ecodes.BTN_RIGHT, 0)
        self._ui.syn()
        self._right_emitted = False

        if tag_ctrl:
            time.sleep(MODIFIER_SETTLE)
            self._ctrl(0)

    def _emit_pan_up(self, keep_modifier=False):
        """keep_modifier is for the recentre, which lifts and re-presses the button
        mid-stroke. Dropping Ctrl there too was pure cost: it doubled the number of
        modifier transitions and opened a window where the desktop could see the
        button still down with Ctrl already gone, which is a plain right-drag and
        rotates.
        """
        # Button first: dropping Ctrl while the button is still down would leave a
        # plain right-drag, which Onshape rotates on.
        #
        # Released unconditionally. This runs only while something owns the button —
        # _end_pan requires _panning, the recentre requires _right_emitted, which
        # covers the button-held gesture too — and the hand-off between the two never
        # comes through here: _handle_right_button settles Ctrl itself and clears
        # _panning directly, without touching the button. Skipping the release when
        # _right_down looked true therefore protected nothing, and if that flag ever
        # went stale it left the button held with the wrong Ctrl state: whichever
        # gesture that is would run on until something else happened to clear it.
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

    def _write_motion(self, code, value, is_rotate):
        """Write one axis of motion, scaled down for whichever gesture is currently
        rotate. Pan gets the raw value unconditionally — see ROTATE_SCALE.
        """
        if is_rotate and ROTATE_SCALE != 1.0:
            value = self._scale_rotate(code, value)
            if value == 0:
                return
        self._ui.write(ecodes.EV_REL, code, value)

    def _scale_rotate(self, code, value):
        """value * ROTATE_SCALE, without losing sub-pixel motion to rounding: the
        remainder left over from one sample carries into the next, so a slow,
        deliberate rotate at a low scale still moves eventually instead of every
        sample individually truncating to zero.
        """
        if code == ecodes.REL_X:
            total = self._rotate_remainder_x + value * ROTATE_SCALE
            scaled = int(total)
            self._rotate_remainder_x = total - scaled
        else:
            total = self._rotate_remainder_y + value * ROTATE_SCALE
            scaled = int(total)
            self._rotate_remainder_y = total - scaled
        return scaled

    def _resume_button_gesture(self):
        """The right button is still physically down, but a yield dropped its
        synthetic press mid-stroke (see yield_stroke) while the hand never let go.
        Motion resuming means the gesture should resume too, exactly as a fresh
        press would — this mirrors _handle_right_button's own press branch, since a
        real button event is never coming to do it for us.
        """
        if PAN_REQUIRES_RIGHT_BUTTON and not self._ctrl_down:
            self._ctrl(1)
            time.sleep(MODIFIER_SETTLE)
        self._repress_right_button()
        self._last_press_pos = POINTER.position() if POINTER.ok else None
        self._last_press_time = time.monotonic()
        self._reset_deadzone()      # a resumed stroke earns its own dead zone too

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
                # Per-axis absolute sum rather than true distance: it overshoots a
                # diagonal by up to 41%, and a margin erring large is the safe
                # direction to err in. Tracked for whichever gesture the button is
                # currently driving too — a long sweep runs the cursor out of screen
                # the same way regardless of which one it is. Measured on the raw
                # value, ahead of any rotate scaling below — erring larger only makes
                # the recentre margin more conservative, never less.
                self._edge_travel += abs(value)
                bare_motion = not self._right_down
                if bare_motion:
                    if code == ecodes.REL_X:
                        self._travel_x += value
                    else:
                        self._travel_y += value
                    self._start_pan()
                    self._last_motion = time.monotonic()
                elif (not self._right_emitted and not self._ctrl_down
                        and time.monotonic() >= self._yield_until
                        and GATE.view_rect() is not None):
                    # The button is held but nothing is emitted for it: a yield cut
                    # this stroke off mid-gesture and the button never came back up
                    # to give it a real press to resume on. Without this, the only
                    # way out is releasing the physical button — see
                    # _resume_button_gesture.
                    self._resume_button_gesture()
                # See the PAN_REQUIRES_RIGHT_BUTTON comment: bare motion and "button
                # held" only mean rotate/pan once paired with this setting.
                self._write_motion(code, value, bare_motion == PAN_REQUIRES_RIGHT_BUTTON)
                self._recenter_if_near_edge()
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
        # Reset on every check, so this only ever describes recent motion: a flick
        # widens the margin while it lasts and stops counting as soon as it stops.
        lookahead = self._edge_travel
        self._edge_travel = 0.0

        geometry = GATE.view_rect()
        if geometry is None or geometry[2] <= 0 or geometry[3] <= 0:
            # The safe region went away mid-stroke — a dialog opened over the view, or
            # the extension stopped reporting. There is now nowhere the cursor is known
            # to be harmless, so let go of the button rather than keep dragging it
            # across whatever appeared.
            if self._panning:
                self._end_pan(syn=True)
            return
        win_x, win_y, win_w, win_h = geometry

        # Widened by the distance covered since the last check, which bounds the
        # overshoot two ways at once: that much motion has already been sent but may
        # not have reached the cursor yet, so the reading below is stale by up to
        # this far, and roughly that much more can land before the next check.
        #
        # A small window must not end up with a safe region of zero.
        margin = min(RECENTER_MARGIN + lookahead, win_w // 3, win_h // 3)

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

        if not self._right_emitted:
            # No button is held — by either gesture — so there is nothing to protect
            # and no press cycle to pay for; just put the cursor back. This case
            # matters more than it sounds: motion keeps flowing between strokes and
            # through the yield cooldown, and without recentring here the cursor
            # wanders clean out of the view and over the feature tree.
            POINTER.warp(centre_x, centre_y)
            self.recenters += 1
            self._last_motion = time.monotonic()
            return

        self._emit_pan_up(keep_modifier=True)
        self._ui.syn()
        time.sleep(RECENTER_SETTLE)

        POINTER.warp(centre_x, centre_y)
        time.sleep(RECENTER_SETTLE)

        if self._panning:
            self._press_pan()
        else:
            # The button-held gesture. Ctrl (if any) rode through the lift above
            # untouched — _press_pan would re-decide it from PAN_REQUIRES_RIGHT_BUTTON,
            # which is only correct for the free gesture — so only the button itself
            # needs to come back.
            self._last_press_pos = (centre_x, centre_y)
            self._last_press_time = time.monotonic()
            self._repress_right_button()
        self._ui.syn()
        self.recenters += 1

        # A recentre only happens mid-stroke, and it stalls this loop for two
        # RECENTER_SETTLE naps. Without refreshing the idle deadline, the timer can
        # fire straight afterwards and tear down the stroke we just restored — which
        # gets much more likely as PAN_IDLE_RELEASE approaches the stall duration.
        self._last_motion = time.monotonic()

    def yield_stroke(self, dx=0.0, dy=0.0, immediate=False):
        """Another pointing device stirred. Drop everything the gated mouse is
        holding, so the shared pointer is not carrying it into someone else's
        gesture.

        Deliberately not conditioned on _panning. After the hand-off to rotate that
        flag is already False while the right button stays held, so keying off it
        meant the right mouse could cancel a pan but never a rotate — the button then
        stayed down until the physical button happened to come back up.

        Plain motion is subject to its own dead zone, pan_yield_deadzone_px, measured
        the same way as the gated mouse's: net displacement since the stroke started
        yielding to it. A button press or a wheel turn is `immediate` and skips it —
        there is no such thing as an accidental click or an accidental scroll. So
        does motion while bare motion is driving rotate rather than pan — see
        bare_motion_is_rotate below.
        """
        with self._lock:
            if not (self._panning or self._right_emitted or self._ctrl_down):
                self._other_travel_x = 0.0
                self._other_travel_y = 0.0
                return

            # Symmetric with _start_pan's own is_pan check: whichever gesture bare
            # motion is currently driving gets the same dead-zone treatment leaving
            # that it gets entering. Under the default mapping bare motion drives
            # rotate, which _start_pan gives no dead zone at all — so nothing here
            # should make it wait to leave either. That symmetry also sidesteps a
            # trap an idle-time check falls into: a trackball's ball keeps rolling a
            # little after the hand lifts, so _last_motion keeps getting refreshed on
            # its own and "the gated device has gone quiet" is not something the
            # other mouse can wait around for.
            #
            # Everything else — bare-motion pan (the non-default mapping), and any
            # gesture already handed off to the physical button, which drops
            # _panning to False regardless of mapping — keeps the dead zone. A
            # button-held gesture especially: a pause mid-hold is normal, so it
            # still needs protecting from a bump.
            bare_motion_is_rotate = self._panning and PAN_REQUIRES_RIGHT_BUTTON
            if not immediate and not bare_motion_is_rotate and PAN_YIELD_DEADZONE > 0:
                now = time.monotonic()
                if now - self._other_travel_at > PAN_IDLE_RELEASE:
                    self._other_travel_x = 0.0
                    self._other_travel_y = 0.0
                self._other_travel_at = now
                self._other_travel_x += dx
                self._other_travel_y += dy
                if (self._other_travel_x * self._other_travel_x
                        + self._other_travel_y * self._other_travel_y
                        < PAN_YIELD_DEADZONE * PAN_YIELD_DEADZONE):
                    return      # not a deliberate move yet

            self._end_pan(syn=True)          # no-op when not panning
            self._release_right_button()     # this is what catches the rotate
            if self._ctrl_down:
                time.sleep(MODIFIER_SETTLE)  # button before Ctrl, as everywhere else
                self._ctrl(0)
            self._ui.syn()

            self._reset_deadzone()
            self._other_travel_x = 0.0
            self._other_travel_y = 0.0
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
        self._context_menus = []
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
        geometry = backend.window_geometry(window_id)
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
        """The area the cursor may occupy while panning: the region the extension has
        verified belongs to the 3D view and nothing else. None when there is no fresh
        one, and None means do not pan.

        It is deliberately not the canvas's own rect. Onshape lays controls over the
        canvas and, with the feature list collapsed, the canvas runs underneath the
        slide-out entirely. Those are ordinary DOM elements, so a right-button release
        over one opens a context menu and a press over one activates it.

        There used to be a fall back to the whole Chrome window here, for when the
        extension went quiet. That was worse than no answer: the window includes the
        tab strip, the bookmarks bar and the feature tree, so the fallback licensed
        the cursor to sit on precisely the things this rect exists to avoid. Panning
        stops instead — the same way the gate itself fails closed.
        """
        with self._lock:
            if (self._canvas is not None
                    and time.monotonic() - self._canvas_at <= CANVAS_STALE_AFTER):
                return self._canvas
            return None

    def record_context_menu(self, events):
        """A contextmenu event fired on the page. Log it with enough of both sides to
        say *why*, because that is the thing that is otherwise impossible to catch: by
        the time you see the menu, whatever caused it is long gone.

        The page supplies what the menu opened on and where. The daemon supplies what
        it was doing at the time. Between them the causes separate cleanly:

          overlay, inside our region  -> the region was wrong; the probe missed
                                         something, and the pointer was legitimately
                                         somewhere we had declared safe
          overlay, outside our region -> the cursor got somewhere it should not have:
                                         a recentre that did not keep up, or a press
                                         that landed before one
          canvas, short release       -> our own drag read as a click, so Onshape (or
                                         Chrome) offered a menu on the canvas itself;
                                         look at moved_px against MIN_DRAG_PX
          not while panning           -> not ours at all; the other mouse, or a real
                                         right-click
        """
        translator = self.translator
        release = getattr(translator, "last_release", None) if translator else None
        panning = bool(translator and translator._panning)

        for event in events:
            if not isinstance(event, dict):
                continue

            since_release = (None if not release
                             else round(time.monotonic() - release["at"], 3))
            on_canvas = bool(event.get("onCanvas"))
            in_region = event.get("inRegion")

            if not panning and (since_release is None or since_release > 1.0):
                why = "not during a pan (other mouse, or a real right-click)"
            elif on_canvas:
                # On the canvas the region was right and the cursor was where we meant
                # it to be, so the cause is the gesture itself — but only if the drag
                # really was too short. A menu after a healthy drag is Onshape's own
                # canvas menu, which is a different thing entirely and not a bug here.
                moved = release and release.get("moved_px")
                if moved is None:
                    why = "on the canvas, with no release measurement to compare"
                elif moved < MIN_DRAG_PX:
                    why = (f"on the canvas after only {moved}px of drag "
                           f"(MIN_DRAG_PX={MIN_DRAG_PX}) — the page read it as a click")
                else:
                    why = (f"on the canvas after {moved}px of drag — a real drag, so "
                           f"this is Onshape's own canvas menu, not a stray press")
            elif in_region is True:
                why = "on an overlay INSIDE the region we reported safe — probe missed it"
            elif in_region is False:
                why = "on an overlay outside the region — the cursor should not have been there"
            else:
                why = "on an overlay, with no region reported at the time"

            suppressed = bool(event.get("suppressed"))
            record = {
                "at": time.strftime("%H:%M:%S"),
                "target": str(event.get("target"))[:120],
                "on_canvas": on_canvas,
                "at_point": [event.get("x"), event.get("y")],
                "in_reported_region": in_region,
                # Whether a menu actually reached the user. `suppressed` is our own
                # doing; `menu_shown` is the outcome, and stays true if something raised
                # one anyway — which is the only way to tell suppression is working
                # rather than merely being attempted.
                "suppressed": suppressed,
                "suppressed_why": event.get("suppressedWhy"),
                "menu_shown": not event.get("prevented", False),
                "drag_px": event.get("dragPx"),
                "ctrl_held": event.get("ctrl"),
                "panning": panning,
                "seconds_since_release": since_release,
                "release": release,
                "why": why,
            }

            with self._lock:
                self._context_menus.append(record)
                del self._context_menus[:-CONTEXT_MENU_HISTORY]

            outcome = "SUPPRESSED" if suppressed else (
                "menu shown" if record["menu_shown"] else "handled by the page")
            log(f"context menu [{outcome}]: {why} | target={record['target']} "
                f"at={record['at_point']} drag={record['drag_px']}px")

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
                "context_menus": list(reversed(self._context_menus)),
                "canvas_diag_age": (None if self._canvas_diag_at == 0.0
                                    else round(time.monotonic() - self._canvas_diag_at, 1)),
            }
            translator = self.translator
        state["device"] = DEVICE_PATH
        state["platform"] = backend.name
        state["code"] = SOURCE_HASH
        state["pan_idle_release_ms"] = round(PAN_IDLE_RELEASE * 1000)
        state["pan_recenter"] = RECENTER and POINTER.ok
        state["pan_recenter_margin_px"] = RECENTER_MARGIN
        state["pan_yield_to_other_mice"] = PAN_YIELD
        state["pan_deadzone_px"] = PAN_DEADZONE
        state["pan_yield_deadzone_px"] = PAN_YIELD_DEADZONE
        state["pan_requires_right_button"] = PAN_REQUIRES_RIGHT_BUTTON
        state["rotate_scale"] = ROTATE_SCALE
        state["min_drag_px"] = MIN_DRAG_PX
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
            menus = body.get("contextmenu")
            if isinstance(menus, list) and menus:
                GATE.record_context_menu(menus[:CONTEXT_MENU_HISTORY])
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


def _inside(rect, position):
    """Was `position` within `rect`? None when either is unknown, which is itself worth
    recording — "we had no idea where the cursor was" is a different diagnosis from
    "the cursor was outside the view"."""
    if rect is None or position is None:
        return None
    x, y, w, h = rect
    return x <= position[0] < x + w and y <= position[1] < y + h


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


def install_signal_handlers():
    """Lift whatever is held before the process goes away.

    The synthetic button and Ctrl outlive us: they are pressed in the real input
    stream, so nothing releases them on our behalf. The `finally` in main() covers an
    ordinary exit, and these cover the ways a service gets stopped. A hard kill still
    cannot be caught anywhere — `clear_stale_holds` is what cleans up after one.
    """
    def bail(signum, _frame):
        translator = GATE.translator
        if translator is not None:
            try:
                translator.release_all()
            except Exception:
                pass
        log(f"stopping on signal {signum}")
        raise SystemExit(0)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, bail)
        except (ValueError, OSError):
            pass            # not the main thread, or not supported here


def clear_stale_holds(ui, modifier):
    """Undo a previous run that was killed outright while holding something.

    A button-up nobody pressed is ignored, so this is safe to do unconditionally —
    and it turns "my right button is stuck down" into "restart the service".
    """
    try:
        ui.write(ecodes.EV_KEY, ecodes.BTN_RIGHT, 0)
        ui.syn()
        if modifier is not None:
            modifier.write(ecodes.EV_KEY, ecodes.KEY_LEFTCTRL, 0)
            modifier.syn()
    except Exception as exc:
        log(f"could not clear stale holds: {exc}")


def watch_focus():
    backend.watch_focus(GATE.set_chrome_focused)


def watch_other_pointers(gated_identifier):
    def on_activity(dx=0.0, dy=0.0, immediate=False):
        translator = GATE.translator
        if translator is not None:
            translator.yield_stroke(dx, dy, immediate)

    backend.watch_other_pointers(gated_identifier, on_activity,
                                 lambda: PAN_YIELD)


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

    setup = "setup.cmd" if backend.name == "windows" else "setup.sh"
    raise SystemExit(
        f"No mouse configured. Run {setup} to choose one, or pass a device.\n"
        f"Expected '{backend.DEVICE_HINT}' in {CONFIG_PATH}"
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


def resolve_min_drag_px(config):
    """A bad value is a typo in a hand-edited file, so warn and fall back rather than
    refusing to start."""
    raw = config.get("min_drag_px")
    if raw is None:
        return MIN_DRAG_PX
    try:
        value = int(float(raw))
    except ValueError:
        log(f"min_drag_px: '{raw}' is not a number; using {MIN_DRAG_PX}")
        return MIN_DRAG_PX
    clamped = max(0, min(200, value))
    if clamped != value:
        log(f"min_drag_px: {value} is outside 0-200; using {clamped}")
    return clamped


def resolve_yield_deadzone(config):
    raw = config.get("pan_yield_deadzone_px")
    if raw is None:
        return PAN_YIELD_DEADZONE
    try:
        value = int(float(raw))
    except ValueError:
        log(f"pan_yield_deadzone_px: '{raw}' is not a number; using {PAN_YIELD_DEADZONE}")
        return PAN_YIELD_DEADZONE
    clamped = max(0, min(500, value))
    if clamped != value:
        log(f"pan_yield_deadzone_px: {value} is outside 0-500; using {clamped}")
    return clamped


def resolve_yield(config):
    raw = config.get("pan_yield_to_other_mice")
    if raw is None:
        return PAN_YIELD
    return raw.strip().lower() in ("1", "true", "yes", "on")


def resolve_rotate_scale(config):
    """Multiplier applied to rotate's motion. A bad value is a typo in a hand-edited
    file, so warn and fall back rather than refusing to start."""
    raw = config.get("rotate_scale")
    if raw is None:
        return DEFAULT_ROTATE_SCALE
    try:
        value = float(raw)
    except ValueError:
        log(f"rotate_scale: '{raw}' is not a number; using {DEFAULT_ROTATE_SCALE}")
        return DEFAULT_ROTATE_SCALE
    clamped = max(ROTATE_SCALE_MIN, min(ROTATE_SCALE_MAX, value))
    if clamped != value:
        log(f"rotate_scale: {value:g} is outside "
            f"{ROTATE_SCALE_MIN}-{ROTATE_SCALE_MAX}; using {clamped:g}")
    return clamped


def resolve_pan_button(config):
    raw = config.get("pan_requires_right_button")
    if raw is None:
        return PAN_REQUIRES_RIGHT_BUTTON
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


def main():
    global DEVICE_PATH, PAN_IDLE_RELEASE, RECENTER, RECENTER_MARGIN, PAN_YIELD
    global PAN_DEADZONE, PAN_YIELD_DEADZONE, PAN_REQUIRES_RIGHT_BUTTON
    global LEFT_CLICK_KEY, LEFT_CLICK_CODE, ROTATE_SCALE, MIN_DRAG_PX
    _open_log()
    config = read_config()
    DEVICE_PATH = resolve_device(config)
    PAN_IDLE_RELEASE = resolve_pan_idle(config)
    RECENTER, RECENTER_MARGIN = resolve_recenter(config)
    PAN_YIELD = resolve_yield(config)
    PAN_DEADZONE = resolve_deadzone(config)
    PAN_YIELD_DEADZONE = resolve_yield_deadzone(config)
    PAN_REQUIRES_RIGHT_BUTTON = resolve_pan_button(config)
    LEFT_CLICK_KEY, LEFT_CLICK_CODE = resolve_left_click(config)
    ROTATE_SCALE = resolve_rotate_scale(config)
    MIN_DRAG_PX = resolve_min_drag_px(config)

    # Must happen before any window rect is read: on Windows an un-declared process
    # gets virtualised coordinates from GetWindowRect while the cursor answers in
    # physical ones, and recentring then warps somewhere unrelated.
    log(f"coordinate space: {backend.declare_dpi_aware()}")
    install_signal_handlers()

    threading.Thread(target=serve, daemon=True).start()
    threading.Thread(target=watch_focus, daemon=True).start()
    threading.Thread(target=pan_timer, daemon=True).start()
    threading.Thread(target=watch_other_pointers, args=(DEVICE_PATH,),
                     daemon=True).start()
    recentring = (f"recentre at {RECENTER_MARGIN}px"
                  if RECENTER and POINTER.ok else "recentring off")
    pan_trigger = ("hold the right button to pan, move to rotate"
                   if PAN_REQUIRES_RIGHT_BUTTON else
                   "move to pan, hold the right button to rotate")
    log(f"listening on 127.0.0.1:{PORT}, gating {DEVICE_PATH} "
        f"({pan_trigger}, dead zone {PAN_DEADZONE}px, "
        f"yield dead zone {PAN_YIELD_DEADZONE}px, "
        f"idle release {PAN_IDLE_RELEASE * 1000:.0f}ms, "
        f"rotate scale {ROTATE_SCALE:g}, min drag {MIN_DRAG_PX}px, "
        f"{recentring}, backend {backend.name})")

    while True:
        dev = None
        ui = None
        modifier = None
        try:
            dev = backend.open_gated_device(DEVICE_PATH)
            ui = backend.VirtualOutput(dev.template)

            # Ctrl lives on this device, so panning cannot work without it.
            modifier = backend.modifier_output()
            if modifier is None:
                raise SystemExit(
                    "cannot create the Ctrl device, so Ctrl+right-drag panning is "
                    "impossible; see the error above")
            clear_stale_holds(ui, modifier)
            translator = Translator(ui, modifier)
            GATE.translator = translator
            log(f"grabbed {dev.name}")
            for event in dev.events():
                if GATE.is_open():
                    translator.handle(event)
                else:
                    translator.note_while_closed(event)
        except Exception as exc:
            # Deliberately broad. evdev raises OSError, but the Interception binding
            # raises its own RuntimeError, and a backend that fails in some third way
            # must still leave a retrying daemon rather than a dead mouse and no
            # process. SystemExit and KeyboardInterrupt are BaseException, so the
            # real exits still get through.
            log(f"device error ({exc}); waiting for it to come back")
            time.sleep(1)
        finally:
            translator = GATE.translator
            GATE.translator = None
            # Anything still held has to come up before the device is handed back,
            # or the mouse returns to normal with a button stuck down.
            if translator is not None:
                try:
                    translator.release_all()
                except Exception:
                    pass
            if dev is not None:
                try:
                    dev.close()
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
