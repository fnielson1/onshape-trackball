## 1. Platform seam refactor (Linux stays green)

- [x] 1.1 Extract `Translator`, `Gate`, `Handler`, `parse_rect`, `pan_timer`, `read_config` and every `resolve_*` clamper out of `gate.py` into a platform-neutral core module, leaving behavior byte-for-byte identical
- [x] 1.2 Define the backend interface: `enumerate_mice()`, `detect_mouse(timeout)`, `open_gated_device(identifier)`, `VirtualOutput` (`move`/`button`/`wheel`/`key_tap`), `Pointer` (`position`/`warp`), `watch_focus(callback)`, `other_pointer_activity(callback)`
- [x] 1.3 Move the evdev/uinput/libX11/xprop code into a Linux backend implementing that interface, selected by `sys.platform`
- [x] 1.4 Normalize backend events to the existing `(type, code, value)` shape so `Translator.handle` is unchanged
- [x] 1.5 Make config path resolution platform-aware (`$XDG_CONFIG_HOME` / `%APPDATA%`) without changing the file format or keys
- [ ] 1.6 Confirm `test_translator.py` and `test_config.py` pass unmodified on Linux
- [ ] 1.7 Smoke-test the real Linux install end to end — pan, rotate, zoom, left-click, re-centering, `--status` — and confirm no regression before any Windows code is written

## 2. Interception binding

- [x] 2.1 Write a `ctypes` wrapper over `interception.dll` covering context create/destroy, device predicate, receive, send, `get_hardware_id`, and wait-with-timeout
- [x] 2.2 Add a driver-presence check that distinguishes "not installed", "installed but needs reboot", and "active"
- [x] 2.3 Implement `enumerate_mice()` returning hardware ID plus display name for every attached mouse
- [x] 2.4 Implement `detect_mouse(timeout)` using the same accumulated-motion threshold `pick-mouse.py` uses, returning a hardware ID
- [x] 2.5 Handle the duplicate-hardware-ID case: report the collision and resolve to the lower device number

## 3. Windows input backend

- [x] 3.1 Implement `open_gated_device(hardware_id)` — resolve the ID to a device number, filter that device exclusively, and yield normalized events
- [x] 3.2 Swallow every event from the gated device while the gate is closed, including cursor motion
- [x] 3.3 Implement `VirtualOutput` injection for motion, buttons, wheel and key taps, preserving the Ctrl-before-button / button-before-Ctrl ordering `MODIFIER_SETTLE` depends on
- [x] 3.4 Implement `Pointer` via `GetCursorPos`/`SetCursorPos`, degrading to re-centering-disabled with a logged reason if unavailable
- [x] 3.5 Declare per-monitor DPI awareness at startup so window rects, cursor reads and warps share one coordinate space
- [x] 3.6 Implement `other_pointer_activity()` — observe non-gated mice without capturing them, for `pan_yield_to_other_mice`
- [x] 3.7 Release every held button and modifier on exit, crash and signal, so the mouse never returns to normal with a button stuck down
- [x] 3.8 Wait for the configured device rather than exiting when it is absent at startup, and reflect that in `device_attached`

## 4. Windows focus tracking

- [x] 4.1 Implement `watch_focus()` on `SetWinEventHook(EVENT_SYSTEM_FOREGROUND)` with its own message pump thread
- [x] 4.2 Identify Chrome by the owning process image name; decide and document whether other Chromium builds are accepted
- [x] 4.3 Report the focused window's absolute rect via `GetWindowRect`
- [x] 4.4 Report not-focused when there is no foreground window, the shell holds focus, or the owning process cannot be queried
- [x] 4.5 Restart the watcher on failure, setting `chrome_focused` false and logging the reason first
- [x] 4.6 Verify the gate opens only when focus and the extension's tab report agree, and reopens instantly on refocus without a new push

## 5. `setup.cmd` — scaffolding and read-only paths

- [x] 5.1 Parse `--status`/`-s`, `--reconfigure`, `--device`, `--uninstall`, `--yes`/`-y`, `--help`/`-h`; reject unknown options and a valueless `--device` with a non-zero exit
- [x] 5.2 Write the usage text covering every option and all install steps, mirroring `setup.sh --help`
- [x] 5.3 Add the small Python entry points the script delegates to: status polling, device enumeration, config templating
- [x] 5.4 Implement the status board — driver state, Scheduled Task, device grab, config freshness, gate state, extension push — interpreting the daemon's status endpoint as `setup.sh` does
- [x] 5.5 Report the daemon as down rather than erroring when port 47653 does not respond
- [x] 5.6 Verify `--status` creates, modifies and removes nothing in every machine state

## 6. `setup.cmd` — install steps

- [x] 6.1 Detect administrator rights; when the driver install needs them, and they are absent, explain how to re-run elevated and exit without attempting it
- [x] 6.2 Install the Interception driver, then stop at the reboot gate with an explanation that re-running afterwards resumes
- [x] 6.3 Detect and name specific install blockers — Secure Boot, driver signature enforcement, anti-cheat interference — instead of reporting a generic failure
- [x] 6.4 Create `%APPDATA%\onshape-trackball\config` with all seven keys and their inline documentation on first run
- [x] 6.5 Append keys missing from an older config without disturbing existing values
- [x] 6.6 Implement mouse selection: numbered list, detect-by-movement, and `--device ID`; require an explicit choice even under `--yes`
- [x] 6.7 Register the Scheduled Task (at-logon, run only when logged on, restart on failure), start it, and confirm the daemon responds
- [x] 6.8 Re-register and restart the task when its definition points at a different script path or interpreter
- [x] 6.9 Report the failure and the daemon log location when the daemon does not respond within the startup timeout
- [x] 6.10 Detect config drift against the running daemon's settings and restart the service
- [x] 6.11 Verify the device grab and print the extension step with the absolute path to `extension/`, reporting it as done once a push has arrived
- [x] 6.12 Implement `--reconfigure` — re-pick the mouse and restart the service
- [x] 6.13 Verify a re-run on a complete install reports every step done and changes nothing, and that an interrupted run resumes correctly

## 7. `setup.cmd` — uninstall

- [x] 7.1 Stop and remove the Scheduled Task and delete the config
- [x] 7.2 Ask separately about removing the driver, defaulting to keeping it, and note that removal needs a reboot
- [x] 7.3 State that the Chrome extension must be removed by hand
- [x] 7.4 Verify no file inside the repository is modified or deleted on any uninstall path

## 8. Verification and documentation

- [x] 8.1 Confirm `test_translator.py` and `test_config.py` pass on Windows with no driver installed and no hardware
- [ ] 8.2 Manually verify the gated mouse is fully inert outside a frontmost onshape.com tab — cursor does not move, buttons and wheel do nothing
- [ ] 8.3 Manually verify pan, rotate, zoom and left-button-clears-selection in Onshape on Windows
- [ ] 8.4 Manually verify pan re-centering, including on a scaled and a multi-monitor display
- [ ] 8.5 Manually verify `pan_yield_to_other_mice` and document how the behavior differs from X11
- [ ] 8.6 Verify fail-closed: kill the daemon mid-pan and confirm the mouse goes dead with nothing stuck down
- [x] 8.7 Add a Windows section to `README.md` — requirements, install steps including the reboot, config path, everyday commands, and the anti-cheat limitation
- [x] 8.8 Update the README file table with the new modules and `setup.cmd`
