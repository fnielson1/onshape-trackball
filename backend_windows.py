"""Windows backend: Interception for exclusive capture, Win32 for focus.

The shapes here mirror `backend_linux` one for one, so `gate.py` cannot tell them
apart:

    exclusive grab   EVIOCGRAB          -> interceptor_set_filter on one device
    focus            xprop -root -spy   -> SetWinEventHook(EVENT_SYSTEM_FOREGROUND)

Translated output is not one of these any more: every gesture the translator
decides on goes out over gate.py's own WebSocket channel to the extension,
identically on both platforms, so there is nothing platform-specific left to
implement here for it. Only the gated device is ever touched — every other mouse
is left completely alone, untouched by anything in this file.
"""

import ctypes
import ctypes.wintypes as wt
import os
import time
import winreg

import codes
import interceptor

# The two hardware-ID shapes this project's DLL has produced over time name the
# same device differently, and not merely in punctuation:
#
#     driver-backed DLL   HID\VID_04CA&PID_0061&REV_0100
#     current DLL         \\?\HID#VID_04CA&PID_0061#9&299ea37&0&0000#{378de44c-...}
#
# Used below to key the registry's friendly-name lookup (_friendly_names) by the
# one thing every shape agrees on. interceptor.py owns the regex, so there is one
# definition instead of two drifting copies.
_vid_pid = interceptor._vid_pid

NAME = "windows"

RESTART_HINT = 'schtasks /Run /TN "Onshape trackball gate"'
DEVICE_HINT = "device = HID\\VID_046D&PID_C52B&..."

MOTION_THRESHOLD = 30  # accumulated |delta| units, matching the Linux picker

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _bind_win32():
    """Declare the calls that return or take handles.

    ctypes defaults a return type to c_int, which silently truncates a 64-bit
    handle to 32 bits. The result is a hook or window handle that looks plausible
    and never works, so these are declared rather than left to the default.
    """
    user32.GetForegroundWindow.restype = wt.HWND
    user32.GetForegroundWindow.argtypes = []

    user32.SetWinEventHook.restype = wt.HANDLE
    user32.SetWinEventHook.argtypes = [wt.DWORD, wt.DWORD, wt.HMODULE,
                                       WINEVENTPROC, wt.DWORD, wt.DWORD,
                                       wt.DWORD]

    user32.UnhookWinEvent.restype = wt.BOOL
    user32.UnhookWinEvent.argtypes = [wt.HANDLE]

    user32.GetWindowRect.restype = wt.BOOL
    user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]

    user32.GetWindowThreadProcessId.restype = wt.DWORD
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND,
                                                ctypes.POINTER(wt.DWORD)]

    kernel32.OpenProcess.restype = wt.HANDLE
    kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]

    kernel32.CloseHandle.restype = wt.BOOL
    kernel32.CloseHandle.argtypes = [wt.HANDLE]

    kernel32.QueryFullProcessImageNameW.restype = wt.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD,
                                                    wt.LPWSTR,
                                                    ctypes.POINTER(wt.DWORD)]


def log(msg):
    print(f"[gate] {msg}", flush=True)


def config_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "onshape-trackball")


# ------------------------------------------------------------------ DPI

def declare_dpi_aware():
    """Put window rects, cursor reads and warps in one coordinate space.

    Without this a scaled display reports window rects in virtualised pixels while
    the cursor answers in physical ones, and recentring warps to the wrong place —
    the failure being a pan that jumps somewhere unrelated near a screen edge.
    """
    PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
    try:
        if user32.SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2):
            return "per-monitor-v2"
    except AttributeError:
        pass                                   # pre-1703
    try:
        # 2 == PROCESS_PER_MONITOR_DPI_AWARE
        if ctypes.WinDLL("shcore").SetProcessDpiAwareness(2) == 0:
            return "per-monitor"
    except (OSError, AttributeError):
        pass
    try:
        if user32.SetProcessDPIAware():
            return "system"
    except AttributeError:
        pass
    return "none"


# ------------------------------------------------------------------ events


class Event:
    """Same duck type as an evdev InputEvent: .type / .code / .value."""

    __slots__ = ("type", "code", "value")

    def __init__(self, etype, code, value):
        self.type = etype
        self.code = code
        self.value = value

    def __repr__(self):
        return f"Event({self.type}, {self.code}, {self.value})"


