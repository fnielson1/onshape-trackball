"""ctypes binding for the Interception kernel filter driver.

Only the calls this project needs. A wrapper rather than a dependency for the same
reason `Pointer` used raw ctypes over python-xlib on Linux: the surface is a dozen
functions, and vendoring it keeps the install to "put interceptor.dll somewhere".

The driver is what makes a Windows exclusive grab possible at all. It sits above the
mouse class driver, so a stroke it hands us has not reached any application — if we
do not send it on, it never happened. That is what `EVIOCGRAB` buys on Linux, and
nothing in user space (Raw Input, low-level hooks) is equivalent.

Nothing here touches the driver at import; a missing DLL surfaces when a context is
created, so `gate.py` still execs on a machine without it.
"""

import ctypes
import os
import sys

MAX_KEYBOARD = 10
MAX_MOUSE = 10
MAX_DEVICE = MAX_KEYBOARD + MAX_MOUSE

# Devices are numbered 1..10 for keyboards and 11..20 for mice. The numbering is
# positional and is reassigned across reboots and re-plugs, which is exactly why the
# config stores a hardware ID and resolves it here instead.
FIRST_MOUSE = MAX_KEYBOARD + 1

# --- mouse stroke state flags ------------------------------------------------------
MOUSE_LEFT_BUTTON_DOWN = 0x001
MOUSE_LEFT_BUTTON_UP = 0x002
MOUSE_RIGHT_BUTTON_DOWN = 0x004
MOUSE_RIGHT_BUTTON_UP = 0x008
MOUSE_MIDDLE_BUTTON_DOWN = 0x010
MOUSE_MIDDLE_BUTTON_UP = 0x020
MOUSE_BUTTON_4_DOWN = 0x040
MOUSE_BUTTON_4_UP = 0x080
MOUSE_BUTTON_5_DOWN = 0x100
MOUSE_BUTTON_5_UP = 0x200
MOUSE_WHEEL = 0x400
MOUSE_HWHEEL = 0x800

# --- mouse stroke movement flags ---------------------------------------------------
MOUSE_MOVE_RELATIVE = 0x000
MOUSE_MOVE_ABSOLUTE = 0x001
MOUSE_VIRTUAL_DESKTOP = 0x002
MOUSE_ATTRIBUTES_CHANGED = 0x004
MOUSE_MOVE_NOCOALESCE = 0x008

# --- filters -----------------------------------------------------------------------
FILTER_MOUSE_NONE = 0x0000
FILTER_MOUSE_ALL = 0xFFFF
FILTER_KEY_NONE = 0x0000
FILTER_KEY_ALL = 0xFFFF

# One wheel click. Windows counts wheel movement in these; Linux REL_WHEEL counts
# whole clicks, and REL_WHEEL_HI_RES counts in exactly this unit.
WHEEL_DELTA = 120


class MouseStroke(ctypes.Structure):
    _fields_ = [
        ("state", ctypes.c_ushort),
        ("flags", ctypes.c_ushort),
        ("rolling", ctypes.c_short),
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("information", ctypes.c_uint),
    ]


class KeyStroke(ctypes.Structure):
    _fields_ = [
        ("state", ctypes.c_ushort),
        ("code", ctypes.c_ushort),
        ("information", ctypes.c_uint),
    ]


# The C API passes strokes as an opaque buffer sized for the larger of the two.
STROKE_SIZE = ctypes.sizeof(MouseStroke)
Stroke = ctypes.c_char * STROKE_SIZE

PREDICATE = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int)


class InterceptionError(RuntimeError):
    pass


def _candidate_paths():
    """Where interceptor.dll might be, most specific first."""
    here = os.path.dirname(os.path.abspath(__file__))
    yield os.path.join(here, "interceptor.dll")
    yield "interceptor.dll"          # PATH / system32


_lib = None


def library():
    """Load interceptor.dll, cached. Raises InterceptionError if it is not there."""
    global _lib
    if _lib is not None:
        return _lib

    tried = []
    for path in _candidate_paths():
        try:
            lib = ctypes.WinDLL(path)
        except OSError:
            tried.append(path)
            continue
        _bind(lib)
        _lib = lib
        return _lib

    raise InterceptionError(
        "interceptor.dll not found. Looked in:\n  " + "\n  ".join(tried)
        + "\nInstall the Interception driver and place the DLL beside gate.py.")


