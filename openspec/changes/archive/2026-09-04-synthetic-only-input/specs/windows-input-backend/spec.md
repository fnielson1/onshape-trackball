## MODIFIED Requirements

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

## REMOVED Requirements

### Requirement: Synthetic input injection
**Reason**: Replaced by the `synthetic-input-channel` capability — every translated
action (rotate/pan motion and its button, the left-click key-tap, the wheel delta)
is now sent to the Chrome extension over a local channel and dispatched as an
untrusted DOM event, rather than injected as real OS-level input. This removes the
class of bug where a mismatched injected press/release left a real button state
stuck down at the OS level (observed live: it made Windows route every subsequent
click, from either mouse, to whichever window held implicit capture, until the
device was unplugged and replugged).

**Migration**: No backend-level migration needed — callers that previously relied
on `backend.VirtualOutput`/`KeyOutput` now go through `synthetic-input-channel`'s
daemon-side sink instead. See that capability's spec for the replacement behavior.

The daemon SHALL inject the mouse and keyboard events the translator produces —
relative motion, button press and release, wheel, and the left-click key tap — with
ordering preserved.

#### Scenario: Pan stroke synthesis
- **WHEN** the translator opens a pan stroke
- **THEN** Ctrl is pressed, then the right button, then motion is injected
- **AND** on release the right button lifts before Ctrl, separated by the settle delay

#### Scenario: Left button clears selection
- **WHEN** `left_click_key` is `space` and the gated mouse's left button is pressed
- **THEN** a space key tap is injected and the click itself is not

#### Scenario: Wheel passthrough
- **WHEN** the gated mouse's wheel is turned while the gate is open
- **THEN** an equivalent wheel event is injected unchanged

### Requirement: Cursor query and warp
**Reason**: Cursor recentring existed only to keep a real, OS-level cursor from
running off the edge of the screen during a long pan. The gated mouse's virtual
position (see `synthetic-input-channel`) is never clamped to a screen rect — only
its on-page icon is, cosmetically — so there is no edge to recentre away from, and
nothing left calls `Pointer.warp`/`Pointer.position` for this purpose.

**Migration**: `pan_recenter` and `pan_recenter_margin_px` are no longer read from
config; an existing config file with them present is harmless. No code depends on
this requirement's removal beyond deleting the now-dead recentring path in `gate.py`.

The backend SHALL read and set the system cursor position so pan recentring works,
replacing the X11 `XQueryPointer`/`XWarpPointer` calls.

#### Scenario: Recentre near a view edge
- **WHEN** `pan_recenter` is true and the cursor comes within
  `pan_recenter_margin_px` of the reported view edge during a pan
- **THEN** the cursor is warped to the middle of the view
- **AND** the pan button is briefly lifted around the warp so the jump is not read as
  one large pan

#### Scenario: Cursor API unavailable
- **WHEN** the cursor position cannot be read
- **THEN** recentring is disabled with a logged reason and panning otherwise continues
