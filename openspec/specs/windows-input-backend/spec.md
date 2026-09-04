## Purpose

Exclusive capture of one mouse on Windows via the Interception kernel filter driver,
and enumeration of candidate mice with stable identifiers for the config — the
Windows counterpart to the Linux `evdev`/`uinput`/`libX11` backend, preserving its
fail-closed exclusivity guarantee.

## Requirements

### Requirement: Exclusive capture of the gated mouse
The daemon SHALL capture the configured mouse through the Interception driver such
that no other application receives its events, matching the exclusivity `EVIOCGRAB`
provides on Linux.

#### Scenario: Gate closed, gated mouse moved
- **WHEN** the gate is closed and the gated mouse is moved, clicked or scrolled
- **THEN** no motion, button or wheel event reaches any application
- **AND** the system cursor does not move

#### Scenario: Gate open, gated mouse moved
- **WHEN** the gate is open and the gated mouse is moved
- **THEN** the translated gesture is sent as a message over the synthetic-input
  channel to the extension
- **AND** no event is injected as OS-level input, and the real system cursor is
  never moved by the gated mouse

#### Scenario: Other mice unaffected
- **WHEN** a mouse other than the configured one is used
- **THEN** its events pass through untouched whether the gate is open or closed

#### Scenario: Daemon stops
- **WHEN** the daemon exits, crashes or is killed
- **THEN** the driver stops filtering and the gated mouse returns to normal behaviour
- **AND** no held button or modifier is left stuck down

### Requirement: Stable device identity
The backend SHALL identify the gated mouse by its Interception hardware ID rather
than by device number, because device numbers are reassigned across reboots and
re-plugs.

#### Scenario: Config stores a durable identifier
- **WHEN** a mouse is chosen during setup
- **THEN** the `device` config key holds that mouse's hardware ID

#### Scenario: Device number changes after reboot
- **WHEN** the machine reboots and the mouse is assigned a different device number
- **THEN** the daemon resolves the configured hardware ID to the new number and grabs
  the same physical mouse

#### Scenario: Configured mouse absent at startup
- **WHEN** the daemon starts and no attached device matches the configured hardware ID
- **THEN** it waits for the device to appear rather than exiting
- **AND** `/status` reports `device_attached` as false

#### Scenario: Two identical mice attached
- **WHEN** two mice report the same hardware ID
- **THEN** setup SHALL warn that they cannot be told apart and the lower device number
  is used

### Requirement: Mouse enumeration
The backend SHALL list attached mice with a human-readable name and hardware ID, and
SHALL support identifying one by physical movement, so setup can offer the same
choose-or-detect flow `pick-mouse.py` offers on Linux.

#### Scenario: Listing
- **WHEN** enumeration is requested
- **THEN** every attached mouse is returned with its hardware ID and a display name

#### Scenario: Detection by movement
- **WHEN** detection is requested and one mouse is moved past the motion threshold
  within the timeout
- **THEN** that mouse's hardware ID is returned

#### Scenario: Detection times out
- **WHEN** no mouse is moved before the timeout expires
- **THEN** detection reports failure without selecting a device

#### Scenario: Enumeration without the driver
- **WHEN** the Interception driver is not installed
- **THEN** enumeration reports that clearly rather than returning an empty list
