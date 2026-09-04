## Purpose

A local WebSocket channel from the daemon (`gate.py`) to the Chrome extension that
carries every translated gesture (rotate, pan, zoom, clear-selection) as messages,
and the extension's synthesis of those messages into untrusted DOM events dispatched
directly onto Onshape's canvas/page — replacing OS-level synthetic input injection
entirely. Includes the two on-page cursor icons that replace the native OS cursor
for the gated mouse, since nothing real is left driving it around the screen.

## Requirements

### Requirement: Local WebSocket channel from the daemon to the extension
The daemon SHALL run a minimal WebSocket server bound to `127.0.0.1` that pushes
every translated gesture to the Chrome extension, and SHALL validate the connecting
page's `Origin` header against the extension's own fixed origin before accepting
the handshake.

#### Scenario: Extension connects with the correct origin
- **WHEN** the extension's background script opens a WebSocket connection with
  `Origin: chrome-extension://<the pinned extension ID>/`
- **THEN** the daemon accepts the handshake and begins relaying gesture messages

#### Scenario: An arbitrary page attempts to connect
- **WHEN** a WebSocket handshake arrives with an `Origin` that does not match the
  pinned extension ID
- **THEN** the daemon rejects the handshake
- **AND** logs the rejected origin

#### Scenario: No client connected
- **WHEN** no extension is currently connected
- **THEN** the daemon does not start a new pan/rotate stroke, and left-click/wheel
  messages are simply not sent — the same fail-closed behavior as an unverified
  view region

### Requirement: Every translated gesture is sent as a channel message, never as OS-level injected input
The daemon SHALL NOT write to any OS-level synthetic input device (mouse or
keyboard) for translated output. Rotate/pan press, motion and release, a wheel
delta for zoom, and a key-tap event for clear-selection SHALL all be sent as
messages over the channel.

#### Scenario: Rotate or pan stroke
- **WHEN** the translator opens a pan or rotate stroke
- **THEN** a press message identifying the gesture (`pan` or `rotate`) is sent,
  followed by motion messages as the gated mouse moves, followed by a release
  message when the stroke ends
- **AND** no `BTN_RIGHT` or Ctrl event is written to any OS-level device

#### Scenario: Left button clears selection
- **WHEN** `left_click_key` is `space` and the gated mouse's left button is pressed
- **THEN** a key-tap message for Space is sent over the channel
- **AND** no key event is written to any OS-level device

#### Scenario: Wheel turn
- **WHEN** the gated mouse's wheel is turned while the gate is open
- **THEN** a wheel-delta message is sent over the channel
- **AND** no wheel event is written to any OS-level device

### Requirement: The extension synthesizes untrusted DOM events from channel messages
`content.js` SHALL translate each channel message into the matching untrusted DOM
event, dispatched directly on Onshape's canvas or document, accumulating its own
virtual cursor position for the gated mouse rather than reading or moving the real
OS cursor.

#### Scenario: Pan stroke synthesis
- **WHEN** a press message for `pan` arrives, followed by motion messages
- **THEN** a `mousedown` is dispatched on the canvas with `ctrlKey: true`, followed
  by `mousemove` events at the accumulated virtual position, also with
  `ctrlKey: true`

#### Scenario: Rotate stroke synthesis
- **WHEN** a press message for `rotate` arrives, followed by motion messages
- **THEN** a `mousedown` is dispatched on the canvas with `ctrlKey: false`, followed
  by `mousemove` events at the accumulated virtual position, also with
  `ctrlKey: false`

#### Scenario: Stroke end
- **WHEN** a release message arrives
- **THEN** a `mouseup` is dispatched at the current virtual position with the same
  `ctrlKey` value the stroke used throughout

#### Scenario: Zoom synthesis
- **WHEN** a wheel-delta message arrives
- **THEN** a synthetic `wheel` event carrying that delta is dispatched on the canvas

#### Scenario: Clear-selection synthesis
- **WHEN** a key-tap message for Space arrives
- **THEN** a synthetic `keydown` followed by `keyup` for Space is dispatched on the
  document

#### Scenario: Virtual position is never clamped to the screen
- **WHEN** the gated mouse's accumulated virtual position falls outside the
  canvas's on-screen rect
- **THEN** the dispatched event's coordinates are still used as-is — only the
  on-page icon (see below) is clamped, not the coordinates the event carries

### Requirement: Two on-page cursor icons replace the native cursor while the gate is open
While the gate is open, `content.js` SHALL hide the real OS cursor glyph over the
page and render two on-page indicators: one at the gated mouse's virtual position,
one at the other (untrusted-vs-trusted-distinguished) mouse's real position.

#### Scenario: Gate opens
- **WHEN** the gate transitions from closed to open
- **THEN** the page's cursor is hidden (`cursor: none`)
- **AND** both on-page cursor icons become visible

#### Scenario: Gate closes
- **WHEN** the gate transitions from open to closed, including mid-gesture
- **THEN** the real cursor glyph is restored
- **AND** both icons are hidden
- **AND** any open synthetic gesture is ended with a synthetic release, so
  Onshape's own drag state does not stay open with no matching release ever coming

#### Scenario: Telling the two mice apart
- **WHEN** a `mousemove` event is observed at the top level of the page
- **THEN** `event.isTrusted` distinguishes the other (real) mouse's motion, used to
  position its icon, from this extension's own synthetic dispatch
