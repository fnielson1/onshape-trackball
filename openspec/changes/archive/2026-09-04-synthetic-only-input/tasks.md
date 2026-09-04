## 1. Spike: validate the unproven synthetic events, before anything else is built

- [x] 1.1 Build a throwaway content script that dispatches an untrusted `wheel`
      event on Onshape's canvas and confirm live whether it zooms — the spike
      itself was inconclusive via remote browser automation (2026-09-04); the
      question it existed to answer was resolved instead by task 10.2's live
      hardware test, confirming untrusted wheel dispatch does zoom correctly
- [x] 1.2 Build a throwaway content script that dispatches untrusted
      `keydown`/`keyup` for Space on `document` and confirm live whether it clears
      the selection — **confirmed working live 2026-09-04**
- [x] 1.3 Record the result of both in `design.md`'s Open Questions, and if either
      failed, stop and re-scope this change back to rotate/pan only before
      continuing past this point

## 2. Extension: pin the origin

- [x] 2.1 Add a fixed `key` field to `extension/manifest.json` so the unpacked
      extension's ID is deterministic — pinned ID: `oihhifecnmdihmijdhcmlhgilbagdmod`
- [x] 2.2 Reload the extension via a prompt, and confirm `chrome://extensions` shows the expected,
      stable extension ID — **done by the user**. Confirmed indirectly but solidly:
      `gate.log` shows `channel: extension connected` right after the daemon
      restarted, which only happens if the handshake's Origin matched the pinned
      ID — a mismatch logs `channel handshake failed` and closes the connection
      instead (see `Channel._handshake` in gate.py). `/status` now reports
      `channel_connected: true`

## 3. `gate.py`: the WebSocket channel

- [x] 3.1 Implement a minimal hand-rolled WebSocket server (HTTP Upgrade handshake,
      text frames, client-frame unmasking, ping/pong, close) bound to `127.0.0.1`
- [x] 3.2 Validate the handshake's `Origin` header against the extension ID pinned
      in step 2.1; reject and log any mismatch
- [x] 3.3 Expose a `connected` check and a `send(message)` method for the
      translator to use as its one output sink
- [x] 3.4 Surface channel state (connected/disconnected, last message age) in the
      `/status` JSON, prominently enough that a dead channel is easy to diagnose

## 4. `gate.py`: rip out OS-level output entirely

- [x] 4.1 Replace every `self._ui.write*`/`self._ui.syn` call in `Translator` with
      a channel message: press/motion/release for pan and rotate, a key-tap message
      for the left button, a wheel-delta message for the wheel
- [x] 4.2 Delete `MODIFIER_SETTLE`, the Ctrl-tag-on-release trick, and all
      `_ctrl`/`_tap_key` OS-device writes
- [x] 4.3 Delete the recentring path entirely: `_ensure_inside_window`'s
      edge-recentre branch, the recentre re-press logic, `presses_recentred`, and
      `resolve_recenter`/`pan_recenter`/`pan_recenter_margin_px` — found and fixed a
      real regression along the way: the deleted method also carried an unrelated
      safety check (release the stroke if the safe view region disappears
      mid-gesture), re-added directly in the motion handler
- [x] 4.4 Gate pan/rotate start on channel connectivity, mirroring the existing
      `GATE.view_rect() is None: return` check in `_start_pan`
- [x] 4.5 End any open stroke immediately if the channel disconnects mid-gesture,
      rather than buffering or retrying
- [x] 4.6 Remove the now-pointless dual-sink selection machinery inherited from the
      earlier design (there is exactly one sink)
- [x] 4.7 Update `main()`'s startup log line and `/status` output to drop every
      OS-injection-specific field that no longer applies

## 5. Backends: keep capture, drop output

- [x] 5.1 Remove `VirtualOutput` and `modifier_output`/`KeyOutput` from
      `backend_windows.py` and `backend_linux.py` — nothing calls them once step 4
      is done
- [x] 5.2 Remove `Pointer`/`Pointer.warp`/`Pointer.position` entirely from both
      backends — nothing calls them after recentring is gone; kept
      `GatedDevice`/exclusive capture, hardware-ID resolution, and mouse
      enumeration/detection untouched, including the dead `.template` attribute's
      removal since it existed only to construct the old `VirtualOutput`
- [x] 5.3 Confirm `backend.py`'s documented interface reflects the smaller surface

## 6. `extension/background.js`: the channel connection

- [x] 6.1 Open and maintain a WebSocket connection to `127.0.0.1` on the channel
      port, reconnecting on drop
- [x] 6.2 Relay every channel message to the active tab's content script — relays
      to every onshape.com tab, matching the existing canvas-rect-report pattern;
      each tab's own `document.hasFocus()` check decides which one actually acts
- [x] 6.3 Tighten the existing `chrome.alarms` heartbeat so an idle service worker
      reconnects promptly after MV3 suspends it — this channel has no OS-level
      fallback left, so a slow reconnect now means a fully inert mouse. Implemented
      as opportunistic reconnect attempts on every existing wake trigger (tab
      activation, focus change, and — the tightest in practice — content.js's own
      once-a-second `onMessage` traffic), not just the 30s alarm itself

## 7. `extension/content.js`: gesture synthesis

- [x] 7.1 Accumulate the gated mouse's own virtual position (unclamped) from motion
      messages, seeded at the safe-viewport centre
