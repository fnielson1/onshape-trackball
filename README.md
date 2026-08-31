# Onshape trackball gate

Restricts one of two mice so it **only works while onshape.com is frontmost in
Chrome**, and turns its motion into Onshape navigation. Everywhere else — other
tabs, other windows, the desktop — that mouse does nothing at all.

| Left mouse | Onshape sees | Result |
| --- | --- | --- |
| move | synthetic Ctrl + right-drag | **pan** |
| right button + move | Ctrl dropped, same drag | **rotate** |
| wheel | wheel | zoom |
| left button | space | **clear the selection** |

## How it works

The daemon grabs the chosen mouse exclusively, so the desktop never sees the real
device, and replays its events onto a synthetic one — but only while the gate is
open. The gate needs **two** signals to agree:

- **The desktop** says the focused window belongs to Chrome, tracked event-driven
  rather than polled.
- **A Chrome extension** says the frontmost Chrome window's active tab is on
  `onshape.com`. It reports only on the *tab*, never on focus — the daemon owns that,
  so refocusing Chrome reopens the gate instantly instead of waiting for a push.

Neither is sufficient alone: the window manager cannot see a tab's URL, and the
extension's MV3 service worker gets suspended while Chrome sits in the background.
Window titles were ruled out early — Onshape's sign-in page is titled just "Sign
in", and document tabs are named after the document.

Both halves are platform-specific, and everything above them is not. `gate.py` holds
the translator, the gate and the config; `backend_linux.py` and `backend_windows.py`
hold the four things that genuinely differ:

| | Linux | Windows |
| --- | --- | --- |
| Exclusive grab | `EVIOCGRAB` | Interception driver |
| Synthetic output | two `uinput` devices | Interception send, `SendInput` for keys |
| Cursor | `XQueryPointer` / `XWarpPointer` | `GetCursorPos` / `SetCursorPos` |
| Focus | `xprop -root -spy` | `SetWinEventHook` |
| Service | systemd user unit | scheduled task |

Panning is synthesised by holding Ctrl and the right button — one of Onshape's two
documented pan gestures. The other, middle-drag, is deliberately unused: panning
presses the same button repeatedly, and two presses inside the double-click window
are a double middle-click, which Onshape reads as Zoom to Fit.

A mouse never reports "I stopped moving", so a timeout ends the stroke — see
`pan_idle_release_ms` below.

It **fails closed**: if the daemon or extension stops, the mouse goes dead rather
than becoming unrestricted.

## Requirements

**Linux**

- X11 session (focus tracking uses `xprop`; there is no Wayland equivalent)
- Google Chrome
- `python3-evdev` — `sudo apt install python3-evdev`
- `sudo` access, for a one-time udev rule and group change

**Windows**