SYN = Event(codes.EV_SYN, codes.SYN_REPORT, 0)

# Interception reports a button as a pair of one-shot flags; the translator wants a
# key event with a value. Ordered so a press is seen before a release in the rare
# stroke that carries both.
_BUTTON_FLAGS = (
    (interceptor.MOUSE_LEFT_BUTTON_DOWN, codes.BTN_LEFT, 1),
    (interceptor.MOUSE_LEFT_BUTTON_UP, codes.BTN_LEFT, 0),
    (interceptor.MOUSE_RIGHT_BUTTON_DOWN, codes.BTN_RIGHT, 1),
    (interceptor.MOUSE_RIGHT_BUTTON_UP, codes.BTN_RIGHT, 0),
    (interceptor.MOUSE_MIDDLE_BUTTON_DOWN, codes.BTN_MIDDLE, 1),
    (interceptor.MOUSE_MIDDLE_BUTTON_UP, codes.BTN_MIDDLE, 0),
    (interceptor.MOUSE_BUTTON_4_DOWN, codes.BTN_SIDE, 1),
    (interceptor.MOUSE_BUTTON_4_UP, codes.BTN_SIDE, 0),
    (interceptor.MOUSE_BUTTON_5_DOWN, codes.BTN_EXTRA, 1),
    (interceptor.MOUSE_BUTTON_5_UP, codes.BTN_EXTRA, 0),
)

_warned_absolute = False


