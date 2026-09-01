#!/usr/bin/env python3
"""The parts of setup.cmd that batch has no business doing.

Batch keeps the control flow, the prompting and the status board, because that is
what a user reading `setup.cmd` wants to find in it. JSON, HTTP, the registry and
device enumeration come here instead.

Every subcommand prints one thing and exits with a code the script can branch on:
0 success, 1 a definite negative, 2 a usage error. Anything unexpected goes to
stderr so a stray traceback cannot be mistaken for a value.

    setup_helper.py driver-state
    setup_helper.py list-mice
    setup_helper.py detect-mouse [SECONDS]
    setup_helper.py device-name HWID
    setup_helper.py config-path
    setup_helper.py ensure-config [HWID]
    setup_helper.py config-get KEY
    setup_helper.py config-set KEY VALUE
    setup_helper.py status [FIELD]
    setup_helper.py wait-daemon [SECONDS]
    setup_helper.py drift
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 47653
STATUS_URL = f"http://127.0.0.1:{PORT}/status"

OK, NO, USAGE = 0, 1, 2


def _gate_namespace():
    """gate.py's own parsing, exec'd the way the test suites do it.

    Reimplementing the resolvers here is how the installer and the daemon would
    drift into disagreeing about what the config means, which is exactly the bug
    `drift` exists to catch.
    """
    src = open(os.path.join(HERE, "gate.py")).read()
    src = src.replace('if __name__ == "__main__":', "if False:")
    ns = {}
    exec(compile(src, "gate.py", "exec"), ns)
    return ns


# ------------------------------------------------------------------ config

CONFIG_KEYS = ("device", "left_click_key", "pan_requires_right_button",
               "pan_deadzone_px", "pan_idle_release_ms",
               "pan_recenter", "pan_recenter_margin_px", "pan_yield_to_other_mice",
               "pan_yield_deadzone_px", "rotate_scale", "min_drag_px")

DEFAULT_PAN_IDLE_MS = 150
DEFAULT_RECENTER_MARGIN = 35
DEFAULT_DEADZONE_PX = 10
DEFAULT_YIELD_DEADZONE_PX = 20
DEFAULT_ROTATE_SCALE = 0.5
DEFAULT_MIN_DRAG_PX = 6

RESTART = 'schtasks /Run /TN "Onshape trackball gate"'


def config_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "onshape-trackball")


def config_path():
    return os.path.join(config_dir(), "config")


def _block(key, device=""):
    """One documented setting. Same prose as setup.sh, so the two files agree."""
    if key == "device":
        return (
            "# Which mouse is gated. Set by setup.cmd; change it with:  "
            "setup.cmd --reconfigure\n"
            "#\n"
            "# This is the device's hardware ID, not its slot number: Interception\n"
            "# renumbers devices across reboots, and a number would silently start\n"
            "# gating a different mouse.\n"
            f"device = {device}\n")

    if key == "left_click_key":
        return (
            "\n# What the gated mouse's left button does.\n"
            "#\n"
            "# Because the cursor is penned in the middle of the view, a real left "
            "click would\n"
            "# just select whatever geometry happens to be under it — rarely what you "
            "want from a\n"
            "# navigation mouse. Onshape clears the whole selection on space, so the "
            "button taps\n"
            "# that instead, and the click itself is swallowed.\n"
            "#\n"
            "#   space   clear the selection (Onshape's own shortcut)\n"
            "#   esc     send Escape instead\n"
            "#   none    pass the click straight through, as an ordinary left click\n"
            "left_click_key = space\n")

    if key == "pan_requires_right_button":
        return (
            "\n# Which of pan and rotate the gated mouse's right button performs.\n"
            "#\n"
            "# One of the two is bracketed by the right button's own press and "
            "release; the\n"
            "# other is driven by bare motion instead, started once it clears "
            "pan_deadzone_px\n"
            "# and ended by pan_idle_release_ms after it stops, since bare motion has "
            "no\n"
            "# button of its own to mark either.\n"
            "#\n"
            "#   true    hold the right button to pan, move without it to rotate\n"
            "#   false   move to pan, hold the right button to rotate — the original "
            "mapping\n"
            "pan_requires_right_button = true\n")

    if key == "pan_idle_release_ms":
        return (
            "\n# How long a pan stroke stays live after you stop moving, in "
            "milliseconds.\n"
            "#\n"
            "# A mouse never says \"I stopped\", so this timeout is what ends a "
            "stroke: the\n"
            "# pan button is released this long after the last motion. Without it the "
            "button\n"
            "# would stay down for ever.\n"
            "#\n"
            "#   lower   strokes end sooner; brief pauses split one pan into several\n"
            "#   higher  the button stays held longer after you stop moving\n"
            "#\n"
            "# Accepted range is 20-2000; anything outside is clamped.\n"
            f"pan_idle_release_ms = {DEFAULT_PAN_IDLE_MS}\n")

    if key == "pan_deadzone_px":
        return (
            "\n# How far the gated mouse must travel before a pan actually starts.\n"
            "#\n"
            "# Measured as net displacement, not distance travelled, so jitter that "
            "wanders out\n"
            "# and back never trips it — only a deliberate push does. Each stroke "
            "earns its own\n"
            "# dead zone, and a nudge that goes nowhere expires rather than banking "
            "toward the\n"
            "# next one.\n"
            "#\n"
            "# Panning only. Rotating, zooming and the left button are unaffected, and "
            "the cursor\n"
            "# keeps tracking your hand throughout — it just is not panning yet.\n"
            "#\n"
            "# Accepted range 0-500; 0 starts panning on the first movement.\n"
            f"pan_deadzone_px = {DEFAULT_DEADZONE_PX}\n")

    if key in ("pan_recenter", "pan_recenter_margin_px"):
        if key == "pan_recenter_margin_px":
            return ""       # written together with pan_recenter
        return (
            "\n# Panning drags the real cursor, so a long sweep runs out of screen and "
            "the pan\n"
            "# dies. With recentring on, the cursor is warped back to the middle of "
            "the view\n"
            "# whenever it comes within pan_recenter_margin_px of an edge, making a "
            "pan\n"
            "# effectively unlimited. The pan button is briefly lifted around the warp "
            "so the\n"
            "# jump is not read as one huge pan.\n"
            "#\n"
            "# The edge is the usable 3D view's edge, not the Chrome window's. The "
            "extension\n"
            "# probes the page to find the region that genuinely belongs to the view — "
            "the canvas\n"
            "# minus the controls Onshape stacks on top of it — and the daemon pens "
            "the cursor\n"
            "# inside that. If the extension stops reporting, it falls back to the "
            "whole window\n"
            "# after a few seconds.\n"
            "#\n"
            "# Set pan_recenter to false to get the old behaviour: pan until you hit "
            "the edge,\n"
            "# then lift and reposition.\n"
            "pan_recenter = true\n"
            f"pan_recenter_margin_px = {DEFAULT_RECENTER_MARGIN}\n")

    if key == "pan_yield_to_other_mice":
        return (
            "\n# Both mice drive one shared cursor, so while a pan stroke is live the "
            "held\n"
            "# pan button applies to whatever your other mouse does too: its motion "
            "pans, and\n"
            "# its wheel arrives as wheel-with-button-held instead of a clean scroll.\n"
            "#\n"
            "# With this on, the other mice are watched read-only (never captured, so "
            "they keep\n"
            "# working normally) and any activity on one drops the pan stroke "
            "immediately.\n"
            "# Panning resumes shortly after they go quiet.\n"
            "#\n"
            "# Turn it off if resting your hand on the other mouse interrupts panning "
            "too eagerly.\n"
            "pan_yield_to_other_mice = true\n")

    if key == "pan_yield_deadzone_px":
        return (
            "\n# How far the *other* mouse must travel before its motion counts as "
            "deliberate\n"
            "# and drops the pan or rotate the gated mouse is holding.\n"
            "#\n"
            "# Net displacement, measured the same way as pan_deadzone_px. Below "
            "this, resting\n"
            "# a hand on the other mouse or bumping it in passing does not interrupt "
            "the stroke.\n"
            "# A button press or a wheel turn on it always interrupts immediately, "
            "regardless of\n"
            "# this setting — neither happens by accident.\n"
            "#\n"
            "# Accepted range 0-500; 0 yields on the very first movement, which was "
            "the only\n"
            "# behaviour before this setting existed.\n"
            f"pan_yield_deadzone_px = {DEFAULT_YIELD_DEADZONE_PX}\n")

    if key == "rotate_scale":
        return (
            "\n# Multiplier applied to rotate's motion.\n"
            "#\n"
            "# Rotate is plain motion passed straight through to Onshape's orbit "
            "gesture,\n"
            "# with nothing between the physical mouse and the browser to scale it "
            "down —\n"
            "# a trackball's raw counts become orbit degrees 1:1, which reads as far "
            "more\n"
            "# sensitive than an ordinary pointing device, where the same counts only "
            "move a\n"
            "# cursor.\n"
            "#\n"
            "#   below 1   a given push rotates the model less\n"
            "#   1         raw, unscaled motion — how this behaved before this "
            "setting existed\n"
            "#   above 1   a given push rotates the model more\n"
            "#\n"
            "# Panning is unaffected — it already has its own feel via "
            "pan_deadzone_px and\n"
            "# pan_recenter_margin_px.\n"
            "#\n"
            "# Accepted range 0.05-5.0; anything outside is clamped.\n"
            f"rotate_scale = {DEFAULT_ROTATE_SCALE}\n")

    if key == "min_drag_px":
        return (
            "\n# A press and release with nothing in between is a click, and Ctrl + "
            "right-click\n"
            "# opens Chrome's context menu mid-pan. Every release is preceded by at "
            "least this\n"
            "# much real cursor displacement, so it always reads as a drag instead — "
            "topped up\n"
            "# with a synthetic nudge if the real motion fell short. That nudge pans "
            "or\n"
            "# rotates the model too, which is why it is kept small.\n"
            "#\n"
            "#   lower   less forced motion on a short release, but less margin "
            "against a\n"
            "#           stray context menu\n"
            "#   higher  more margin against a context menu, at the cost of a bigger "
            "nudge on\n"
            "#           a release that fell well short\n"
            "#\n"
            "# Accepted range 0-200; anything outside is clamped.\n"
            f"min_drag_px = {DEFAULT_MIN_DRAG_PX}\n")

    return ""


def read_config():
    values = {}
    try:
        with open(config_path()) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    except FileNotFoundError:
        pass
    return values


def ensure_config(device=""):
    """Create the config, or append settings a older one predates. -> what happened."""
    path = config_path()
    os.makedirs(config_dir(), exist_ok=True)

    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# Onshape trackball gate — configuration.\n")
            fh.write("#\n")
            fh.write("# Apply changes with:\n")
            fh.write(f"#   {RESTART}\n")
            for key in CONFIG_KEYS:
                fh.write(_block(key, device))
        return "created"

    existing = read_config()
    missing = [k for k in CONFIG_KEYS if k not in existing]
    if not missing:
        return "current"
    with open(path, "a", encoding="utf-8") as fh:
        for key in missing:
            fh.write(_block(key, device))
    return "extended:" + ",".join(missing)


def config_set(key, value):
    """Replace a key in place, preserving its comment block."""
    path = config_path()
    os.makedirs(config_dir(), exist_ok=True)
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.split("=", 1)[0].strip() == key:
            lines[i] = f"{key} = {value}\n"
            break
    else:
        lines.append(f"{key} = {value}\n")

    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(lines)


# ------------------------------------------------------------------ daemon status


def fetch_status(timeout=1.0):
    try:
        with urllib.request.urlopen(STATUS_URL, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def cmd_status(argv):
    state = fetch_status()
    if state is None:
        return NO
    if not argv:
        print(json.dumps(state, indent=2))
        return OK
    value = state.get(argv[0])
    if value is None and argv[0] not in state:
        return NO
    print("" if value is None else value)
    return OK


def cmd_wait_daemon(argv):
    deadline = time.monotonic() + (float(argv[0]) if argv else 15.0)
    while time.monotonic() < deadline:
        if fetch_status(timeout=0.5) is not None:
            return OK
        time.sleep(0.4)
    return NO


def cmd_drift():
    """Do the daemon's live settings still match the config file?

    Compared through gate.py's own resolvers, so "the file says 250 but the daemon
    clamped it to 200" is not reported as drift.
    """
    state = fetch_status()
    if state is None:
        return NO

    ns = _gate_namespace()
    ns["CONFIG_PATH"] = config_path()
    config = ns["read_config"]()

    _recenter, margin = ns["resolve_recenter"](config)
    wanted = {
        "pan_idle_release_ms": round(ns["resolve_pan_idle"](config) * 1000),
        "pan_recenter_margin_px": margin,
        "pan_yield_to_other_mice": ns["resolve_yield"](config),
        "pan_deadzone_px": ns["resolve_deadzone"](config),
        "pan_yield_deadzone_px": ns["resolve_yield_deadzone"](config),
        "pan_requires_right_button": ns["resolve_pan_button"](config),
        "left_click_key": ns["resolve_left_click"](config)[0],
        "rotate_scale": ns["resolve_rotate_scale"](config),
        "min_drag_px": ns["resolve_min_drag_px"](config),
        "device": config.get("device", ""),
    }

    # Config is not the only thing that goes stale. Editing gate.py leaves the daemon
    # running code that no longer exists on disk, and every value above still matches
    # because none of them changed -- so the fingerprint of the source is compared
    # too. The daemon reports the hash it started from; this is the file as it stands.
    #
    # Skipped when either side is unknown: a daemon predating this field reports
    # nothing, and an unreadable gate.py hashes to None. Neither is drift worth
    # restarting for, and guessing would make the check cry wolf forever.
    running = state.get("code")
    on_disk = ns["source_hash"](os.path.join(HERE, "gate.py"))
    if running and on_disk:
        wanted["code"] = on_disk

    # pan_recenter is deliberately not compared: the daemon reports it as "enabled in
    # the config AND the cursor API answered", so a machine where recentring is off
    # for lack of a cursor would look like permanent, unfixable drift. The margin is
    # compared, which is what catches an actual edit to this setting.
    differing = [k for k, v in wanted.items() if state.get(k) != v]
    if differing:
        print(",".join(sorted(differing)))
        return NO
    return OK


# ------------------------------------------------------------------ devices


def cmd_driver_state():
    try:
        import interceptor
    except Exception as exc:                       # pragma: no cover - import guard
        print(f"missing|cannot load the binding: {exc}")
        return NO
    state, message = interceptor.driver_state()
    # Exactly one line, always. Batch reads this with `for /f`, which iterates
    # line by line — a multi-line message means the last line silently overwrites
    # the state, and the "where I looked" detail from a missing DLL is several
    # lines long. setup.cmd prints its own install guidance anyway.
    print(f"{state}|{' '.join(str(message).split())}")
    return OK if state == "active" else NO


def cmd_list_mice():
    import backend_windows
    try:
        mice = backend_windows.enumerate_mice()
    except Exception as exc:
        print(f"cannot enumerate mice: {exc}", file=sys.stderr)
        return NO
    if not mice:
        return NO
    for index, (hwid, label) in enumerate(mice, 1):
        print(f"{index}|{hwid}|{label}")
    return OK


def cmd_detect_mouse(argv):
    import backend_windows
    timeout = float(argv[0]) if argv else 10.0
    try:
        hwid = backend_windows.detect_mouse(timeout)
    except Exception as exc:
        print(f"detection failed: {exc}", file=sys.stderr)
        return NO
    if not hwid:
        return NO
    print(hwid)
    return OK


def cmd_device_name(argv):
    if not argv:
        return USAGE
    import backend_windows
    try:
        print(backend_windows._label_for(argv[0]))
    except Exception:
        print(argv[0])
    return OK


def cmd_device_present(argv):
    """Is the configured mouse attached right now?"""
    if not argv:
        return USAGE
    try:
        import interceptor
        with interceptor.Context() as ctx:
            return OK if ctx.devices_for_hardware_id(argv[0]) else NO
    except Exception:
        return NO


# ------------------------------------------------------------------ entry


def main(argv):
    if not argv:
        print(__doc__.strip(), file=sys.stderr)
        return USAGE

    command, rest = argv[0], argv[1:]

    if command == "driver-state":
        return cmd_driver_state()
    if command == "list-mice":
        return cmd_list_mice()
    if command == "detect-mouse":
        return cmd_detect_mouse(rest)
    if command == "device-name":
        return cmd_device_name(rest)
    if command == "device-present":
        return cmd_device_present(rest)
    if command == "config-path":
        print(config_path())
        return OK
    if command == "config-dir":
        print(config_dir())
        return OK
    if command == "ensure-config":
        print(ensure_config(rest[0] if rest else ""))
        return OK
    if command == "config-get":
        if not rest:
            return USAGE
        value = read_config().get(rest[0])
        if not value:
            return NO
        print(value)
        return OK
    if command == "config-set":
        if len(rest) < 2:
            return USAGE
        config_set(rest[0], " ".join(rest[1:]))
        return OK
    if command == "status":
        return cmd_status(rest)
    if command == "wait-daemon":
        return cmd_wait_daemon(rest)
    if command == "drift":
        return cmd_drift()

    print(f"unknown subcommand: {command}", file=sys.stderr)
    return USAGE


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
