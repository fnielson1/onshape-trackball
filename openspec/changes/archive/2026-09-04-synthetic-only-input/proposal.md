## Why

The daemon currently drives Onshape by injecting *real*, trusted, OS-level input on
behalf of the gated mouse — synthetic hardware events via `uinput` on Linux and the
Interception driver's `send` on Windows — for every translated gesture: rotate/pan
motion and the right button that drags them, the left button's space-tap, and the
wheel's zoom. Because these are indistinguishable from real hardware to the OS and
to Chrome, they carry all of real hardware's failure modes: a release that never
lands leaves a button state stuck open at the OS level (observed live — see the
"can't click anything outside the browser" incident this session, caused by exactly
this), Chrome's and Onshape's own click-vs-drag heuristics have to be fought with
`MODIFIER_SETTLE` timing, a Ctrl-tag-on-release trick and a drag-nudge, and the two
mice are forced to share one real, visible cursor that the gated mouse's own
rotate/pan strokes drag around unpredictably.

Driving Onshape's canvas directly with untrusted, page-dispatched `MouseEvent`s
(confirmed live this session: plain and Ctrl-tagged synthetic events reproduce
rotate and pan correctly, proportionally and reversibly, and Chrome never raises its
own `contextmenu` in response to synthetic dispatch, even for drags well under the
old click threshold) sidesteps that whole failure class rather than working around
it. This proposal makes that the *only* way the daemon's translated output reaches
Onshape — not an opt-in extra with a fallback to the old path, since the old path is
exactly what this removes.

## What Changes

- **BREAKING**: the daemon stops injecting real OS-level input for anything the
  translator produces. `backend.VirtualOutput` (relative motion, button press/release,
  wheel) and the Ctrl/space `KeyOutput` are no longer used for output on either
  platform. The gated mouse's raw input is still captured exclusively (so it never
  leaks to the desktop), but everything the translator decides to do with it —
  rotate, pan, zoom, clear-selection — now goes out over a new local channel to the
  Chrome extension instead, which dispatches the matching untrusted DOM events
  directly onto Onshape's canvas/page.
- **BREAKING**: no fallback to OS-level injection. If the channel is not connected —
  extension not loaded, its service worker suspended, Chrome not focused — the gate
  simply does not act, the same way it already refuses to act with no verified view
  region. This is a straight extension of the project's existing fail-closed
  philosophy, not a new exception to it.
- **New local channel, `gate.py` → the extension.** A minimal, hand-rolled WebSocket
  server in `gate.py` (no new dependency, matching the project's existing posture)
  pushes every translated gesture to the extension in real time: rotate/pan
  press/motion/release (with the pan/rotate distinction carried explicitly, not
  inferred from a real Ctrl key), a wheel delta for zoom, and a key-tap event for
  the left button's clear-selection. `background.js` holds the connection (the
  content script's page CSP blocks `localhost` directly) and relays it to the active
  tab's content script.
- **`content.js` synthesizes every gesture.** Rotate/pan become a
  `mousedown`/`mousemove`/`mouseup` sequence dispatched on the canvas, with
  `ctrlKey` set for pan. Zoom becomes a synthetic `wheel` event on the canvas.
  Clear-selection becomes a synthetic `keydown`/`keyup` for Space on the document,
  matching what a real space press already does today.
- **Two on-page cursor icons replace the native one for the gated mouse.** With
  nothing left touching the real OS cursor, the gated mouse has no on-screen
  representation unless the page draws one. While the gate is open, `content.js`
  hides the real cursor glyph (`cursor: none`) and renders two small custom cursors:
  one at the gated mouse's own virtual position (driven by the channel), one at the
  other mouse's real position (`content.js`'s existing top-level `mousemove`
  listener already sees this; `event.isTrusted` tells the two apart, since
  everything this change dispatches is untrusted by construction).
- **Cursor recentring is removed, not reimplemented.** It existed only because a
  long pan dragged a *real* cursor to the edge of the screen. The gated mouse's
  virtual position is never clamped to a screen rect — only the on-page icon is, for
  cosmetic reasons — so nothing needs recentring, and `pan_recenter` /
  `pan_recenter_margin_px` and the recentre re-press machinery go away with it.
  `MODIFIER_SETTLE`, the Ctrl-tag-on-release trick and the drag-nudge go with it too:
  none of Chrome's or Onshape's click-vs-drag heuristics apply to untrusted synthetic
  dispatch, confirmed live this session.
