## Context

`gate.py` is a single 1200-line module in which the interesting logic — the
`Translator` pan/rotate state machine, the `Gate` two-signal AND, config parsing and
clamping, and the HTTP status server — is entirely platform-neutral, but is
interleaved with four platform bindings that are not:

| Concern | Linux today | Windows equivalent |
| --- | --- | --- |
| Exclusive device capture | `evdev` + `EVIOCGRAB` | Interception driver |
| Synthetic output | `uinput` (two virtual devices) | Interception injection |
| Cursor read/warp | `libX11` via ctypes (`Pointer`) | `GetCursorPos`/`SetCursorPos` |
| Focus tracking | `xprop -root -spy` subprocess | `SetWinEventHook` |
| Service | systemd user unit | Scheduled Task |
| Permissions gate | udev rule + `input` group + re-login | driver install + reboot |

The two existing test files are the binding constraint on how this is refactored:
`test_translator.py` `exec`s `gate.py` and drives the translator against a stubbed
virtual device, and both suites are documented as running "without hardware or
permissions". They must keep doing that, on both platforms.

The design decisions below were settled with the user before writing: full runtime
port (not installer-only), using the Interception driver (not Raw Input plus a
low-level hook).

## Goals / Non-Goals

**Goals:**

- Windows gets the same behavior the README describes: the gated mouse is inert
  everywhere except a frontmost onshape.com tab in Chrome, where it pans, rotates,
  zooms and clears the selection.
- `setup.cmd` matches `setup.sh` option for option and step for step, with the same
  re-runnable, resume-where-it-stopped character and the same status board.
- Fail-closed is preserved: daemon down means dead mouse, never unrestricted mouse.
- The translator, gate, config and status code stay single-sourced across platforms,
  and the existing tests keep passing on both without hardware.
- Linux behavior, paths and commands are untouched.

**Non-Goals:**

- Wayland, macOS, or any third platform.
- Packaging, code signing, or an installer executable. `setup.cmd` remains a script
  run from a clone, as `setup.sh` is.
- Redistributing the Interception driver binaries in this repo.
- Changing the extension, the port, or the `/status` JSON contract.
- A GUI, a tray icon, or Windows service (as opposed to Scheduled Task) hosting.

## Decisions

### Interception over Raw Input plus a low-level hook

Interception is a kernel-mode filter driver sitting above the mouse class driver. It
is the only Windows mechanism that gives a genuine per-device exclusive grab: events
from the chosen device can be swallowed before any application, including the cursor,
sees them.

*Alternative considered:* `WM_INPUT` (Raw Input) to identify which physical device
moved, plus a `WH_MOUSE_LL` hook to swallow events. Rejected because the two are not
correlated — the low-level hook does not carry device identity, so swallowing
"the gated mouse's" events means guessing from timing, and the cursor still jumps from
the gated mouse before the hook can suppress it. That breaks the core promise that the
mouse is *dead* outside Onshape, not merely ignored.

*Cost accepted:* an administrator driver install and a reboot. This is a real
regression against `apt install python3-evdev`, but it is structurally the same shape
as the existing udev-rule-plus-re-login gate, so `setup.cmd` can present it the same
way — stop, explain, resume on the next run.

### Backend seam drawn at the narrowest surface

Split `gate.py` into a platform-neutral core plus a backend module selected at import
by `sys.platform`. The backend exposes only:

- `enumerate_mice()` / `detect_mouse(timeout)` — for the picker
- `open_gated_device(identifier)` — the exclusive grab, yielding an event iterator
- `VirtualOutput` — `write(type, code, value)`, `write_event(event)` and `syn()`,
  with the same ordering guarantees `MODIFIER_SETTLE` relies on

  *Revised during implementation.* This was first specified as `move`/`button`/
  `wheel`/`key_tap`. That set cannot work: `test_translator.py`'s stub implements
  `write`/`write_event`/`syn` and asserts on the exact sequence they receive, so a
  higher-level interface would have meant rewriting the assertions in all 8 cases —
  losing the tests as evidence that the refactor changed nothing. The lower seam is
  also the more faithful one, since it is what uinput already exposes; the Windows
  side accumulates writes and emits one Interception stroke per `syn()`, which is
  how uinput batches to `SYN_REPORT` anyway.
- `Pointer` — `position()` / `warp()`, already an isolated class
- `watch_focus(callback)` — pushes `(chrome_focused, geometry, window_id)`
- `other_pointer_activity(callback)` — for `pan_yield_to_other_mice`

Everything above that line — `Translator`, `Gate`, `Handler`, `read_config` and the
`resolve_*` clampers — moves unchanged into the core and is imported by both
platforms. Events are normalized to the existing `(type, code, value)` shape so
`Translator.handle` does not change at all, which is what keeps `test_translator.py`
working against its stub.

*Alternative considered:* a parallel `gate_win.py`. Rejected — the translator is the
part with the hard-won tuning (`MODIFIER_SETTLE` sized by measurement, the deadzone,
re-centering around the warp) and duplicating it guarantees the two drift.

### Hardware ID, not device number, as the configured identity

Interception numbers devices 11–20 for mice, and the numbering is reassigned across
reboots and re-plugs. Storing a number would silently gate the wrong mouse after a
reboot — a bad failure, since the symptom is "my normal mouse died". The config stores
`interception_get_hardware_id()` output and the daemon resolves it to a number at
startup and on device arrival, which mirrors how `/dev/input/by-id` gives Linux a
stable path.

Two physically identical mice share a hardware ID and cannot be distinguished; setup
warns and takes the lower device number rather than pretending otherwise.

### Scheduled Task, not a Windows service