def stroke_to_events(stroke):
    """One Interception stroke -> the event packet evdev would have produced."""
    global _warned_absolute
    out = []

    for flag, code, value in _BUTTON_FLAGS:
        if stroke.state & flag:
            out.append(Event(codes.EV_KEY, code, value))

    if stroke.state & (interceptor.MOUSE_WHEEL | interceptor.MOUSE_HWHEEL):
        horizontal = bool(stroke.state & interceptor.MOUSE_HWHEEL)
        rolling = stroke.rolling
        # A whole number of clicks is the ordinary case and maps to REL_WHEEL. A
        # high-resolution wheel sends fractions of a click, and REL_WHEEL_HI_RES is
        # counted in exactly Windows' unit, so neither direction loses precision.
        if rolling % interceptor.WHEEL_DELTA == 0:
            code = codes.REL_HWHEEL if horizontal else codes.REL_WHEEL
            out.append(Event(codes.EV_REL, code,
                             rolling // interceptor.WHEEL_DELTA))
        else:
            code = (codes.REL_HWHEEL_HI_RES if horizontal
                    else codes.REL_WHEEL_HI_RES)
            out.append(Event(codes.EV_REL, code, rolling))

    if stroke.flags & interceptor.MOUSE_MOVE_ABSOLUTE:
        # Tablets and RDP report absolute coordinates. The translator's whole model
        # is relative deltas, so mistranslating would be worse than declining.
        if (stroke.x or stroke.y) and not _warned_absolute:
            _warned_absolute = True
            log("gated device reports absolute coordinates; motion is ignored. "
                "This gate expects an ordinary relative mouse.")
    else:
        if stroke.x:
            out.append(Event(codes.EV_REL, codes.REL_X, stroke.x))
        if stroke.y:
            out.append(Event(codes.EV_REL, codes.REL_Y, stroke.y))

    out.append(SYN)
    return out


# ------------------------------------------------------------------ enumeration


def _friendly_names():
    """(vid, pid) -> device description, best effort.

    interceptor.dll hands back a hardware ID and nothing else; a list of raw
    `HID\\VID_046D&PID_C52B` or `\\\\?\\HID#VID_046D&PID_C52B#...` strings is close
    to unusable when the whole point of the step is telling two mice apart. The
    registry has the descriptions, keyed by the old-style PnP HardwareID values,
    so this walks the HID enum looking for them and keys its own result by
    (vid, pid) rather than the full string — the one thing every shape of
    hardware ID this project has produced agrees on. Gives up quietly if the
    registry shape ever changes: a missing pretty name is cosmetic.
    """
    names = {}
    try:
        hid = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"SYSTEM\CurrentControlSet\Enum\HID")
    except OSError:
        return names

    with hid:
        for i in range(winreg.QueryInfoKey(hid)[0]):
            try:
                family = winreg.EnumKey(hid, i)
                with winreg.OpenKey(hid, family) as fam:
                    for j in range(winreg.QueryInfoKey(fam)[0]):
                        instance = winreg.EnumKey(fam, j)
                        with winreg.OpenKey(fam, instance) as inst:
                            _collect_name(inst, names)
            except OSError:
                continue
    return names


def _collect_name(key, names):
    try:
        hwids, _ = winreg.QueryValueEx(key, "HardwareID")
    except OSError:
        return
    desc = None
    for value in ("FriendlyName", "DeviceDesc"):
        try:
            desc = winreg.QueryValueEx(key, value)[0]
            break
        except OSError:
            continue
    if not desc:
        return
    # DeviceDesc is often "@input.inf,%hid_device%;USB Input Device".
    if desc.startswith("@") and ";" in desc:
        desc = desc.split(";", 1)[1]
    for hwid in hwids or ():
        vp = _vid_pid(hwid)
        if vp is not None:
            names.setdefault(vp, desc)


def enumerate_mice():
    """[(hardware_id, display_name)] for every mouse the driver can see."""
    with interceptor.Context() as ctx:
        found = ctx.mice()

    names = _friendly_names()
    seen = {}
    out = []
    for device, hwid in found:
        # Two identical mice share a hardware ID. Surfacing both rows with the same
        # identifier would let the user pick one and silently gate the other, so the
        # collision is stated instead.
        if hwid in seen:
            seen[hwid] += 1
            continue
        seen[hwid] = 1
        label = names.get(_vid_pid(hwid), hwid)
        out.append([hwid, label])

    for entry in out:
        if seen[entry[0]] > 1:
            entry[1] += f"  (WARNING: {seen[entry[0]]} identical devices share this "
            entry[1] += "hardware ID and cannot be told apart)"
    return [(hwid, label) for hwid, label in out]


def detect_mouse(timeout):
    """Hardware ID of the mouse that gets moved, or None.

    Every mouse is filtered for the duration, so each stroke has to be sent back on
    or the machine loses its pointer while the user is being asked to move one.
    """
    with interceptor.Context() as ctx:
        devices = {d: h for d, h in ctx.mice()}
        if not devices:
            return None

        ctx.filter_devices(devices)
        try:
            travelled = {}
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                remaining = max(0, int((deadline - time.monotonic()) * 1000))
                device = ctx.wait(min(remaining, 200) or 1)
                if not device:
                    continue
                stroke = ctx.receive_mouse(device)
                if stroke is None:
                    continue
                ctx.send_mouse(device, stroke)      # keep the mouse alive
                if stroke.flags & interceptor.MOUSE_MOVE_ABSOLUTE:
                    continue
                moved = abs(stroke.x) + abs(stroke.y)
                if not moved:
                    continue
                travelled[device] = travelled.get(device, 0) + moved
                if travelled[device] >= MOTION_THRESHOLD:
                    return devices[device]
            return None
        finally:
            ctx.clear_filter()


# ------------------------------------------------------------------ capture


class GatedDevice:
    """One mouse, filtered exclusively.

    While this is open the device's strokes reach nobody: not the cursor, not any
    application. Sending them on is the translator's job, and it only does that
    while the gate is open — which is the whole fail-closed property.
    """

    def __init__(self, hardware_id):
        self.hardware_id = hardware_id
        self.ctx = interceptor.Context()
        self.device = _wait_for_device(self.ctx, hardware_id)
        self.name = _label_for(hardware_id)
        self.template = self
        self.ctx.filter_device(self.device, interceptor.FILTER_MOUSE_ALL)
        self._closed = False

    def events(self):
        while not self._closed:
            device = self.ctx.wait(200)
            if not device:
                continue
            if device != self.device:
                continue
            stroke = self.ctx.receive_mouse(device)
            if stroke is None:
                continue
            for event in stroke_to_events(stroke):
                yield event

    def send(self, stroke):
        self.ctx.send_mouse(self.device, stroke)

    def close(self):
        self._closed = True
        try:
            self.ctx.clear_filter()
        except Exception:
            pass
        # Destroying the context is what un-filters the device for good. It also
        # happens automatically if the process dies, which is why a crash leaves the
        # mouse working rather than dead.
        try:
            self.ctx.close()
        except Exception:
            pass


def _label_for(hardware_id):
    return _friendly_names().get(_vid_pid(hardware_id), hardware_id)


def _wait_for_device(ctx, hardware_id):
    warned = False
    while True:
        matches = ctx.devices_for_hardware_id(hardware_id)
        if matches:
            if len(matches) > 1:
                log(f"{len(matches)} devices share hardware ID {hardware_id}; "
                    f"gating the lowest ({matches[0]}). They are indistinguishable.")
            return matches[0]
        if not warned:
            log(f"waiting for {hardware_id} to appear (mouse unplugged?)")
            warned = True
        time.sleep(1)


def open_gated_device(identifier):
    return GatedDevice(identifier)


# ------------------------------------------------------------------ focus

CHROME_IMAGES = ("chrome.exe",)

WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MINIMIZEEND = 0x0017
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

WINEVENTPROC = ctypes.WINFUNCTYPE(
    None, wt.HANDLE, wt.DWORD, wt.HWND, wt.LONG, wt.LONG, wt.DWORD, wt.DWORD)


def _image_name(hwnd):
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False,
                                  pid.value)
    if not handle:
        return None
    try:
        size = wt.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf,
                                                   ctypes.byref(size)):
            return None
        return os.path.basename(buf.value).lower()
    finally:
        kernel32.CloseHandle(handle)


