## Purpose

Event-driven foreground-window tracking, Chrome identification, and window geometry
on Win32, feeding the same gate signals (`chrome_focused`, window rectangle) the X11
`xprop -root -spy` watcher feeds on Linux, so gating behaviour is identical across
platforms.

## Requirements

### Requirement: Event-driven foreground window tracking
The daemon SHALL track the foreground window on Windows without polling, updating the
gate's `chrome_focused` signal whenever focus changes.

#### Scenario: Focus moves to Chrome
- **WHEN** a Google Chrome window becomes the foreground window
- **THEN** `chrome_focused` becomes true without waiting for a poll interval

#### Scenario: Focus leaves Chrome
- **WHEN** the foreground window changes to any non-Chrome window
- **THEN** `chrome_focused` becomes false and the gate closes

#### Scenario: Desktop or no foreground window
- **WHEN** there is no foreground window, or the shell holds focus
- **THEN** `chrome_focused` is false

#### Scenario: Watcher failure
- **WHEN** the focus watcher raises or its hook is lost
- **THEN** it sets `chrome_focused` to false, logs the reason, and restarts

### Requirement: Chrome identification
The daemon SHALL identify Chrome by the owning process's executable image name, since
Windows has no `WM_CLASS` equivalent.

#### Scenario: Chrome window focused
- **WHEN** the foreground window belongs to `chrome.exe`
- **THEN** it is treated as Chrome

#### Scenario: Similarly titled non-Chrome window
- **WHEN** a window of another application has a title mentioning Onshape or Chrome
- **THEN** it is not treated as Chrome

#### Scenario: Process cannot be queried
- **WHEN** the owning process cannot be determined
- **THEN** the window is not treated as Chrome and the gate stays closed

### Requirement: Window geometry
The daemon SHALL report the focused Chrome window's absolute screen rectangle, used
as the fallback bound for cursor penning when the extension's canvas report is stale.

#### Scenario: Geometry read on focus change
- **WHEN** a Chrome window takes focus
- **THEN** its absolute position and size are read and handed to the gate

#### Scenario: Per-monitor DPI
- **WHEN** the Chrome window is on a display with a scaling factor other than 100%
- **THEN** the reported rectangle is in the same physical pixel coordinate space the
  cursor position and warp use

#### Scenario: Geometry unavailable
- **WHEN** the window rectangle cannot be read
- **THEN** no geometry is reported and cursor penning falls back to permitting the
  full virtual desktop

### Requirement: Unchanged gate semantics
The Windows focus signal SHALL combine with the extension's tab signal exactly as the
X11 signal does, so gating behaviour is identical across platforms.

#### Scenario: Both signals agree
- **WHEN** Chrome is focused and the extension reports an onshape.com active tab
- **THEN** the gate is open

#### Scenario: Extension silent
- **WHEN** Chrome is focused but no extension push has arrived within the staleness
  window
- **THEN** the gate is closed and `seconds_since_extension_push` reflects that

#### Scenario: Refocusing Chrome
- **WHEN** focus returns to Chrome whose tab was already reported as onshape.com
- **THEN** the gate reopens immediately without waiting for a new extension push