- Windows 10 or 11
- Google Chrome
- Python 3 on `PATH` (the Microsoft Store stub is not enough — `setup.cmd` checks)
- The [Interception](https://github.com/oblitum/Interception) driver, plus
  administrator rights and a reboot to install it

## Install

```bash
./setup.sh
```

Re-run it any time. Every step is checked before it is attempted, so it resumes
rather than repeating work.

1. **Permissions** *(sudo)* — installs a udev rule making `/dev/uinput`
   group-writable, and adds you to the `input` group.
2. **Log out and back in** — the group only takes effect on a fresh session. A new
   terminal is not enough; the systemd user manager has to restart too. The script
   stops here and tells you, then continues from step 3 on the next run.
3. **Choose the mouse** — pick from a list, or press `d` and *move* the mouse you
   want gated. Detection is the reliable option; device names are easy to mix up.
4. **Service** — installs and starts a systemd user unit.
5. **Mouse grab** — confirms the daemon actually has the device.
6. **Extension** — load it by hand: `chrome://extensions` → **Developer mode** →
   **Load unpacked** → this repo's `extension/` directory.

Step 6 is the only manual part, and Chrome will nag about developer-mode
extensions on startup. That is unavoidable for unpacked extensions.

### Windows

```bat
setup.cmd
```

Same character as `setup.sh` — re-run it any time, every step is checked before it
is attempted, and it resumes rather than repeating work.

1. **Driver** *(administrator)* — installs Interception. Download it from
   [oblitum/Interception](https://github.com/oblitum/Interception), run
   `install-interception.exe /install` from an elevated prompt, and put
   `interception.dll` next to `gate.py` or in `vendor\interception\x64\`.
2. **Reboot** — the driver only takes effect on a fresh boot. The script stops
   here and tells you, then continues from step 3 on the next run. This is the
   Windows counterpart of the Linux udev-rule-and-re-login gate.
3. **Configuration** — creates `%APPDATA%\onshape-trackball\config`.
4. **Choose the mouse** — pick from a list, or press `d` and *move* the mouse you
   want gated. Detection is the reliable option; device names are easy to mix up.
5. **Service** — registers and starts a scheduled task that runs at logon.
6. **Mouse grab** — confirms the daemon actually has the device.
7. **Extension** — exactly as on Linux, loaded by hand from `extension\`.

Why a kernel driver: Windows has no user-space way to take one mouse away from
every application. Raw Input can tell you *which* device moved but cannot suppress
it, and a low-level hook can suppress but does not carry device identity — so the
cursor would still jump before anything could stop it. Interception sits above the
mouse class driver, which is what makes the gated mouse genuinely dead rather than
merely ignored.

**Known limitation:** anti-cheat drivers (Vanguard, EasyAntiCheat, BattlEye) and
some endpoint-security products either block input filter drivers or refuse to run
alongside them. `setup.cmd` names the ones it can detect, but there is no
workaround this project can offer.

## Configuration

`~/.config/onshape-trackball/config` on Linux,
`%APPDATA%\onshape-trackball\config` on Windows, created on first run:

```ini
device               = /dev/input/by-id/usb-PixArt_USB_Optical_Mouse-event-mouse
left_click_key       = space
pan_idle_release_ms  = 150
```

Same keys and same format on both. Only `device` differs in shape: a
`/dev/input/by-id` path on Linux, an Interception hardware ID
(`HID\VID_046D&PID_C52B&...`) on Windows. It is the hardware ID rather than the
device number because Interception renumbers devices across reboots, and a number
would quietly start gating a different mouse.

Every setting is documented inline in the file itself, which the setup script
generates and keeps up to date — that file is the reference, not this list. Bad
values warn and fall back rather than stopping the daemon.

After editing, re-run the setup script (it notices the drift and restarts) or:

```bash
systemctl --user restart onshape-mouse-gate.service    # Linux
```
```bat
schtasks /End /TN "Onshape trackball gate" & schtasks /Run /TN "Onshape trackball gate"
```

## Everyday commands

```bash
./setup.sh --status         # what is working, at a glance
./setup.sh --reconfigure    # switch to the other mouse
./setup.sh --help           # full usage
curl -s localhost:47653/status

systemctl --user stop onshape-mouse-gate.service    # temporarily normal mouse
```

On Windows the same options, through `setup.cmd`:

```bat
setup.cmd --status
setup.cmd --reconfigure
setup.cmd --help
curl -s localhost:47653/status

schtasks /End /TN "Onshape trackball gate"          :: temporarily normal mouse
```

## Troubleshooting

`curl -s localhost:47653/status` reports the live gate state; `./setup.sh --status`
interprets it for you.

| Symptom | Look at |
| --- | --- |
| Mouse completely dead | `gate_open` — needs `chrome_focused` **and** `onshape_tab` |
| Gate never opens | `seconds_since_extension_push` is `null` → extension not loaded |
| Mouse works everywhere | Daemon not running, so nothing is grabbing it |
| Motion does not pan | Onshape's `View manipulation` preference must accept Ctrl + right-drag |
| Changes not taking effect | `daemon settings match the config` in `--status` |

You cannot click *into* Onshape with the gated mouse — it is inert until Onshape is
already frontmost, so focus the window with your other mouse first. Both mice also
share one cursor; the gated one simply stops contributing motion outside Onshape.

## Uninstall

```bash
./setup.sh --uninstall      # Linux
```
```bat
setup.cmd --uninstall       :: Windows
```

Removes the service and config. Asks separately before touching shared state — the
udev rule and your `input` group membership on Linux, the Interception driver on
Windows — and defaults to keeping it. Never touches this directory. Remove the
extension yourself at `chrome://extensions`.

## Files

| File | Purpose |
| --- | --- |
| `setup.sh` | Linux installer, status board, reconfigure, uninstall |
| `setup.cmd` | The same on Windows |
| `setup_helper.py` | What `setup.cmd` delegates JSON, HTTP and enumeration to |
| `gate.py` | The daemon: translator, gate, config, status server |
| `backend.py` | Picks a platform backend and defines the interface |
| `backend_linux.py` | evdev/uinput capture, libX11 cursor, `xprop` focus |
| `backend_windows.py` | Interception capture, Win32 cursor, focus and Raw Input |
| `interception.py` | ctypes binding for `interception.dll` |
| `codes.py` | Input event constants, so the core needs no evdev |
| `pick-mouse.py` | Lists mice, or detects one by movement (Linux) |
| `extension/` | Chrome MV3 extension reporting the active tab |
| `test_translator.py` | Pan/rotate state machine — 8 cases |
| `test_config.py` | Config parsing, clamping, fallbacks — 11 cases |

```bash
python3 test_translator.py && python3 test_config.py
```

Both run on either platform without hardware, permissions or the driver: the
translator tests drive synthetic events against a stubbed virtual device, and the
event codes come from `codes.py` rather than from evdev.
