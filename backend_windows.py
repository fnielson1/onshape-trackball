"""Windows backend: Interception for capture, Win32 for the cursor and focus.

The shapes here mirror `backend_linux` one for one, so `gate.py` cannot tell them
apart:

    exclusive grab   EVIOCGRAB          -> Interception filter on one device
    synthetic mouse  uinput clone       -> Interception send on the same device
    synthetic keys   second uinput dev  -> SendInput with scancodes
    cursor           XQueryPointer      -> GetCursorPos / SetCursorPos
    focus            xprop -root -spy   -> SetWinEventHook(EVENT_SYSTEM_FOREGROUND)
    other mice       read-only evdev    -> Raw Input with RIDEV_INPUTSINK

Two of those deserve a note.

Keys go through SendInput rather than an Interception keyboard device: the modifier
must work even when no keyboard is enumerated by the driver, and SendInput always
is. It does mean the button and the modifier travel on separate streams, exactly as
they do on Linux across two uinput devices — which is what MODIFIER_SETTLE already
exists to absorb.

Other mice are watched with Raw Input, not Interception. Interception can only
observe by filtering, and filtering a mouse means it stops working if we stop
re-sending. Raw Input is read-only by construction, so an unrelated mouse can never
be collateral damage.
"""

import ctypes
import ctypes.wintypes as wt
import os
import time
import winreg

import codes
import interception

NAME = "windows"

RESTART_HINT = 'schtasks /Run /TN "Onshape trackball gate"'
DEVICE_HINT = "device = HID\\VID_046D&PID_C52B&..."

MOTION_THRESHOLD = 30  # accumulated |delta| units, matching the Linux picker

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_IS_64 = ctypes.sizeof(ctypes.c_void_p) == 8
LRESULT = ctypes.c_longlong if _IS_64 else ctypes.c_long
WPARAM = ctypes.c_ulonglong if _IS_64 else ctypes.c_ulong
LPARAM = ctypes.c_longlong if _IS_64 else ctypes.c_long


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

    user32.GetCursorPos.restype = wt.BOOL
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
    user32.SetCursorPos.restype = wt.BOOL
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]

    user32.SendInput.restype = wt.UINT
    user32.SendInput.argtypes = [wt.UINT, ctypes.c_void_p, ctypes.c_int]

    user32.DefWindowProcW.restype = LRESULT
    user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, WPARAM, LPARAM]

    user32.CreateWindowExW.restype = wt.HWND
    user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR,
                                       wt.DWORD, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, wt.HWND,
                                       wt.HMENU, wt.HINSTANCE, wt.LPVOID]

    user32.GetRawInputData.restype = wt.UINT
    user32.GetRawInputData.argtypes = [wt.HANDLE, wt.UINT, wt.LPVOID,
                                       ctypes.POINTER(wt.UINT), wt.UINT]

    user32.GetRawInputDeviceInfoW.restype = wt.UINT
    user32.GetRawInputDeviceInfoW.argtypes = [wt.HANDLE, wt.UINT, wt.LPVOID,
                                              ctypes.POINTER(wt.UINT)]

    user32.RegisterRawInputDevices.restype = wt.BOOL

    kernel32.OpenProcess.restype = wt.HANDLE
    kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]

    kernel32.CloseHandle.restype = wt.BOOL
    kernel32.CloseHandle.argtypes = [wt.HANDLE]

    kernel32.QueryFullProcessImageNameW.restype = wt.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD,
                                                    wt.LPWSTR,
                                                    ctypes.POINTER(wt.DWORD)]

    kernel32.GetModuleHandleW.restype = wt.HMODULE
    kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]


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
    (interception.MOUSE_LEFT_BUTTON_DOWN, codes.BTN_LEFT, 1),
    (interception.MOUSE_LEFT_BUTTON_UP, codes.BTN_LEFT, 0),
    (interception.MOUSE_RIGHT_BUTTON_DOWN, codes.BTN_RIGHT, 1),
    (interception.MOUSE_RIGHT_BUTTON_UP, codes.BTN_RIGHT, 0),
    (interception.MOUSE_MIDDLE_BUTTON_DOWN, codes.BTN_MIDDLE, 1),
    (interception.MOUSE_MIDDLE_BUTTON_UP, codes.BTN_MIDDLE, 0),
    (interception.MOUSE_BUTTON_4_DOWN, codes.BTN_SIDE, 1),
    (interception.MOUSE_BUTTON_4_UP, codes.BTN_SIDE, 0),
    (interception.MOUSE_BUTTON_5_DOWN, codes.BTN_EXTRA, 1),
    (interception.MOUSE_BUTTON_5_UP, codes.BTN_EXTRA, 0),
)

