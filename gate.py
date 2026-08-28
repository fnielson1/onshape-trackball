#!/usr/bin/env python3
"""Turn the left-hand mouse into an Onshape navigation device.

The physical device is grabbed exclusively (EVIOCGRAB) so X11 never sees it, and its
events are translated onto a virtual uinput clone only while onshape.com is frontmost.

Translation, while the gate is open:
    motion                  -> pan   (synthesised as a held middle-button drag)
    right button + motion   -> rotate (passed straight through; Onshape does this natively)
    wheel, left button      -> passed through unchanged (zoom, select)

The gate opens when both of these agree:
  * X11 says the focused window belongs to Google Chrome  (tracked via `xprop -root -spy`)
  * the Chrome extension says the focused window's active tab is on onshape.com

Either signal alone is insufficient: X11 cannot see a tab's URL, and the extension's
MV3 service worker can be suspended while Chrome sits in the background.
"""

import json
import os
import sys
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import evdev
from evdev import UInput, ecodes

# Which mouse to gate. Chosen by setup.sh and written to this file; a path given on
# the command line overrides it. Resolved lazily in main() so the module stays
# importable (test_translator.py execs this file) on a machine with no config yet.
CONFIG_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "onshape-trackball", "device",
)

VIRTUAL_NAME = "Onshape-gated Mouse"
PORT = 47653

# Onshape's stock "View manipulation" preference: middle-drag pans, right-drag rotates.
# If you switch Onshape to a SolidWorks/Creo-style mapping, change PAN_BUTTON to match.
PAN_BUTTON = ecodes.BTN_MIDDLE

# A pan stroke ends after this long without motion, so the synthetic button is never
# left down. Panning then feels like trackpad strokes: push, pause, push again.
PAN_IDLE_RELEASE = 0.2
PAN_TICK = 0.05

# The extension pushes on every real transition and heartbeats every 30s via
# chrome.alarms. If we go this long with nothing at all, assume it died and fail closed.
STALE_AFTER = 120.0

CHROME_WM_CLASSES = ("google-chrome", "chromium")
MOTION_AXES = (ecodes.REL_X, ecodes.REL_Y)


def log(msg):
    print(f"[onshape-mouse] {msg}", flush=True)


class Translator:
    """Owns the virtual device and the pan state machine.

    Every write to the virtual device goes through this lock, because the idle-release
    timer and the gate can both end a pan stroke concurrently with the read loop.
    """

    def __init__(self, ui):
        self._ui = ui
        self._lock = threading.Lock()
        self._held = set()          # real buttons we have forwarded as pressed
        self._panning = False       # is PAN_BUTTON currently synthesised down?
        self._last_motion = 0.0
        self._right_down = False

    # --- callers must hold self._lock -------------------------------------------

    def _start_pan(self):
        if not self._panning:
            self._ui.write(ecodes.EV_KEY, PAN_BUTTON, 1)
            self._panning = True

    def _end_pan(self, syn):
        """Release the synthetic pan button. syn=False when the source's own
        SYN_REPORT is about to flush the same packet."""
        if not self._panning:
            return
        self._ui.write(ecodes.EV_KEY, PAN_BUTTON, 0)
        self._panning = False
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
                    self._right_down = value != 0
                    # Hand the drag over to Onshape's native rotate cleanly.
                    if value:
                        self._end_pan(syn=False)
                elif value:
                    # Any other button starts a real click; don't leave a pan running
                    # underneath it.
                    self._end_pan(syn=False)
                self._track(code, value)
                self._ui.write_event(event)
                return

            if etype == ecodes.EV_REL and code in MOTION_AXES:
                if not self._right_down:
                    self._start_pan()
                    self._last_motion = time.monotonic()
                self._ui.write_event(event)
                return

            # Wheel, hi-res wheel, MSC_SCAN, SYN_REPORT: verbatim.
            self._ui.write_event(event)

    def note_while_closed(self, event):
        """Keep physical button state honest even while we're dropping events, so the
        gate reopening mid-hold doesn't leave us confused about the right button."""
        if event.type == ecodes.EV_KEY and event.code == ecodes.BTN_RIGHT:
            with self._lock:
                self._right_down = event.value != 0

    def tick(self):
        with self._lock:
            if self._panning and time.monotonic() - self._last_motion > PAN_IDLE_RELEASE:
                self._end_pan(syn=True)

    def release_all(self):
        """Gate closed. Lift anything we left down, real or synthetic."""
        with self._lock:
            if not self._panning and not self._held:
                return
            self._end_pan(syn=False)
            for code in self._held:
                self._ui.write(ecodes.EV_KEY, code, 0)
            self._held.clear()
            self._right_down = False
            self._ui.syn()
            log("gate closed mid-gesture; released held buttons")

    def snapshot(self):
        with self._lock:
            return {"panning": self._panning, "right_button_down": self._right_down}


class Gate:
    def __init__(self):
        self._lock = threading.Lock()
        self._chrome_focused = False
        self._onshape = False
        self._last_push = 0.0
        self._open = False
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

    def set_chrome_focused(self, focused):
        with self._lock:
            self._chrome_focused = focused
        self._recompute()

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
            }
            translator = self.translator
        state["device"] = DEVICE_PATH
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


def serve():
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


def pan_timer():
    while True:
        time.sleep(PAN_TICK)
        translator = GATE.translator
        if translator is not None:
            translator.tick()


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


def focus_from(line):
    wid = parse_active(line)
    return bool(wid) and wid != "0x0" and window_is_chrome(wid)


def watch_focus():
    """Follow _NET_ACTIVE_WINDOW. `xprop -spy` streams a line per change, so this is
    event-driven rather than polled."""
    while True:
        try:
            first = subprocess.run(
                ["xprop", "-root", "_NET_ACTIVE_WINDOW"],
                capture_output=True, text=True, timeout=2,
            ).stdout
            GATE.set_chrome_focused(focus_from(first))

            proc = subprocess.Popen(
                ["xprop", "-root", "-spy", "_NET_ACTIVE_WINDOW"],
                stdout=subprocess.PIPE, text=True,
            )
            for line in proc.stdout:
                GATE.set_chrome_focused(focus_from(line))
        except Exception as exc:
            log(f"focus watcher restarting after error: {exc}")
        GATE.set_chrome_focused(False)
        time.sleep(2)


DEVICE_PATH = None


def resolve_device():
    """Command line wins, then the config file written by setup.sh."""
    if len(sys.argv) > 1:
        return sys.argv[1]
    try:
        with open(CONFIG_PATH) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    except FileNotFoundError:
        pass
    raise SystemExit(
        f"No mouse configured. Run setup.sh to choose one, or pass a device path.\n"
        f"Expected a /dev/input/by-id/... path in {CONFIG_PATH}"
    )


def wait_for_device(path):
    warned = False
    while not os.path.exists(path):
        if not warned:
            log(f"waiting for {path} to appear (mouse unplugged?)")
            warned = True
        time.sleep(1)
    return evdev.InputDevice(path)


def main():
    global DEVICE_PATH
    DEVICE_PATH = resolve_device()

    threading.Thread(target=serve, daemon=True).start()
    threading.Thread(target=watch_focus, daemon=True).start()
    threading.Thread(target=pan_timer, daemon=True).start()
    log(f"listening on 127.0.0.1:{PORT}, gating {DEVICE_PATH}")

    while True:
        dev = wait_for_device(DEVICE_PATH)
        ui = None
        try:
            ui = UInput.from_device(dev, name=VIRTUAL_NAME)
            translator = Translator(ui)
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
            if ui is not None:
                try:
                    ui.close()
                except Exception:
                    pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