def _bind(lib):
    lib.interceptor_create_context.restype = ctypes.c_void_p
    lib.interceptor_create_context.argtypes = []

    lib.interceptor_destroy_context.restype = None
    lib.interceptor_destroy_context.argtypes = [ctypes.c_void_p]

    lib.interceptor_set_filter.restype = None
    lib.interceptor_set_filter.argtypes = [ctypes.c_void_p, PREDICATE,
                                            ctypes.c_ushort]

    lib.interceptor_wait.restype = ctypes.c_int
    lib.interceptor_wait.argtypes = [ctypes.c_void_p]

    lib.interceptor_wait_with_timeout.restype = ctypes.c_int
    lib.interceptor_wait_with_timeout.argtypes = [ctypes.c_void_p, ctypes.c_ulong]

    lib.interceptor_send.restype = ctypes.c_int
    lib.interceptor_send.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                      ctypes.c_void_p, ctypes.c_uint]

    lib.interceptor_receive.restype = ctypes.c_int
    lib.interceptor_receive.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                         ctypes.c_void_p, ctypes.c_uint]

    lib.interceptor_get_hardware_id.restype = ctypes.c_uint
    lib.interceptor_get_hardware_id.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                                 ctypes.c_void_p, ctypes.c_uint]

    lib.interceptor_is_mouse.restype = ctypes.c_int
    lib.interceptor_is_mouse.argtypes = [ctypes.c_int]

    lib.interceptor_is_keyboard.restype = ctypes.c_int
    lib.interceptor_is_keyboard.argtypes = [ctypes.c_int]


class Context:
    """An open handle on the driver.

    The predicate passed to set_filter is held as an attribute deliberately: ctypes
    does not keep the trampoline alive, and letting it be collected while the driver
    still holds the pointer is a crash rather than an error.
    """

    def __init__(self):
        lib = library()
        self._lib = lib
        handle = lib.interceptor_create_context()
        if not handle:
            raise InterceptionError(
                "the Interception driver is installed but not accepting a context. "
                "This is what it looks like before the reboot that activates it.")
        self._ctx = ctypes.c_void_p(handle)
        self._predicate = None

    # --- lifecycle ----------------------------------------------------------------

    def close(self):
        if self._ctx is not None:
            self._lib.interceptor_destroy_context(self._ctx)
            self._ctx = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- devices ------------------------------------------------------------------

    def hardware_id(self, device):
        """The device's HID hardware ID, or '' if the driver will not say.

        This is what the config stores: unlike the device number it survives a
        reboot and a re-plug.
        """
        size = 1024
        buf = ctypes.create_unicode_buffer(size)
        written = self._lib.interceptor_get_hardware_id(
            self._ctx, device, buf, size * ctypes.sizeof(ctypes.c_wchar))
        if not written or written > size * ctypes.sizeof(ctypes.c_wchar):
            return ""
        return buf.value

    def mice(self):
        """[(device_number, hardware_id)] for every mouse the driver can see.

        A device that reports no hardware ID is skipped rather than offered: it
        cannot be written to the config, so it could never be selected again.
        """
        found = []
        for device in range(FIRST_MOUSE, MAX_DEVICE + 1):
            hwid = self.hardware_id(device)
            if hwid:
                found.append((device, hwid))
        return found

    def keyboards(self):
        found = []
        for device in range(1, MAX_KEYBOARD + 1):
            hwid = self.hardware_id(device)
            if hwid:
                found.append((device, hwid))
        return found

    def devices_for_hardware_id(self, hardware_id):
        """Every mouse matching a hardware ID, lowest device number first.

        Two identical mice genuinely share an ID and cannot be told apart; callers
        decide what to do about the collision rather than having it hidden here.
        """
        return [device for device, hwid in self.mice() if hwid == hardware_id]

    # --- filtering ----------------------------------------------------------------

    def filter_device(self, device, filter_bits=FILTER_MOUSE_ALL):
        """Route one device's strokes to us and nobody else."""
        def predicate(candidate):
            return 1 if candidate == device else 0

        self._predicate = PREDICATE(predicate)
        self._lib.interceptor_set_filter(self._ctx, self._predicate, filter_bits)

    def filter_devices(self, devices, filter_bits=FILTER_MOUSE_ALL):
        """Route several devices to us at once — what detection needs, since it has
        to watch every mouse to see which one moves."""
        wanted = set(devices)

        def predicate(candidate):
            return 1 if candidate in wanted else 0

        self._predicate = PREDICATE(predicate)
        self._lib.interceptor_set_filter(self._ctx, self._predicate, filter_bits)

    def clear_filter(self):
        """Stop filtering. The device goes back to behaving normally immediately."""
        def predicate(_candidate):
            return 0

        self._predicate = PREDICATE(predicate)
        self._lib.interceptor_set_filter(self._ctx, self._predicate,
                                          FILTER_MOUSE_NONE)

    # --- i/o -----------------------------------------------------------------------

    def wait(self, timeout_ms=None):
        """Device number with a stroke ready, or 0 on timeout."""
        if timeout_ms is None:
            return self._lib.interceptor_wait(self._ctx)
        return self._lib.interceptor_wait_with_timeout(self._ctx, timeout_ms)

    def receive_mouse(self, device):
        """One mouse stroke, or None."""
        buf = Stroke()
        got = self._lib.interceptor_receive(self._ctx, device,
                                             ctypes.byref(buf), 1)
        if got <= 0:
            return None
        return MouseStroke.from_buffer_copy(bytes(buf)[:ctypes.sizeof(MouseStroke)])

    def send_mouse(self, device, stroke):
        buf = Stroke()
        ctypes.memmove(buf, ctypes.byref(stroke), ctypes.sizeof(MouseStroke))
        return self._lib.interceptor_send(self._ctx, device, ctypes.byref(buf), 1)

    def send_key(self, device, stroke):
        buf = Stroke()
        ctypes.memmove(buf, ctypes.byref(stroke), ctypes.sizeof(KeyStroke))
        return self._lib.interceptor_send(self._ctx, device, ctypes.byref(buf), 1)


