## Why

The gate only runs on Linux/X11: `gate.py` is built on `evdev`, `uinput`, `libX11`
and `xprop`, and `setup.sh` installs a udev rule, an `input` group membership and a
systemd user unit. On Windows none of those exist, so there is nothing for a batch
installer to install — making the project usable there means porting the runtime as
well as the installer.

## What Changes

- **New Windows input backend.** Replace `evdev`/`uinput` with the
  [Interception](https://github.com/oblitum/Interception) kernel filter driver, which
  is the only Windows mechanism offering a true per-device exclusive grab equivalent
  to `EVIOCGRAB`. The gated mouse's events are swallowed before any application sees
  them and replayed as synthetic input only while the gate is open, preserving the
  fail-closed guarantee.
- **New Windows focus backend.** Replace the `xprop -root -spy` watcher with a Win32
  `SetWinEventHook(EVENT_SYSTEM_FOREGROUND)` listener, and `xwininfo` window geometry
  with `GetWindowRect`. Chrome is identified by executable image name rather than
  `WM_CLASS`.
- **New Windows pointer backend.** Replace the `libX11` `XQueryPointer`/`XWarpPointer`
  `Pointer` class with `GetCursorPos`/`SetCursorPos`, keeping pan recentring working.
- **Refactor `gate.py` into a shared core plus platform backends.** The translator
  state machine, gate logic, config parsing and HTTP status server are platform-neutral
  and must keep behaving identically on both systems; only capture, injection, pointer
  and focus become swappable. `test_translator.py` and `test_config.py` must keep
  passing unchanged on both platforms.
- **New `setup.cmd`.** A batch installer mirroring `setup.sh`'s full surface —
  `--status`, `--reconfigure`, `--device`, `--uninstall`, `--yes`, `--help` — including
  the same resumable, re-runnable, every-step-checked behaviour. It installs the
  Interception driver (requiring a reboot, the Windows analogue of the udev rule plus
  re-login), picks the mouse, registers the daemon as a Scheduled Task, verifies the
  grab, and prints the same status board.
- **Service lifecycle moves to a Scheduled Task** on Windows (logon-triggered, restart
  on failure) in place of the systemd user unit.
- **Config path becomes `%APPDATA%\onshape-trackball\config`** on Windows, with the
  same keys, the same inline documentation and the same clamping. Device identity is
  a stable Interception device identifier instead of a `/dev/input/by-id` path.
- Extension is unchanged; it already talks to `localhost:47653` and needs only the
  documented manual load.
- **Incidental fix, agreed separately.** `gate.py`'s in-code `PAN_DEADZONE` fallback
  was 10 while `setup.sh`, the config it writes, and `test_config.py` all said 20 —
  a pre-existing disagreement on `main`, not introduced here. It also made
  `setup.sh --status` report permanent unfixable config drift for any config missing
  that key, since the drift check assumes 20. Set to 20; both suites now pass.
- **Not breaking.** The Linux path keeps its current behaviour, paths and commands.

## Capabilities

### New Capabilities
- `windows-input-backend`: exclusive capture of one mouse via the Interception driver,
  synthetic mouse and keyboard injection, cursor query/warp, and enumeration of
  candidate mice with stable identifiers for the config.
- `windows-focus-tracking`: event-driven foreground-window tracking, Chrome
  identification, and window geometry on Win32, feeding the same gate signals the X11
  watcher feeds today.
- `windows-installer`: `setup.cmd` — prerequisite and driver install, reboot gate,
  mouse selection, Scheduled Task lifecycle, config creation and drift detection,
  status board, reconfigure and uninstall.

### Modified Capabilities
<!-- None. openspec/specs/ is empty; existing Linux behaviour is unspecified and
     unchanged by this change. -->

## Impact

- **Code**: `gate.py` split into a platform-neutral core and `backend_linux` /
  `backend_windows` modules; new `pick-mouse` equivalent for Windows device
  enumeration; new `setup.cmd`; `README.md` gains a Windows section.
- **Dependencies**: Interception driver (kernel-mode, signed, requires an
  administrator install and a reboot) plus its Python ctypes bindings. Python 3 on
  Windows. No new dependency on the Linux side.
- **Risk**: the driver install is the sharpest edge — it needs admin rights, a reboot,
  and on some machines Secure Boot or anti-cheat software will interfere. `setup.cmd`
  must detect and report these rather than failing opaquely.
- **Tests**: `test_translator.py` and `test_config.py` must run on Windows without
  hardware or the driver, which is the main constraint on how the backend seam is
  drawn.
- **Unaffected**: `extension/`, the HTTP status contract on port 47653, and every
  documented Linux command.
