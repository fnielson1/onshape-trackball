#!/usr/bin/env python3
"""Turn the left-hand mouse into an Onshape navigation device.

The physical device is grabbed exclusively so the desktop never sees it, but its
events never reach the OS as real input either. While onshape.com is frontmost,
each translated action is sent as a message over a local WebSocket channel to a
Chrome extension, which dispatches the matching untrusted DOM event directly on
Onshape's page — see the Channel class below.

Translation, while the gate is open (default mapping; see PAN_REQUIRES_RIGHT_BUTTON):
    right button + motion   -> pan    (a Ctrl-tagged synthetic drag)
    motion                  -> rotate (a plain synthetic drag)
    wheel                   -> zoom   (a synthetic wheel event)
    left button             -> clear the selection (a synthetic Space tap; the
                                click itself is never sent)

The gate opens when both of these agree:
  * the focused window belongs to Google Chrome
  * the Chrome extension says the focused window's active tab is on onshape.com

Either signal alone is insufficient: the window manager cannot see a tab's URL, and
the extension's MV3 service worker can be suspended while Chrome sits in the
background.

Everything in this file is platform-neutral. The two things that are not — exclusive
capture and focus tracking — live behind `backend.py`, which picks an implementation
from sys.platform. Both test suites exec this file, so it must stay importable on a
machine with no driver, no display and no hardware.
"""

import base64
import hashlib
import json
import os
import signal
import socket
import struct
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
DEFAULT_PAN_IDLE_RELEASE_MS = 5
PAN_IDLE_RELEASE = DEFAULT_PAN_IDLE_RELEASE_MS / 1000.0

# Outside this range the feature stops behaving like a pan stroke at all: too low
# and a stroke ends between mouse reports, too high and the button hangs around
# long after you stop.
PAN_IDLE_MIN_MS = 5
PAN_IDLE_MAX_MS = 2000

# How often the idle check runs, so a release lands within PAN_TICK of the deadline.
PAN_TICK = 0.05

# The usable view rect comes from the extension's content script, which probes the
# page to find the region that genuinely belongs to the 3D view — the canvas minus
# whatever Onshape stacks on top of it. Goes stale if the script stops reporting (tab
# closed, extension reloaded), at which point there is no region and panning stops.
CANVAS_STALE_AFTER = 5.0

# How many contextmenu reports to keep for --status. Enough to cover a session's
# worth of "it just happened again" without the snapshot becoming a log file.
CONTEXT_MENU_HISTORY = 20

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

# How often the cached window rect is re-read, to survive a move or resize that
# happens without any focus change.
GEOMETRY_REFRESH = 2.0

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
# PAN_DEADZONE, and slowing it down mid-pan would just make a long pan take longer
# to land for no benefit.
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

# Symbolic names for the buttons the translator forwards untranslated — the left
# button when left_click_key is "none", and any other button the gated mouse
# happens to have. BTN_RIGHT is deliberately absent: it always goes through the
# pan/rotate press/motion/release messages instead, never this generic one.
BUTTON_NAMES = {
    ecodes.BTN_LEFT: "LEFT",
    ecodes.BTN_MIDDLE: "MIDDLE",
    ecodes.BTN_SIDE: "SIDE",
    ecodes.BTN_EXTRA: "EXTRA",
}

# Symbolic names for the wheel axes, sent over the channel instead of injected —
# content.js maps these onto a synthetic WheelEvent's deltaX/deltaY.
WHEEL_NAMES = dict(zip(WHEEL_CODES, WHEEL_AXES))

# The channel's own port, separate from PORT (the human-readable /status server).
# Bound to 127.0.0.1 only, same as PORT.
CHANNEL_PORT = 47654

# Fixed by extension/manifest.json's own "key" field, which pins Chrome's
# otherwise-random unpacked-extension ID to this value. Lets the channel validate
# the WebSocket handshake's Origin without a setup-time detection step or a config
# key to keep in sync — see design.md's "extension's origin is pinned" decision.
EXTENSION_ID = "oihhifecnmdihmijdhcmlhgilbagdmod"
EXPECTED_ORIGIN = f"chrome-extension://{EXTENSION_ID}"

WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


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


