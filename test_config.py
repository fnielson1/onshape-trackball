"""Exercise gate.py's config parsing: defaults, clamping, junk, legacy fallback."""
import os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(HERE, "gate.py")).read().replace('if __name__ == "__main__":', 'if False:')
ns = {}
exec(compile(src, "gate.py", "exec"), ns)

results = []

def check(name, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"      expected {want!r}, got {got!r}")
    results.append(ok)

def with_config(text):
    """Point the module at a temp config containing `text` (None = no file)."""
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "config")
    if text is not None:
        open(path, "w").write(text)
    ns["CONFIG_PATH"] = path
    ns["LEGACY_DEVICE_PATH"] = os.path.join(tmp, "device")
    return ns["read_config"]()

sys.argv = ["gate.py"]  # no command-line device override

# --- pan_idle_release_ms ------------------------------------------------------
check("missing file falls back to the default",
      ns["resolve_pan_idle"](with_config(None)), 0.15)

check("missing key falls back to the default",
      ns["resolve_pan_idle"](with_config("device = /dev/x\n")), 0.15)

check("a plain value is honoured",
      ns["resolve_pan_idle"](with_config("pan_idle_release_ms = 250\n")), 0.25)

check("junk falls back to the default",
      ns["resolve_pan_idle"](with_config("pan_idle_release_ms = banana\n")), 0.15)

check("below the floor is clamped up",
      ns["resolve_pan_idle"](with_config("pan_idle_release_ms = 5\n")), 0.02)

check("above the ceiling is clamped down",
      ns["resolve_pan_idle"](with_config("pan_idle_release_ms = 9999\n")), 2.0)

check("whitespace and comments are ignored",
      ns["resolve_pan_idle"](with_config(
          "# a comment\n\n   pan_idle_release_ms   =   150   \n")), 0.15)

# --- recentring ---------------------------------------------------------------
check("recentring defaults to on at 80px",
      ns["resolve_recenter"](with_config(None)), (True, 80))

check("recentring can be switched off",
      ns["resolve_recenter"](with_config("pan_recenter = false\n")), (False, 80))

check("'yes' also enables it",
      ns["resolve_recenter"](with_config("pan_recenter = yes\n")), (True, 80))

check("a custom margin is honoured",
      ns["resolve_recenter"](with_config("pan_recenter_margin_px = 220\n")), (True, 220))

check("a junk margin falls back",
      ns["resolve_recenter"](with_config("pan_recenter_margin_px = wide\n")), (True, 80))

check("an oversized margin is clamped",
      ns["resolve_recenter"](with_config("pan_recenter_margin_px = 5000\n")), (True, 600))

check("a negative margin is clamped to zero",
      ns["resolve_recenter"](with_config("pan_recenter_margin_px = -40\n")), (True, 0))

# --- minimum press interval ----------------------------------------------------
check("press interval defaults off for ctrl_right",
      ns["resolve_press_interval"](with_config(None), "ctrl_right"), 0.0)

check("press interval defaults to 501ms for middle-drag",
      ns["resolve_press_interval"](with_config(None), "middle"), 0.501)

check("a custom press interval is honoured",
      ns["resolve_press_interval"](with_config("pan_min_press_interval_ms = 300\n"),
                                   "ctrl_right"), 0.3)

check("zero disables the wait",
      ns["resolve_press_interval"](with_config("pan_min_press_interval_ms = 0\n"),
                                   "middle"), 0.0)

check("a junk press interval falls back",
      ns["resolve_press_interval"](with_config("pan_min_press_interval_ms = soon\n"),
                                   "middle"), 0.501)

check("an oversized press interval is clamped",
      ns["resolve_press_interval"](with_config("pan_min_press_interval_ms = 9999\n"),
                                   "middle"), 2.0)

# --- pan gesture ----------------------------------------------------------------
check("gesture defaults to ctrl_right",
      ns["resolve_pan_gesture"](with_config(None)), "ctrl_right")

check("middle can be selected",
      ns["resolve_pan_gesture"](with_config("pan_gesture = middle\n")), "middle")

check("an unknown gesture falls back to the default",
      ns["resolve_pan_gesture"](with_config("pan_gesture = elbow\n")), "ctrl_right")

# --- device -------------------------------------------------------------------
check("device is read from the config",
      ns["resolve_device"](with_config("device = /dev/input/by-id/usb-A-event-mouse\n")),
      "/dev/input/by-id/usb-A-event-mouse")

cfg = with_config("device =\n")             # present but empty
open(ns["LEGACY_DEVICE_PATH"], "w").write("/dev/input/by-id/usb-legacy-event-mouse\n")
check("an empty device falls back to the legacy file",
      ns["resolve_device"](cfg), "/dev/input/by-id/usb-legacy-event-mouse")

cfg = with_config(None)
try:
    ns["resolve_device"](cfg)
    check("no device anywhere raises SystemExit", "no exception", "SystemExit")
except SystemExit:
    check("no device anywhere raises SystemExit", "SystemExit", "SystemExit")

sys.argv = ["gate.py", "/dev/input/by-id/usb-cli-event-mouse"]
check("the command line overrides the config",
      ns["resolve_device"](with_config("device = /dev/input/by-id/usb-A-event-mouse\n")),
      "/dev/input/by-id/usb-cli-event-mouse")

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
