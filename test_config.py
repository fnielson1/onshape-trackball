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
check("recentring defaults to on at 35px",
      ns["resolve_recenter"](with_config(None)), (True, 35))

check("recentring can be switched off",
      ns["resolve_recenter"](with_config("pan_recenter = false\n")), (False, 35))

check("'yes' also enables it",
      ns["resolve_recenter"](with_config("pan_recenter = yes\n")), (True, 35))

check("a custom margin is honoured",
      ns["resolve_recenter"](with_config("pan_recenter_margin_px = 220\n")), (True, 220))

check("a junk margin falls back",
      ns["resolve_recenter"](with_config("pan_recenter_margin_px = wide\n")), (True, 35))

check("an oversized margin is clamped",
      ns["resolve_recenter"](with_config("pan_recenter_margin_px = 5000\n")), (True, 600))

check("a negative margin is clamped to zero",
      ns["resolve_recenter"](with_config("pan_recenter_margin_px = -40\n")), (True, 0))

# --- pan dead zone --------------------------------------------------------------
check("dead zone defaults to 10px",
      ns["resolve_deadzone"](with_config(None)), 10)

check("a custom dead zone is honoured",
      ns["resolve_deadzone"](with_config("pan_deadzone_px = 25\n")), 25)

check("zero disables the dead zone",
      ns["resolve_deadzone"](with_config("pan_deadzone_px = 0\n")), 0)

check("a junk dead zone falls back",
      ns["resolve_deadzone"](with_config("pan_deadzone_px = far\n")), 10)

check("an oversized dead zone is clamped",
      ns["resolve_deadzone"](with_config("pan_deadzone_px = 9000\n")), 500)

# --- other-mouse yield dead zone -------------------------------------------------
check("yield dead zone defaults to 20px",
      ns["resolve_yield_deadzone"](with_config(None)), 20)

check("a custom yield dead zone is honoured",
      ns["resolve_yield_deadzone"](with_config("pan_yield_deadzone_px = 8\n")), 8)

check("zero disables the yield dead zone",
      ns["resolve_yield_deadzone"](with_config("pan_yield_deadzone_px = 0\n")), 0)

check("a junk yield dead zone falls back",
      ns["resolve_yield_deadzone"](with_config("pan_yield_deadzone_px = far\n")), 20)

check("an oversized yield dead zone is clamped",
      ns["resolve_yield_deadzone"](with_config("pan_yield_deadzone_px = 9000\n")), 500)

# --- rotate scale ---------------------------------------------------------------
check("rotate scale defaults to 0.5",
      ns["resolve_rotate_scale"](with_config(None)), 0.5)

check("a custom scale is honoured",
      ns["resolve_rotate_scale"](with_config("rotate_scale = 1.2\n")), 1.2)

check("1.0 restores raw, unscaled motion",
      ns["resolve_rotate_scale"](with_config("rotate_scale = 1\n")), 1.0)

check("a junk scale falls back",
      ns["resolve_rotate_scale"](with_config("rotate_scale = fast\n")), 0.5)

check("below the floor is clamped up",
      ns["resolve_rotate_scale"](with_config("rotate_scale = 0.001\n")), 0.05)

check("above the ceiling is clamped down",
      ns["resolve_rotate_scale"](with_config("rotate_scale = 50\n")), 5.0)

# --- which gesture the right button performs -------------------------------------
check("defaults to true: hold the button to pan",
      ns["resolve_pan_button"](with_config(None)), True)

check("can be switched to the original mapping",
      ns["resolve_pan_button"](with_config("pan_requires_right_button = false\n")), False)

check("'no' also disables it",
      ns["resolve_pan_button"](with_config("pan_requires_right_button = no\n")), False)

check("an unrecognised value reads as false, same as pan_yield_to_other_mice",
      ns["resolve_pan_button"](with_config("pan_requires_right_button = sideways\n")), False)

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
