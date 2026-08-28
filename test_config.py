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
      ns["resolve_pan_idle"](with_config(None)), 0.1)

check("missing key falls back to the default",
      ns["resolve_pan_idle"](with_config("device = /dev/x\n")), 0.1)

check("a plain value is honoured",
      ns["resolve_pan_idle"](with_config("pan_idle_release_ms = 250\n")), 0.25)

check("junk falls back to the default",
      ns["resolve_pan_idle"](with_config("pan_idle_release_ms = banana\n")), 0.1)

check("below the floor is clamped up",
      ns["resolve_pan_idle"](with_config("pan_idle_release_ms = 5\n")), 0.02)

check("above the ceiling is clamped down",
      ns["resolve_pan_idle"](with_config("pan_idle_release_ms = 9999\n")), 2.0)

check("whitespace and comments are ignored",
      ns["resolve_pan_idle"](with_config(
          "# a comment\n\n   pan_idle_release_ms   =   150   \n")), 0.15)

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