- [x] 7.2 Dispatch `mousedown`/`mousemove`/`mouseup` on the canvas for pan (with
      `ctrlKey: true`) and rotate (`ctrlKey: false`), per the spike's confirmed
      approach from the earlier (discarded) session
- [x] 7.3 Dispatch a synthetic `wheel` event on the canvas for a wheel-delta message
      — **delta sign/scale unvalidated, see design.md's Open Questions**
- [x] 7.4 Dispatch synthetic `keydown`/`keyup` for Space on `document` for a
      key-tap message — confirmed working live against a real Onshape document
- [x] 7.5 Add `cursor: none` (toggled with the gate) and two on-page cursor icons:
      one at the gated mouse's virtual position, one at the other mouse's real
      position via `event.isTrusted` on the existing top-level `mousemove` listener
      — also had to guard the existing `mousedown`/`mouseup` right-button-tracking
      listeners with `isTrusted` (they would otherwise track our own synthetic
      strokes too, which cannot raise a real context menu and would only risk
      mis-attributing a genuine one), and updated `test_probe.js`'s simulated
      events to mark themselves `isTrusted: true` accordingly
- [x] 7.6 End any open synthetic gesture and restore the real cursor when the gate
      closes mid-stroke, so Onshape's own drag state cannot stay open forever

## 8. Config and documentation

- [x] 8.1 Stop generating `pan_recenter`/`pan_recenter_margin_px` in
      `setup_helper.py`'s config template; leave old configs with them present
      untouched (harmless, just unread) — did the same in `setup.sh`, its Linux
      counterpart, for parity, since both platforms share this daemon behavior now
- [x] 8.2 Rewrite README's "How it works" section around the channel as the
      primary output path, removing the OS-injection description — also added a
      "Two cursors" subsection, rewrote the Linux/Windows differences table
      (output/cursor rows no longer platform-specific), and rewrote the
      `backend_linux.py`/`backend_windows.py` module docstrings the same way
- [x] 8.3 Update the config table and troubleshooting table: remove recentring and
      nudge-related rows, add the channel's connected/disconnected states —
      **found and fixed a real regression along the way**: `content.js`'s
      context-menu *suppression* (`SUPPRESS_CONTEXT_MENU`/`suppressionReason`)
      existed only to stop Chrome's native menu from firing off the gated mouse's
      own real OS-injected drag, which can no longer happen; left in place it would
      have suppressed the *other* mouse's genuine right-clicks instead, since the
      `isTrusted`-guarded tracking it read now only ever sees that mouse's real
      input. Removed the suppression (kept the diagnostic reporting), rewrote
      README's "Stopping them" section accordingly, bumped `content.js`'s
      `canvas_diag.v` to 8, and updated `gate.py`'s `record_context_menu` and
      `test_probe.js` to match (see design.md decision 6)
- [x] 8.4 Document the fail-closed behavior change explicitly: no channel means no
      pan/rotate/zoom/clear-selection at all, not degraded cursor-sharing

## 9. Tests

- [x] 9.1 Rewrite `test_translator.py`'s `StubUI`-based assertions to assert on
      channel messages instead, for every existing pan/rotate/left-click/wheel case
      — also added direct coverage for the new wire-protocol helpers (frame
      round-trip, client-frame unmasking, handshake origin accept/reject) since
      `StubChannel` never exercises the real socket code
- [x] 9.2 Delete recentring-specific tests entirely
- [x] 9.3 Add coverage for: gesture start refused with no channel connected, and an
      open stroke ending immediately when the channel disconnects mid-gesture
- [x] 9.4 Confirm `test_config.py` still passes with `pan_recenter`/
      `pan_recenter_margin_px` resolvers removed — 31/31, `resolve_recenter` and its
      tests deleted

## 10. Manual verification (not unit-testable headlessly)

- [x] 10.1 Verify rotate and pan against a live Onshape tab, both directions,
      confirming no `contextmenu` fires — confirmed by the user
- [x] 10.2 Verify zoom via the wheel-delta channel path — confirmed by the user;
      the sign/scale guessed in `content.js`'s `gcWheel` was correct as written,
      resolving design.md's wheel Open Question
- [x] 10.3 Verify clear-selection via the key-tap channel path — confirmed by the
      user
- [x] 10.4 Verify the gated mouse is fully inert with the extension not loaded, and
      recovers cleanly once it is — confirmed by the user
- [x] 10.5 Verify killing the daemon mid-stroke leaves nothing stuck — no held
      button state anywhere, since none is ever injected at the OS level now.
      Live-tested this session: force-killed the daemon process (`Stop-Process
      -Force`, no graceful shutdown) mid-gesture. `right_button_down: false` on
      the next status check, and the Windows Scheduled Task's restart-on-failure
      brought the daemon back up on its own within seconds — but this surfaced a
      real bug, since fixed: `background.js`'s WebSocket `onclose` handler
      cleared its own connection state but never told `content.js` the channel
      had dropped, so a page-side gesture killed mid-stroke had no way to learn
      the daemon was gone and clean itself up (end the drag, restore the real
      cursor, hide the icons) until the *next* explicit "gate" message happened
      to arrive. Fixed by relaying a synthetic `{type: "gate", open: false}` on
      every `onclose`, matching what a graceful `release_all()` already sends
- [x] 10.6 Verify the two on-page cursor icons correctly track each mouse during
      simultaneous use — confirmed by the user