def _read_exact(sock, n):
    """Read exactly n bytes, or None if the connection closed first."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _read_http_headers(sock):
    """Read a request line and headers up to the blank line.

    Small and bounded — this only ever has to parse a handshake from our own
    extension, not arbitrary HTTP, so it does not need a general parser.
    """
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise ValueError("connection closed during handshake")
        data += chunk
        if len(data) > 16384:
            raise ValueError("handshake too large")
    head, _, _rest = data.partition(b"\r\n\r\n")
    headers = {}
    for line in head.decode("iso-8859-1").split("\r\n")[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    return headers


def _ws_read_frame(sock):
    """-> (opcode, payload) or None on a closed connection. Assumes one frame per
    message (no fragmentation) — true of every control frame by spec, and true of
    anything this trusted local client sends, since it never has to send much."""
    header = _read_exact(sock, 2)
    if header is None:
        return None
    b0, b1 = header[0], header[1]
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    if length == 126:
        ext = _read_exact(sock, 2)
        if ext is None:
            return None
        length = struct.unpack("!H", ext)[0]
    elif length == 127:
        ext = _read_exact(sock, 8)
        if ext is None:
            return None
        length = struct.unpack("!Q", ext)[0]
    mask_key = _read_exact(sock, 4) if masked else None
    payload = _read_exact(sock, length) if length else b""
    if payload is None:
        return None
    if masked and mask_key:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def _ws_send_frame(sock, opcode, payload):
    """Server-to-client frames are never masked — only client-to-server ones are."""
    length = len(payload)
    if length < 126:
        header = bytes([0x80 | opcode, length])
    elif length < 65536:
        header = bytes([0x80 | opcode, 126]) + struct.pack("!H", length)
    else:
        header = bytes([0x80 | opcode, 127]) + struct.pack("!Q", length)
    sock.sendall(header + payload)


class Channel:
    """A minimal WebSocket server for one trusted local client: the extension's
    background script. Implements just enough of RFC 6455 for that — the HTTP
    Upgrade handshake, text frames, client-frame unmasking, ping/pong, close —
    binding to 127.0.0.1 only and checking the handshake's Origin against the
    extension's own fixed ID, so an arbitrary web page cannot open this and observe
    the gated mouse's raw motion.

    One-directional in practice: the daemon pushes every translated gesture, and
    nothing the client sends back is ever acted on beyond keeping the connection
    alive. A second client connecting simply replaces the first.
    """

    def __init__(self, port):
        self._port = port
        self._lock = threading.Lock()
        self._sock = None
        self._last_send_at = 0.0
        self.on_disconnect = None      # optional callback, called with no args
        self.on_connect = None         # optional callback, called with no args

    @property
    def connected(self):
        with self._lock:
            return self._sock is not None

    def seconds_since_send(self):
        """None before the first message this run, so /status can tell "never sent
        one yet" apart from "sent one a while ago"."""
        with self._lock:
            sent_at = self._last_send_at
        return None if sent_at == 0.0 else round(time.monotonic() - sent_at, 1)

    def send(self, message):
        """Silently does nothing when no client is connected — every caller relies
        on this rather than checking `connected` itself first, so a disconnect
        between the check and the send can never slip through."""
        with self._lock:
            sock = self._sock
        if sock is None:
            return
        try:
            _ws_send_frame(sock, 0x1, json.dumps(message).encode())
        except OSError:
            self._drop(sock)
            return
        with self._lock:
            self._last_send_at = time.monotonic()

    def _drop(self, client):
        """Safe to call more than once, and safe to call with a socket that has
        already been replaced by a newer connection — in which case this is a
        no-op, not a disconnect of the current one."""
        with self._lock:
            if self._sock is not client:
                return
            self._sock = None
        try:
            client.close()
        except OSError:
            pass
        log("channel: extension disconnected")
        callback = self.on_disconnect
        if callback is not None:
            try:
                callback()
            except Exception:
                pass

    def _handshake(self, client):
        headers = _read_http_headers(client)
        origin = headers.get("origin", "")
        if origin != EXPECTED_ORIGIN:
            client.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            raise ValueError(f"origin {origin!r} did not match {EXPECTED_ORIGIN!r}")
        key = headers.get("sec-websocket-key")
        if not key:
            client.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            raise ValueError("missing Sec-WebSocket-Key")
        accept = base64.b64encode(
            hashlib.sha1((key + WS_MAGIC).encode()).digest()).decode()
        client.sendall((
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode())

    def _read_loop(self, client):
        try:
            while True:
                frame = _ws_read_frame(client)
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == 0x8:          # close
                    break
                if opcode == 0x9:          # ping -> pong
                    _ws_send_frame(client, 0xA, payload)
                # text/binary/pong: nothing the client sends changes anything here.
        except OSError:
            pass
        finally:
            self._drop(client)

    def serve_forever(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", self._port))
        listener.listen(1)
        while True:
            client, _addr = listener.accept()
            try:
                self._handshake(client)
            except Exception as exc:
                log(f"channel handshake failed: {exc}")
                try:
                    client.close()
                except OSError:
                    pass
                continue
            with self._lock:
                self._sock = client
            log("channel: extension connected")
            callback = self.on_connect
            if callback is not None:
                try:
                    callback()
                except Exception:
                    pass
            threading.Thread(target=self._read_loop, args=(client,), daemon=True).start()


CHANNEL = Channel(CHANNEL_PORT)


class Translator:
    """Runs the gesture state machine and sends the result over the channel.

    There is exactly one output sink — CHANNEL — and no OS-level input device is
    ever written to. Every write used to also have to worry about ordering against
    a second, independent Ctrl device (MODIFIER_SETTLE) and about surviving
    Chrome's and Onshape's own click-vs-drag heuristics (the Ctrl-tag-on-release
    trick, the drag-nudge); none of that applies to untrusted synthetic dispatch,
    confirmed live, so none of it exists here.

    Every method below still runs under self._lock, because the idle-release timer
    and the gate can both end a stroke concurrently with the read loop.
    """

    def __init__(self, channel):
        self._channel = channel
        self._stroke_open = False   # is a press currently open on the channel?
        self._gesture = None        # "pan" | "rotate" | None: which gesture, if any
        self._lock = threading.Lock()
        self._held = set()          # other buttons currently forwarded as pressed
        self._panning = False       # is the bare-motion-driven stroke currently held?
        self._last_motion = 0.0
        self._right_down = False
        self._travel_x = 0.0
        self._travel_y = 0.0
        self._rotate_remainder_x = 0.0
        self._rotate_remainder_y = 0.0
        self.left_taps = 0
        self._pending_dx = 0
        self._pending_dy = 0
        self.last_release = None    # see _close_stroke

    # --- callers must hold self._lock -------------------------------------------

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

        # Only the free gesture actually needs protecting from an accidental bump — a
        # pan that fires because a hand rested on the trackball is disruptive in a way
        # a stray twitch of rotate is not, and unlike pan, rotate has no press of its
        # own to bracket it with, so the dead zone was ever only standing in for one.
        # So this only ever applies when bare motion is driving pan; rotate is exempt
        # entirely.
        is_pan = not PAN_REQUIRES_RIGHT_BUTTON
        if is_pan and PAN_DEADZONE > 0 and self._within_deadzone():
            return          # not a deliberate push yet

        # A pan presses the right button somewhere on screen, so it must not start
        # without a region known to be 3D view and nothing else. Without one, that
        # press lands wherever the cursor happens to be — a toolbar button, the
        # feature tree — and the release opens a context menu.
        if GATE.view_rect() is None:
            return

        # Fails closed the same way: with nobody to open a stroke for, there is
        # nothing to be "panning" toward — _open_stroke itself would just no-op,
        # but _panning must not go true over a gesture that never actually opened.
        if not self._channel.connected:
            return

        self._open_stroke("pan" if is_pan else "rotate")
        self._panning = True
        self._reset_deadzone()

    def _handle_right_button(self, event):
        """The motion-driven stroke already holds the right button, so a physical
        press must not press it twice — it hands off instead: the current drag
        closes and immediately reopens as whichever gesture the button now drives,
        at the same virtual position, which reads as one continuous gesture to
        Onshape as long as no motion lands in between. Caller holds self._lock."""
        pressed = event.value != 0

        if pressed:
            self._right_down = True
            if self._panning:
                self._panning = False
                self._close_stroke()
                self._open_stroke("pan" if PAN_REQUIRES_RIGHT_BUTTON else "rotate")
                return              # button is already down; swallow the duplicate
            self._open_stroke("pan" if PAN_REQUIRES_RIGHT_BUTTON else "rotate")
            return

        self._right_down = False
        self._end_pan()
        self._close_stroke()

    def _tap_key(self, key_name):
        """Caller holds self._lock."""
        self._channel.send({"type": "tap", "key": key_name})
        self.left_taps += 1

    def _open_stroke(self, gesture):
        """Begin a press/motion/release sequence on the channel, unless one is
        already open or the channel has nobody to open it for — fail closed here,
        the same way a pan already refuses to start with no verified view region."""
        if self._stroke_open:
            return
        if not self._channel.connected:
            return
        self._channel.send({"type": "press", "gesture": gesture})
        self._stroke_open = True
        self._gesture = gesture

    def _close_stroke(self):
        """Idempotent — every caller that might be ending a stroke calls this
        unconditionally rather than checking first, so nothing can race past it."""
        if not self._stroke_open:
            return
        self._channel.send({"type": "release"})
        self._stroke_open = False
        self._gesture = None
        # Kept so a context-menu report arriving from the page can be lined up
        # against the release that most likely caused it.
        self.last_release = {"at": time.monotonic()}

    def _end_pan(self):
        """Release the bare-motion-driven stroke, if one is open."""
        if not self._panning:
            return
        self._close_stroke()
        self._panning = False
        self._reset_deadzone()      # the next stroke earns its own dead zone

    def _track(self, code, value):
        if value == 1:
            self._held.add(code)
        elif value == 0:
            self._held.discard(code)

    def _send_click(self, code, value):
        """The left button when left_click_key is "none", or any other button the
        gated mouse has. Caller holds self._lock."""
        name = BUTTON_NAMES.get(code)
        if name is None:
            return          # nothing sensible to forward
        self._channel.send({"type": "click", "code": name, "value": value})

    def _write_motion(self, code, value, is_rotate):
        """Accumulate one axis of motion, scaled down for whichever gesture is
        currently rotate — pan gets the raw value unconditionally, see
        ROTATE_SCALE. Flushed as one combined dx/dy message on the next
        SYN_REPORT, the same way the old OS-level device batched a write/syn pair.
        """
        if is_rotate and ROTATE_SCALE != 1.0:
            value = self._scale_rotate(code, value)
            if value == 0:
                return
        if code == ecodes.REL_X:
            self._pending_dx += value
        else:
            self._pending_dy += value

    def _flush_motion(self):
        """Send accumulated motion as one message, dropping it instead if no stroke
        is open — there is no real cursor left to track a hand through the dead
        zone with, so a fresh stroke simply starts from the page's own seeded
        position rather than resuming wherever an untracked drift left off."""
        dx, dy = self._pending_dx, self._pending_dy
        self._pending_dx = 0
        self._pending_dy = 0
        if (dx or dy) and self._stroke_open:
            self._channel.send({"type": "motion", "dx": dx, "dy": dy})

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
        """The right button is still physically down, but something else closed
        its stroke mid-hold while the hand never let go — the view region
        disappearing and coming back, or the channel dropping and reconnecting.
        Motion resuming means the gesture should resume too, exactly as a fresh
        press would — this mirrors _handle_right_button's own press branch, since a
        real button event is never coming to do it for us.
        """
        self._open_stroke("pan" if PAN_REQUIRES_RIGHT_BUTTON else "rotate")
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
                        # End the pan first: the tap must not land mid-drag.
                        self._end_pan()
                        self._tap_key(LEFT_CLICK_KEY)
                    return                  # swallow press and release alike
                if value:
                    # Any other button starts a real click; don't leave a pan running
                    # underneath it.
                    self._end_pan()
                self._track(code, value)
                self._send_click(code, value)
                return

            if etype == ecodes.EV_REL and code in WHEEL_CODES:
                if self._panning:
                    self._end_pan()
                name = WHEEL_NAMES.get(code)
                if name is not None:
                    self._channel.send({"type": "wheel", "code": name, "value": value})
                return

            if etype == ecodes.EV_REL and code in MOTION_AXES:
                if self._stroke_open and GATE.view_rect() is None:
                    # The safe region went away mid-stroke — a dialog opened over the
                    # view, or the extension stopped reporting. There is now nowhere
                    # known to be harmless, so let go rather than keep dragging across
                    # whatever appeared.
                    self._end_pan()
                    self._close_stroke()
                    return
                bare_motion = not self._right_down
                if bare_motion:
                    if code == ecodes.REL_X:
                        self._travel_x += value
                    else:
                        self._travel_y += value
                    self._start_pan()
                    self._last_motion = time.monotonic()
                elif not self._stroke_open and GATE.view_rect() is not None:
                    # The button is held but nothing is emitted for it: something
                    # else closed the stroke mid-gesture and the button never came
                    # back up to give it a real press to resume on. Without this,
                    # the only way out is releasing the physical button — see
                    # _resume_button_gesture.
                    self._resume_button_gesture()
                # See the PAN_REQUIRES_RIGHT_BUTTON comment: bare motion and "button
                # held" only mean rotate/pan once paired with this setting.
                self._write_motion(code, value, bare_motion == PAN_REQUIRES_RIGHT_BUTTON)
                return

            if etype == ecodes.EV_SYN:
                self._flush_motion()
                return

            # EV_MSC and anything else: no translated meaning, nothing to forward.

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
                self._end_pan()
            elif not self._panning and now - self._last_motion > PAN_IDLE_RELEASE:
                # Let a stale nudge expire, so movement from a while ago cannot
                # combine with a fresh one to cross the dead zone.
                self._reset_deadzone()

    def release_all(self):
        """Gate closed. Lift anything we left down."""
        with self._lock:
            # Cleared before the early return, not after it. Leaving it set here is
            # how it went stale: close the gate while the physical right button is
            # down and the flag survived for the rest of the session.
            self._right_down = False
            if not self._panning and not self._held and not self._stroke_open:
                return
            self._end_pan()
            self._close_stroke()
            for code in self._held:
                name = BUTTON_NAMES.get(code)
                if name is not None:
                    self._channel.send({"type": "click", "code": name, "value": 0})
            self._held.clear()
            self._right_down = False
            self._reset_deadzone()
            log("gate closed mid-gesture; released held buttons")

    def channel_disconnected(self):
        """The channel dropped mid-gesture. There is nothing left to send a release
        to, so this just stops believing a stroke is open rather than trying to
        buffer or retry — see design.md's "fail closed on a missing channel"
        decision."""
        with self._lock:
            self._panning = False
            self._stroke_open = False
            self._gesture = None
            self._pending_dx = 0
            self._pending_dy = 0
            self._reset_deadzone()

    def snapshot(self):
        with self._lock:
            return {
                "panning": self._panning,
                "right_button_down": self._right_down,
                "left_taps": self.left_taps,
                "stroke_open": self._stroke_open,
                "gesture": self._gesture,
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
            # Told to the extension explicitly, not inferred from message traffic:
            # it is what decides whether to show the on-page cursor icons and hide
            # the real one, and a gap between gestures must not look like "closed".
            CHANNEL.send({"type": "gate", "open": new})
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
                                         something, and the other mouse's real click
                                         landed somewhere we had declared safe
          overlay, outside our region -> the other mouse's real click landed on an
                                         overlay outside the region — expected; only
                                         the region itself is ever verified safe
          on the canvas               -> Onshape's own canvas menu, most likely
                                         reacting to our own synthetic dispatch —
                                         not a bug here
          not while panning           -> not ours at all: the other mouse's real
                                         click, or a real right-click on the gated
                                         mouse's own hardware before it was captured
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
                # it to be, so this is Onshape's own canvas menu, not a bug here.
                why = "on the canvas — Onshape's own canvas menu"
            elif in_region is True:
                why = "on an overlay INSIDE the region we reported safe — probe missed it"
            elif in_region is False:
                why = "on an overlay outside the region — the cursor should not have been there"
            else:
                why = "on an overlay, with no region reported at the time"

            record = {
                "at": time.strftime("%H:%M:%S"),
                "target": str(event.get("target"))[:120],
                "on_canvas": on_canvas,
                "at_point": [event.get("x"), event.get("y")],
                "in_reported_region": in_region,
                # Whether a menu actually reached the user. False now covers two
                # cases: something on the page calling preventDefault on its own, and
                # content.js's own targeted suppression (see MENU_SUPPRESS_WINDOW_MS
                # and lastSyntheticRelease there) recognising this as our own
                # gesture's release and preventDefault-ing plus Escape-tapping it.
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

            outcome = "menu shown" if record["menu_shown"] else "handled by the page"
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
        state["pan_deadzone_px"] = PAN_DEADZONE
        state["pan_requires_right_button"] = PAN_REQUIRES_RIGHT_BUTTON
        state["rotate_scale"] = ROTATE_SCALE
        state["left_click_key"] = LEFT_CLICK_KEY
        state["device_attached"] = translator is not None
        state["channel_connected"] = CHANNEL.connected
        state["seconds_since_channel_send"] = CHANNEL.seconds_since_send()
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


def serve_channel():
    CHANNEL.serve_forever()


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
    """Tell the extension whatever gesture was open is over before the process goes
    away, on a best-effort basis — an ordinary stop, not the fail-closed path a
    crash or a dropped channel already covers on its own. The `finally` in main()
    covers a normal exit; these cover the ways a service gets stopped. A hard kill
    still cannot be caught anywhere, but there is nothing left for it to strand:
    with no OS-level device ever written to, there is no button state a kill can
    leave stuck.
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


def watch_focus():
    backend.watch_focus(GATE.set_chrome_focused)


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


def _on_channel_disconnect():
    translator = GATE.translator
    if translator is not None:
        translator.channel_disconnected()


def _on_channel_connect():
    # A fresh connection — first load, or a reconnect after the service worker was
    # suspended — has missed every "gate" message sent while it was gone. Without
    # this, a reconnect while the gate happens to be open would leave the page
    # showing the real cursor indefinitely, since nothing tells it otherwise until
    # the next actual transition.
    CHANNEL.send({"type": "gate", "open": GATE.is_open()})


def main():
    global DEVICE_PATH, PAN_IDLE_RELEASE
    global PAN_DEADZONE, PAN_REQUIRES_RIGHT_BUTTON
    global LEFT_CLICK_KEY, LEFT_CLICK_CODE, ROTATE_SCALE
    _open_log()
    config = read_config()
    DEVICE_PATH = resolve_device(config)
    PAN_IDLE_RELEASE = resolve_pan_idle(config)
    PAN_DEADZONE = resolve_deadzone(config)
    PAN_REQUIRES_RIGHT_BUTTON = resolve_pan_button(config)
    LEFT_CLICK_KEY, LEFT_CLICK_CODE = resolve_left_click(config)
    ROTATE_SCALE = resolve_rotate_scale(config)

    # Kept even though recentring is gone: window rects still matter for the pan
    # press's view-region check, and per-monitor DPI awareness is what keeps that
    # rect in the same coordinate space as everything else on Windows.
    log(f"coordinate space: {backend.declare_dpi_aware()}")
    install_signal_handlers()

    CHANNEL.on_disconnect = _on_channel_disconnect
    CHANNEL.on_connect = _on_channel_connect
    threading.Thread(target=serve, daemon=True).start()
    threading.Thread(target=serve_channel, daemon=True).start()
    threading.Thread(target=watch_focus, daemon=True).start()
    threading.Thread(target=pan_timer, daemon=True).start()
    pan_trigger = ("hold the right button to pan, move to rotate"
                   if PAN_REQUIRES_RIGHT_BUTTON else
                   "move to pan, hold the right button to rotate")
    log(f"listening on 127.0.0.1:{PORT}, channel on 127.0.0.1:{CHANNEL_PORT}, "
        f"gating {DEVICE_PATH} "
        f"({pan_trigger}, dead zone {PAN_DEADZONE}px, "
        f"idle release {PAN_IDLE_RELEASE * 1000:.0f}ms, "
        f"rotate scale {ROTATE_SCALE:g}, backend {backend.name})")

    while True:
        dev = None
        try:
            dev = backend.open_gated_device(DEVICE_PATH)
            translator = Translator(CHANNEL)
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
            # Tell the extension whatever gesture was open is over. Nothing here can
            # leave a real button held: the device is captured exclusively but never
            # written to, so handing it back returns the mouse to normal outright.
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


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