The daemon must run in the interactive user's session — it injects input and reads the
foreground window, neither of which works from session 0. A Scheduled Task with an
at-logon trigger, "run only when user is logged on", and restart-on-failure is the
closest analogue to a systemd *user* unit. A true Windows service would need session
brokering for no benefit.

### DPI awareness declared per-monitor

`GetWindowRect`, `GetCursorPos` and `SetCursorPos` must agree on a coordinate space or
re-centering will warp to the wrong place on scaled displays. The daemon declares
per-monitor DPI awareness at startup so all three report physical pixels, matching the
single coordinate space the X11 root window provides today.

### Config at `%APPDATA%`, same format

Same `key = value` format, same keys, same inline documentation, same clamping — only
the location and the restart command in the header comment differ. This keeps
`read_config` and `test_config.py` platform-neutral, and keeps the README's
configuration section true on both systems.

### `setup.cmd` shells out to Python for anything non-trivial

Batch is a poor language for JSON parsing, HTTP requests and device enumeration. The
script keeps control flow, prompting and the status board in batch — so it is a real
`.cmd` the user can read and run — and delegates status polling, device enumeration
and config templating to small Python entry points. The alternative, a PowerShell
script, was set aside because the user asked for a batch script and because execution
policy is one more thing to explain.

## Risks / Trade-offs

- **Driver install is the sharpest edge** → `setup.cmd` detects and names the specific
  blocker (no admin rights, Secure Boot, signature enforcement, anti-cheat drivers)
  rather than reporting a generic failure, and the reboot gate is presented as a
  normal step, not an error.
- **Interception is a third-party kernel driver with modest maintenance activity** →
  it is not vendored; setup points at the upstream release and verifies what is
  installed. If it ever becomes unusable the seam above means only the backend module
  is affected.
- **Anti-cheat and some security software treat input filter drivers as hostile** →
  documented as a known limitation in the README's Windows section; there is no
  mitigation available to this project.
- **Refactoring `gate.py` risks regressing tuned Linux behavior** → the refactor lands
  as its own step with both suites green and a manual Linux smoke test *before* any
  Windows code is written, so a regression is attributable.
- **Windows has no shared-pointer story identical to X11's** →
  `pan_yield_to_other_mice` still works (other mice are observed, not grabbed), but the
  interaction differs in detail; verified by hand rather than assumed.
- **No CI for either platform** → the test suites cover the translator and config only;
  everything below the backend seam is verified manually against the checklist in
  tasks.md.

## Migration Plan

No migration: this is additive. Linux installs are untouched, the config format and
`/status` contract are unchanged, and the extension is shared. Rollback for a Windows
user is `setup.cmd --uninstall` plus an opt-in driver removal.

## Open Questions

**Resolved during implementation:**

- *Which Interception binding.* An in-repo `ctypes` wrapper (`interception.py`), for
  the reason anticipated: the surface actually needed is a dozen calls, and it keeps
  the install to "put the DLL somewhere" rather than adding a pip dependency.
- *Whether to accept other Chromium builds.* No — `chrome.exe` only. The extension
  has to be loaded in whichever browser the check matches, so gating Edge or Brave
  would present as a mouse that is simply dead. Recorded in `window_is_chrome`.
- *How keys reach the browser.* `SendInput` with scancodes rather than an Interception
  keyboard device, so the modifier works even when the driver enumerates no keyboard.
  It leaves button and modifier on separate streams — the same condition `MODIFIER_SETTLE`
  already exists to absorb on Linux, where they cross two uinput devices.

**Still open:**

- Whether `left_click_key` should default to something other than `space` on Windows;
  assumed identical until tested in Onshape there.
- Whether Raw Input's view of our own synthetic strokes needs more than the device-name
  exclusion plus the existing yield cooldown. Only real use will show this.
- Absolute-coordinate devices (tablets, RDP sessions) are declined rather than
  translated, since the translator's model is relative deltas throughout. Fine for the
  stated use case; would need real work to support.

## Implementation notes

Four defects in `setup.cmd` were found only by running it; none were visible to
review, and each broke the script outright rather than subtly.

- **LF line endings.** `cmd.exe` seeks by byte offset for `goto` and `call`, computing
  it as if lines ended CRLF. An LF-only batch file therefore lands progressively
  further mid-line and executes fragments (`setlocal` as `tlocal`). `.gitattributes`
  now forces `*.cmd eol=crlf`, because a clone with `core.autocrlf=input` would
  otherwise produce a script that cannot run at all.
- **`for ... do if ... (` spanning lines** in `find_python` failed with "do was
  unexpected at this time", on every path. Rewritten flat.
- **`shift` then `%~1` in one block.** Batch expands `%~1` when it *parses* the block,
  so the value read was the pre-shift argument and `--device` with no value fell
  through instead of erroring. The read now happens after a `goto`.
- **`echo %~1` re-parses for redirection.** `%~1` strips the quotes, so a message
  containing `<`, `>`, `&` or `|` became an operator — the driver error mentioning
  `vendor/interception/<arch>/` made cmd try to open a file called `arch`, printing
  an error and silently dropping the status line. Messages now go through a variable
  and delayed expansion, which is not re-scanned.

Separately, **prompts use `choice`, not `set /p`**. `set /p` reads whatever stdin
happens to be and yields an empty string when it is not the console it expects,
which read a typed "y" as "no". That is merely irritating at the uninstall
confirmation and fatal at mouse selection, where an empty answer means the install
cannot proceed. `choice` takes one keypress from the console and returns it as an
exit code, so there is no variable that can come back empty; `set /p` remains as a
fallback, and mouse selection falls back to it when there are more than nine mice,
since `choice` accepts only single characters.
