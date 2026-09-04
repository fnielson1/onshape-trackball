"""Platform backend selection.

`gate.py` holds everything that is the same everywhere — the translator state
machine, the channel, the gate, config parsing, the status server — and reaches
through this module for what is not: exclusive device capture and focus tracking.
Translated output is no longer one of these — it goes out over the channel to the
extension identically on both platforms, so a backend has nothing to do with it.

The interface a backend must provide:

    ecodes_source()             -> the module whose constants the backend speaks
    enumerate_mice()            -> [(identifier, display_name), ...]
    detect_mouse(timeout)       -> identifier or None
    open_gated_device(ident)    -> GatedDevice (context manager, see below)
    declare_dpi_aware()         -> str, describing the coordinate space adopted
    watch_focus(callback)       -> never returns; calls
                                   callback(focused, geometry, window_id)
    window_geometry(window_id)  -> (x, y, w, h) or None

A GatedDevice exposes `.name`, `.events()` yielding objects with
`.type`/`.code`/`.value`, and `.close()`. Events carry the values in `codes.py`,
so `Translator.handle` is identical on both platforms.

Importing a backend must never touch hardware, a driver or a display: both test
suites exec `gate.py`, which imports this module, on machines with neither.
"""

import sys

if sys.platform == "win32":
    import backend_windows as _impl
elif sys.platform.startswith("linux"):
    import backend_linux as _impl
else:
    raise ImportError(
        f"no input backend for {sys.platform!r}; supported: linux, win32")

name = _impl.NAME

enumerate_mice = _impl.enumerate_mice
detect_mouse = _impl.detect_mouse
open_gated_device = _impl.open_gated_device
declare_dpi_aware = _impl.declare_dpi_aware
watch_focus = _impl.watch_focus
window_geometry = _impl.window_geometry

# Where the config lives, and how to restart the service — both differ per platform
# and both appear in user-facing text, so the backend owns the strings.
config_dir = _impl.config_dir
RESTART_HINT = _impl.RESTART_HINT
DEVICE_HINT = _impl.DEVICE_HINT