# Device class GUIDs, and the filter driver Interception prepends to each. It
# installs itself as an upper filter on the mouse and keyboard classes; that
# registry entry is the authoritative "is it installed", and it survives the gap
# between installing and rebooting, which nothing else does.
_CLASS_FILTERS = (
    ("mouse", r"SYSTEM\CurrentControlSet\Control\Class"
              r"\{4D36E96F-E325-11CE-BFC1-08002BE10318}"),
    ("keyboard", r"SYSTEM\CurrentControlSet\Control\Class"
                 r"\{4D36E96B-E325-11CE-BFC1-08002BE10318}"),
)


def filters_registered():
    """Has install-interception.exe run? Reads the registry; needs no admin.

    Creating a context cannot answer this: it fails identically whether the driver
    is installed and waiting for a reboot or was never installed at all. Telling
    someone to reboot when they have not run the installer wastes a reboot and
    leaves them exactly where they started.
    """
    try:
        import winreg
    except ImportError:
        return False

    for driver, path in _CLASS_FILTERS:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
                filters, _ = winreg.QueryValueEx(key, "UpperFilters")
        except OSError:
            continue
        if any(str(f).lower() == driver for f in (filters or ())):
            return True
    return False


def driver_state():
    """-> 'active' | 'needs_reboot' | 'missing', plus a human explanation.

    The three are genuinely different situations with different fixes, and setup
    must not collapse them into "something went wrong".
    """
    try:
        library()
    except InterceptionError:
        # Deliberately short, and careful not to conflate two separate things: the
        # driver is installed by install-interception.exe, while interceptor.dll is
        # a file you copy yourself. Installing the driver and stopping there is the
        # easy mistake, and "is the driver installed?" would send you to check the
        # one thing that is already done.
        return ("missing",
                "interceptor.dll not found - copy it next to gate.py "
                "(this is a separate step from installing the driver)")

    try:
        ctx = Context()
    except InterceptionError:
        # Distinguished by the registry, not by this failure: creating a context
        # fails identically whether the driver is installed and waiting for a
        # reboot or was never installed at all.
        if filters_registered():
            return ("needs_reboot",
                    "the driver is installed but not yet active - reboot to "
                    "activate it, then run this again")
        return ("missing",
                "interceptor.dll is here but the driver is not installed - run "
                "install-interception.exe /install from an admin prompt, "
                "then reboot")
    try:
        if not ctx.mice():
            return ("needs_reboot",
                    "the driver is answering but reports no mice, which is what a "
                    "half-activated install looks like; reboot and try again")
        return "active", "the Interception driver is installed and answering"
    finally:
        ctx.close()