def window_is_chrome(hwnd):
    """Chrome by executable, never by title.

    The Linux side matches WM_CLASS, which Windows has no equivalent of. Titles were
    ruled out for the same reason they were there: Onshape's sign-in page is titled
    "Sign in", and any application can call itself anything.

    Only chrome.exe is accepted. Other Chromium builds (Edge, Brave) are excluded
    deliberately — the extension has to be loaded in whichever browser this matches,
    and silently gating a browser the user never installed it into would present as
    a mouse that is simply dead.
    """
    if not hwnd:
        return False
    image = _image_name(hwnd)
    return image in CHROME_IMAGES if image else False


def window_geometry(hwnd):
    """Absolute rect of a window -> (x, y, w, h) or None."""
    if not hwnd:
        return None
    rect = wt.RECT()
    try:
        if not user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(rect)):
            return None
    except (ctypes.ArgumentError, OSError):
        return None
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None
    return (rect.left, rect.top, w, h)


def _focus_from(hwnd):
    if not window_is_chrome(hwnd):
        return False, None, None
    return True, window_geometry(hwnd), hwnd


def watch_focus(callback):
    """Event-driven foreground tracking, the counterpart of `xprop -root -spy`.

    A WinEvent hook needs a message pump on the thread that installed it, so this
    call owns the thread it is given and never returns.
    """
    while True:
        hook = None
        try:
            callback(*_focus_from(user32.GetForegroundWindow()))

            def on_event(_hook, _event, hwnd, id_object, _id_child, _thread, _time):
                # Only the window itself, not its scrollbars, menus or carets.
                if id_object != 0:
                    return
                try:
                    callback(*_focus_from(hwnd))
                except Exception as exc:
                    log(f"focus callback failed: {exc}")

            proc = WINEVENTPROC(on_event)
            hook = user32.SetWinEventHook(
                EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND,
                None, proc, 0, 0,
                WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS)
            if not hook:
                raise OSError(f"SetWinEventHook failed "
                              f"({ctypes.get_last_error()})")

            msg = wt.MSG()
            while True:
                got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if got == -1:
                    raise OSError(f"GetMessage failed ({ctypes.get_last_error()})")
                if got == 0:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:
            log(f"focus watcher restarting after error: {exc}")
        finally:
            if hook:
                try:
                    user32.UnhookWinEvent(hook)
                except Exception:
                    pass
        # Fail closed while there is no watcher, exactly as the X11 path does.
        callback(False, None, None)
        time.sleep(2)


# Declared last: the signatures reference the callback types defined above, and a
# truncated handle is the kind of failure that presents as "the hook silently never
# fires" rather than as an error.
_bind_win32()