- **The feature becomes fully platform-neutral on the output side.** `backend_linux.py`
  and `backend_windows.py` keep exclusive capture, stable device identity and mouse
  enumeration (still genuinely platform-specific), but lose their output-injection
  code entirely — there is no more per-platform difference in how a translated
  gesture reaches Onshape.

## Capabilities

### New Capabilities
- `synthetic-input-channel`: the local WebSocket channel from `gate.py` to the
  Chrome extension, and the extension's synthesis of every translated gesture
  (rotate, pan, zoom, clear-selection) as untrusted DOM events dispatched directly
  onto the Onshape page, including the two on-page cursor icons that replace the
  native cursor for the gated mouse.

### Modified Capabilities
- `windows-input-backend`: the "Synthetic input injection" and "Cursor query and
  warp" requirements are removed — the backend no longer injects translated output
  or manages the real cursor at all. "Exclusive capture of the gated mouse", "Stable
  device identity" and "Mouse enumeration" are retained, with the injection-specific
  scenario under exclusive capture ("the translated events are injected as synthetic
  input... Onshape receives them as ordinary mouse input") replaced to describe the
  channel instead.

## Impact

- **Code**:
  - `gate.py`: new WebSocket server and channel sink; `Translator` no longer writes
    to `self._ui`/`self._modifier` at all — every press/motion/release/tap/wheel
    action is a channel message. Removes `MODIFIER_SETTLE`, the Ctrl-tag-on-release
    trick, recentring (`_ensure_inside_window`'s edge-recentre path,
    `pan_recenter`/`pan_recenter_margin_px` resolvers), and the now-pointless
    dual-sink selection logic (there is only one sink).
  - `backend_linux.py` / `backend_windows.py`: remove `VirtualOutput`,
    `modifier_output`/`KeyOutput`, and `Pointer`'s cursor query/warp (no longer
    called by anything). Keep `GatedDevice`/exclusive capture, hardware-ID
    resolution, and mouse enumeration/detection unchanged.
  - `extension/background.js`: opens and maintains the WebSocket connection to
    `gate.py` and relays messages to the active tab's content script.
  - `extension/content.js`: gesture synthesis for all four translated actions, the
    canvas-clamped virtual-position accumulator for the gated mouse, the two custom
    cursor icons, and the `cursor: none` toggle.
  - `README.md`: the "How it works" section, the config table (`pan_recenter`,
    `pan_recenter_margin_px` removed), and the troubleshooting table (recentring and
    nudge-related rows removed; the channel becomes as central to "how it works" as
    exclusive capture already is) need a rewrite, not a patch.
- **Dependencies**: still none new. Same "hand-roll enough of RFC 6455 for one
  trusted local client" posture already used for the HTTP status server.
- **Security**: the WebSocket server binds to `127.0.0.1` only and validates the
  handshake's `Origin` header against the extension's own origin
  (`chrome-extension://<id>/`), so an arbitrary web page cannot open it and observe
  the gated mouse's raw motion.
- **Risk**: the Chrome extension's MV3 service-worker lifecycle is the sharpest new
  edge — an idle service worker can be suspended, dropping the channel. Because
  there is no fallback left, that now means the gated mouse goes inert (fails
  closed) rather than losing only the cursor-separation benefit, which is a real
  behavior change from the earlier (never-shipped) opt-in design and needs to be
  called out clearly in the README.
- **Tests**: `test_translator.py`'s existing gesture-recognition cases need their
  assertions rewritten against channel messages instead of `StubUI`/`StubKeyOutput`
  writes — the state machine itself (dead zone, idle release, yield arbitration)
  is unchanged and should not need new cases. All recentring-specific tests are
  removed along with the feature. The JS-side gesture synthesis and rendering are
  not unit-testable headlessly, as before — verification is manual, against a live
  Onshape tab.
- **Unaffected**: the translator's gesture-recognition logic itself (dead zone,
  idle release, `yield_stroke` arbitration, which gesture bare motion drives), focus
  tracking, the canvas-rect probe, and every config key not named above.
