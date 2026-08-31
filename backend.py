"""Platform backend selection.

`gate.py` holds everything that is the same everywhere — the translator state
machine, the gate, config parsing, the status server — and reaches through this
module for the four things that are not: exclusive device capture, synthetic
output, cursor position, and focus tracking.

The interface a backend must provide:

    ecodes_source()             -> the module whose constants the backend speaks
    enumerate_mice()            -> [(identifier, display_name), ...]
    detect_mouse(timeout)       -> identifier or None
    open_gated_device(ident)    -> GatedDevice (context manager, see below)
    VirtualOutput(template)     -> .write(type, code, value) / .syn()
                                   .write_event(event) / .close()
    modifier_output()           -> VirtualOutput for the Ctrl/space key device,
                                   or None if one cannot be made
    Pointer()                   -> .ok, .position() -> (x, y)|None, .warp(x, y)
    declare_dpi_aware()         -> str, describing the coordinate space adopted
    watch_focus(callback)       -> never returns; calls
                                   callback(focused, geometry, window_id)
    window_geometry(window_id)  -> (x, y, w, h) or None
    watch_other_pointers(ident, on_activity, enabled)
                                -> never returns; calls
                                   on_activity(dx, dy, immediate) when a non-gated
                                   pointing device stirs. dx/dy are its motion, for
                                   the translator's own dead zone; immediate is True
                                   for a button or wheel signal, which always bypass
                                   it

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
VirtualOutput = _impl.VirtualOutput
modifier_output = _impl.modifier_output
Pointer = _impl.Pointer
declare_dpi_aware = _impl.declare_dpi_aware
watch_focus = _impl.watch_focus
window_geometry = _impl.window_geometry
watch_other_pointers = _impl.watch_other_pointers

# Where the config lives, and how to restart the service — both differ per platform
# and both appear in user-facing text, so the backend owns the strings.
config_dir = _impl.config_dir
RESTART_HINT = _impl.RESTART_HINT
DEVICE_HINT = _impl.DEVICE_HINT