_DOWN_FLAG = {
    (codes.BTN_LEFT, 1): interception.MOUSE_LEFT_BUTTON_DOWN,
    (codes.BTN_LEFT, 0): interception.MOUSE_LEFT_BUTTON_UP,
    (codes.BTN_RIGHT, 1): interception.MOUSE_RIGHT_BUTTON_DOWN,
    (codes.BTN_RIGHT, 0): interception.MOUSE_RIGHT_BUTTON_UP,
    (codes.BTN_MIDDLE, 1): interception.MOUSE_MIDDLE_BUTTON_DOWN,
    (codes.BTN_MIDDLE, 0): interception.MOUSE_MIDDLE_BUTTON_UP,
    (codes.BTN_SIDE, 1): interception.MOUSE_BUTTON_4_DOWN,
    (codes.BTN_SIDE, 0): interception.MOUSE_BUTTON_4_UP,
    (codes.BTN_EXTRA, 1): interception.MOUSE_BUTTON_5_DOWN,
    (codes.BTN_EXTRA, 0): interception.MOUSE_BUTTON_5_UP,
}

_warned_absolute = False


def stroke_to_events(stroke):
    """One Interception stroke -> the event packet evdev would have produced."""
    global _warned_absolute
    out = []

    for flag, code, value in _BUTTON_FLAGS:
        if stroke.state & flag:
            out.append(Event(codes.EV_KEY, code, value))

    if stroke.state & (interception.MOUSE_WHEEL | interception.MOUSE_HWHEEL):
        horizontal = bool(stroke.state & interception.MOUSE_HWHEEL)
        rolling = stroke.rolling
        # A whole number of clicks is the ordinary case and maps to REL_WHEEL. A
        # high-resolution wheel sends fractions of a click, and REL_WHEEL_HI_RES is
        # counted in exactly Windows' unit, so neither direction loses precision.
        if rolling % interception.WHEEL_DELTA == 0:
            code = codes.REL_HWHEEL if horizontal else codes.REL_WHEEL
            out.append(Event(codes.EV_REL, code,
                             rolling // interception.WHEEL_DELTA))
        else:
            code = (codes.REL_HWHEEL_HI_RES if horizontal
                    else codes.REL_WHEEL_HI_RES)
            out.append(Event(codes.EV_REL, code, rolling))

    if stroke.flags & interception.MOUSE_MOVE_ABSOLUTE:
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
    """hardware id (upper) -> device description, best effort.

    Interception hands back a hardware ID and nothing else; a list of raw
    `HID\\VID_046D&PID_C52B` strings is close to unusable when the whole point of
    the step is telling two mice apart. The registry has the descriptions, so this
    walks the HID enum looking for them and simply gives up if the shape ever
    changes — a missing pretty name is cosmetic.
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
        names.setdefault(hwid.upper(), desc)


def enumerate_mice():
    """[(hardware_id, display_name)] for every mouse the driver can see."""
    with interception.Context() as ctx:
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
        label = names.get(hwid.upper(), hwid)
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
    with interception.Context() as ctx:
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
                if stroke.flags & interception.MOUSE_MOVE_ABSOLUTE:
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
        self.ctx = interception.Context()
        self.device = _wait_for_device(self.ctx, hardware_id)
        self.name = _label_for(hardware_id)
        self.template = self
        self.ctx.filter_device(self.device, interception.FILTER_MOUSE_ALL)
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
    return _friendly_names().get(hardware_id.upper(), hardware_id)


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


# ------------------------------------------------------------------ output


class VirtualOutput:
    """Accumulates writes into one Interception stroke, flushed on syn().

    uinput works the same way — writes queue until SYN_REPORT — so the translator's
    existing write/syn pairs need no rethinking. Batching also matters: a press and
    the motion that follows it belong in one stroke, or Onshape sees a click.
    """

    def __init__(self, device):
        self._device = device
        self._reset()

    def _reset(self):
        self._state = 0
        self._flags = interception.MOUSE_MOVE_RELATIVE
        self._rolling = 0
        self._x = 0
        self._y = 0
        self._pending = False

    def write(self, etype, code, value):
        if etype == codes.EV_KEY:
            flag = _DOWN_FLAG.get((code, 1 if value else 0))
            if flag:
                self._state |= flag
                self._pending = True
            return
        if etype == codes.EV_REL:
            if code == codes.REL_X:
                self._x += value
            elif code == codes.REL_Y:
                self._y += value
            elif code == codes.REL_WHEEL:
                self._state |= interception.MOUSE_WHEEL
                self._rolling += value * interception.WHEEL_DELTA
            elif code == codes.REL_HWHEEL:
                self._state |= interception.MOUSE_HWHEEL
                self._rolling += value * interception.WHEEL_DELTA
            elif code == codes.REL_WHEEL_HI_RES:
                self._state |= interception.MOUSE_WHEEL
                self._rolling += value
            elif code == codes.REL_HWHEEL_HI_RES:
                self._state |= interception.MOUSE_HWHEEL
                self._rolling += value
            else:
                return
            self._pending = True

    def write_event(self, event):
        if event.type == codes.EV_SYN:
            self.syn()
            return
        self.write(event.type, event.code, event.value)

    def syn(self):
        if not self._pending:
            return
        stroke = interception.MouseStroke(
            state=self._state, flags=self._flags, rolling=self._rolling,
            x=self._x, y=self._y, information=0)
        self._reset()
        try:
            self._device.send(stroke)
        except Exception as exc:
            log(f"could not send stroke: {exc}")

    def close(self):
        self._reset()


# --- keyboard output ---------------------------------------------------------------

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
KEYEVENTF_EXTENDEDKEY = 0x0001

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else wt.DWORD


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


# The AT set-1 scancodes for these three happen to equal their Linux keycodes, which
# is why the translator's codes can be used as the map's keys without ceremony. Kept
# explicit anyway: the coincidence does not hold generally.
_SCANCODE = {
    codes.KEY_ESC: 0x01,
    codes.KEY_LEFTCTRL: 0x1D,
    codes.KEY_SPACE: 0x39,
}


class KeyOutput:
    """Ctrl and the left-click key, via SendInput with scancodes.

    Scancodes rather than virtual keys because Onshape reads the browser's key
    events, and a scancode survives a non-US layout unchanged.
    """

    def __init__(self):
        self.ok = True

    def write(self, etype, code, value):
        if etype != codes.EV_KEY:
            return
        scan = _SCANCODE.get(code)
        if scan is None:
            return
        flags = KEYEVENTF_SCANCODE | (0 if value else KEYEVENTF_KEYUP)
        event = INPUT(type=INPUT_KEYBOARD,
                      ki=KEYBDINPUT(wVk=0, wScan=scan, dwFlags=flags,
                                    time=0, dwExtraInfo=0))
        sent = user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))
        if sent != 1:
            log(f"SendInput rejected scancode {scan:#x} "
                f"(error {ctypes.get_last_error()})")

    def write_event(self, event):
        self.write(event.type, event.code, event.value)

    def syn(self):
        """SendInput has no batching, so each write has already landed."""

    def close(self):
        pass


def modifier_output():
    return KeyOutput()


# ------------------------------------------------------------------ pointer


class Pointer:
    def __init__(self):
        self.ok = False
        try:
            user32.GetCursorPos
            user32.SetCursorPos
        except AttributeError as exc:
            log(f"cursor API unavailable ({exc}); pan recentring disabled")
            return
        self.ok = True

    def position(self):
        if not self.ok:
            return None
        point = wt.POINT()
        if not user32.GetCursorPos(ctypes.byref(point)):
            return None
        return (point.x, point.y)

    def warp(self, x_pos, y_pos):
        if not self.ok:
            return
        user32.SetCursorPos(int(x_pos), int(y_pos))


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


# ------------------------------------------------------------------ other mice

RIDEV_INPUTSINK = 0x00000100
RIDEV_REMOVE = 0x00000001
RID_INPUT = 0x10000003
RIM_TYPEMOUSE = 0
RIDI_DEVICENAME = 0x20000007
WM_INPUT = 0x00FF
HWND_MESSAGE = -3


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wt.USHORT), ("usUsage", wt.USHORT),
                ("dwFlags", wt.DWORD), ("hwndTarget", wt.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", wt.DWORD), ("dwSize", wt.DWORD),
                ("hDevice", wt.HANDLE), ("wParam", ULONG_PTR)]


class _RAWMOUSE_BUTTONS(ctypes.Structure):
    _fields_ = [("usButtonFlags", wt.USHORT), ("usButtonData", wt.USHORT)]


class _RAWMOUSE_UNION(ctypes.Union):
    _fields_ = [("ulButtons", wt.ULONG), ("b", _RAWMOUSE_BUTTONS)]


class RAWMOUSE(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("usFlags", wt.USHORT), ("u", _RAWMOUSE_UNION),
                ("ulRawButtons", wt.ULONG), ("lLastX", wt.LONG),
                ("lLastY", wt.LONG), ("ulExtraInformation", wt.ULONG)]


class RAWINPUT(ctypes.Structure):
    _fields_ = [("header", RAWINPUTHEADER), ("mouse", RAWMOUSE)]


WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, WPARAM, LPARAM)


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


def _device_name(handle):
    size = wt.UINT(0)
    user32.GetRawInputDeviceInfoW(handle, RIDI_DEVICENAME, None,
                                  ctypes.byref(size))
    if not size.value:
        return ""
    buf = ctypes.create_unicode_buffer(size.value + 1)
    if user32.GetRawInputDeviceInfoW(handle, RIDI_DEVICENAME, buf,
                                     ctypes.byref(size)) in (0, -1):
        return ""
    return buf.value


def _normalise(name):
    """Raw Input device paths and Interception hardware IDs describe the same device
    with different punctuation: `\\\\?\\HID#VID_046D&PID_C52B#7&...` against
    `HID\\VID_046D&PID_C52B`. Flattening the separators lets one be tested against
    the other with a substring check."""
    return name.upper().replace("\\", "#").replace("?", "").strip("#")


def watch_other_pointers(gated_identifier, on_activity, enabled):
    """Read-only Raw Input watch; any other mouse stirring ends the pan stroke.

    RIDEV_INPUTSINK delivers events even though this window is never focused, and
    Raw Input cannot suppress anything — so unlike the Interception path, a mistake
    here can never leave one of the user's other mice dead.
    """
    gated = _normalise(gated_identifier)
    class_name = "OnshapeGateRawInput"
    hinstance = kernel32.GetModuleHandleW(None)
    state = {"last": 0.0}

    def wndproc(hwnd, msg, wparam, lparam):
        if msg != WM_INPUT or not enabled():
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        size = wt.UINT(0)
        user32.GetRawInputData(wt.HANDLE(lparam), RID_INPUT, None,
                               ctypes.byref(size),
                               ctypes.sizeof(RAWINPUTHEADER))
        if size.value:
            buf = ctypes.create_string_buffer(size.value)
            got = user32.GetRawInputData(wt.HANDLE(lparam), RID_INPUT, buf,
                                         ctypes.byref(size),
                                         ctypes.sizeof(RAWINPUTHEADER))
            if got and got != -1:
                raw = ctypes.cast(buf, ctypes.POINTER(RAWINPUT)).contents
                if raw.header.dwType == RIM_TYPEMOUSE:
                    name = _normalise(_device_name(raw.header.hDevice))
                    stirred = (raw.mouse.lLastX or raw.mouse.lLastY
                               or raw.mouse.b.usButtonFlags)
                    if stirred and gated and gated not in name:
                        # Raw Input reports our own synthetic strokes too. Rate
                        # limiting is not enough on its own, but combined with the
                        # translator's yield cooldown it keeps a pan from fighting
                        # its own output.
                        now = time.monotonic()
                        if now - state["last"] > 0.01:
                            state["last"] = now
                            try:
                                on_activity()
                            except Exception as exc:
                                log(f"yield callback failed: {exc}")
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    while True:
        proc = WNDPROC(wndproc)
        try:
            wc = WNDCLASS()
            wc.lpfnWndProc = proc
            wc.hInstance = hinstance
            wc.lpszClassName = class_name
            user32.RegisterClassW(ctypes.byref(wc))    # benign if already there

            hwnd = user32.CreateWindowExW(
                0, class_name, class_name, 0, 0, 0, 0, 0,
                wt.HWND(HWND_MESSAGE), None, hinstance, None)
            if not hwnd:
                raise OSError(f"CreateWindowEx failed "
                              f"({ctypes.get_last_error()})")

            rid = RAWINPUTDEVICE(usUsagePage=0x01, usUsage=0x02,
                                 dwFlags=RIDEV_INPUTSINK, hwndTarget=hwnd)
            if not user32.RegisterRawInputDevices(
                    ctypes.byref(rid), 1, ctypes.sizeof(RAWINPUTDEVICE)):
                raise OSError(f"RegisterRawInputDevices failed "
                              f"({ctypes.get_last_error()})")

            msg = wt.MSG()
            while True:
                got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if got in (0, -1):
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:
            log(f"other-pointer watcher restarting after error: {exc}")
        time.sleep(2)


# Declared last: the signatures reference the callback types defined above, and a
# truncated handle is the kind of failure that presents as "the hook silently never
# fires" rather than as an error.
_bind_win32()
