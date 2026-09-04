## Context

Today `gate.py` reads raw events from the exclusively-captured gated mouse, runs
them through `Translator` (dead zone, idle release, `yield_stroke` arbitration —
all unchanged by this proposal), and writes the result to two real OS-level output
devices per platform: `backend.VirtualOutput` (relative motion, `BTN_RIGHT`, wheel)
and a keyboard `KeyOutput` (Ctrl, Space). Both are trusted, hardware-indistinguishable
input as far as Chrome and the OS are concerned.

That trust is also the whole problem. A prior session on this machine hit a live bug
where a released-mid-stroke, mismatched OS-level `BTN_RIGHT` press left the real
system button state stuck down, which made Windows route every subsequent click —
even from the other, ungated mouse — to the window that had implicit capture, until
the physical device was unplugged and replugged. Separately, `MODIFIER_SETTLE`,
the Ctrl-tag-on-release trick and the drag-nudge (`min_drag_px`, already removed on
this branch for the drag-nudge specifically) exist only to make a real synthetic
release survive Chrome's and Onshape's own click-vs-drag heuristics, which read
real, trusted input.

A live experiment this session (see the conversation transcript; not committed
code, since the branch it was tried on was discarded and restarted) established that
dispatching plain, untrusted `MouseEvent`s directly onto Onshape's canvas —
`canvas.dispatchEvent(...)` from a content script — drives rotate and Ctrl-tagged
pan correctly, proportionally and reversibly, with **no** `contextmenu` firing even
for drags under the old click threshold. That result is the basis for this design:
route every translated action through the browser instead of the OS.

## Goals / Non-Goals

**Goals:**
- Every translated action (rotate, pan, zoom, clear-selection) reaches Onshape as
  a content-script-dispatched DOM event, never as OS-level injected input.
- No behavior regression for the actions already validated live (rotate, pan).
- The daemon fails closed when the channel is unavailable, exactly as it already
  fails closed with no verified view region — no fallback path to maintain.
- The output side becomes identical on Linux and Windows; only capture, device
  identity and enumeration remain platform-specific.

**Non-Goals:**
- Changing gesture *recognition* — dead zone, idle release, `yield_stroke`
  arbitration, which gesture bare motion drives. `Translator`'s decision logic is
  unchanged; only what it does with a decision changes.
- Multi-tab correctness beyond what already exists (`document.hasFocus()` gates
  which tab's content script acts, unchanged from the earlier design).
- A new settings UI or config migration tool. Removed keys (`pan_recenter`,
  `pan_recenter_margin_px`) just stop being read; an old config file with them
  present is harmless.

## Decisions

**1. One sink, not a selectable one.** The earlier (discarded) design kept a
`_channel_active` flag and let a stroke pick between the channel and OS injection
per-stroke, with OS injection as fallback. This design deletes the OS-injection
path outright, so there is nothing to select between. `Translator` calls a single
`CHANNEL.send(...)` for every action; there is no `self._ui`/`self._modifier` write
left anywhere in the translated-output path.
- *Alternative considered*: keep the dual-sink structure but default it to
  channel-only. Rejected — the whole point is that the OS path's failure modes
  (the stuck-button incident) go away only if the code that can produce them is
  gone, not merely unused by default.

**2. Fail closed on a missing channel, at gesture start.** Mirroring
`_start_pan`'s existing `GATE.view_rect() is None: return`, a pan/rotate cannot
*start* while the channel is disconnected. If the channel drops **mid-stroke**
(observed live this session: the extension's MV3 service worker suspends and
reconnects repeatedly, sometimes within a few seconds), the open gesture ends
immediately — the daemon does not buffer or retry, since a resumed motion mid-drag
after a gap would read as a stray jump. Left-click and wheel are stateless
(one message, no open stroke) so they simply do nothing while disconnected, same as
a wheel turn does nothing today with no verified view region.
- *Alternative considered*: buffer motion during a brief disconnect and flush on
  reconnect. Rejected as unnecessary complexity for a gap that is normally
  sub-second, and because a flushed burst of buffered motion is itself a stray
  jump — no better than just ending the gesture.

**3. Wheel and keyboard dispatch are unvalidated and need a spike before the rest
is built.** The live experiment this session only exercised mouse events
(`mousedown`/`mousemove`/`mouseup`). Whether Onshape's zoom responds to an
untrusted synthetic `wheel` event, and whether its clear-selection shortcut
responds to untrusted synthetic `keydown`/`keyup` for Space, is **not yet known** —
some web apps deliberately gate keyboard shortcuts on `event.isTrusted` as a
lightweight defense against script-triggered actions, and wheel handling
sometimes depends on trusted deltaMode/deltaY quirks a synthetic event may not
reproduce exactly. See Open Questions and the first task in `tasks.md`.

**4. The extension's origin is pinned via a fixed `key` in `manifest.json`**,
rather than trust-on-first-use or a config-stored ID. Chrome honors a `key` field
to fix an unpacked extension's ID deterministically, so `gate.py` can validate the
WebSocket handshake's `Origin: chrome-extension://<id>/` header against a
constant baked into the daemon, with no setup-time detection step and no config
key to keep in sync.
- *Alternative considered*: read the ID from config, captured during the existing
  "load unpacked" setup step. Rejected — adds a setup step and a config key for a
  value that a manifest field can pin permanently instead.
- *Alternative considered*: trust-on-first-use (accept whatever origin connects
  first, reject any other for the rest of the session). Rejected as strictly worse
  than pinning when pinning is free.

**5. Recentring is deleted, not reimplemented against a virtual position.** The
gated mouse's virtual position (accumulated in `content.js`, dispatched with
`clientX`/`clientY` set to it) is deliberately never clamped — `dispatchEvent`
targets the canvas directly regardless of whether the coordinates nominally fall
inside its on-screen rect, so unlike a real cursor there is no edge to run out of.
Only the on-page *icon* is clamped to the view rect, for cosmetic reasons. This
removes `pan_recenter`/`pan_recenter_margin_px`, the recentre re-press path, and
`Pointer.warp`'s only remaining caller.

**6. Discovered mid-implementation: `content.js`'s context-menu *suppression* is
deleted along with everything else, not adapted.** `SUPPRESS_CONTEXT_MENU`,
`suppressionReason()` and the `preventDefault`/`stopPropagation` calls existed
solely to stop Chrome's native menu from firing off the gated mouse's own real,
OS-injected Ctrl+right-drag — the exact failure class this whole change removes.
With that gone, the mechanism has nothing left to protect against, and left in
place it becomes a straight liability: the `mousedown`/`mouseup` tracking it read
now (correctly, via the `isTrusted` guard added for the on-page cursor icons) only
ever sees the *other* mouse's genuine input, so it would suppress that mouse's own
real right-clicks — a strictly worse bug than the one it used to fix. Diagnostic
reporting (`dragPx`, `ctrl`, `target`, `onCanvas`/`inRegion`) is kept; only the
enforcement is gone. `gate.py`'s `/status` JSON drops the now-always-trivial
`suppressed`/`suppressed_why` fields accordingly — a small, deliberate break in
that JSON shape, not an oversight.

## Risks / Trade-offs

- ~~[Onshape's zoom or clear-selection may not respond to untrusted synthetic
  wheel/keyboard events at all]~~ **Resolved**: both confirmed working live —
  keyboard during the implementation spike (task 1.2), wheel during manual
  verification against real hardware (task 10.2). The scoped-back fallback (real
  OS injection for whichever failed) was never needed.
- [No OS-injection fallback means an MV3 service-worker suspension now makes the
  gated mouse fully inert, not just less smooth] → Mitigation: tighten the
  extension's reconnect behavior and `chrome.alarms` keepalive heartbeat, and make
  `/status`'s `seconds_since_extension_push`/channel-connected fields prominent in
  `--status` output so a dead channel is immediately diagnosable rather than
  read as "the mouse stopped working" with no obvious cause.
- [Relying on Chrome never raising `contextmenu` or treating synthetic dispatch
  specially is an implementation detail of Chrome/Onshape, not a contract] →
  Mitigation: none available; accepted as the trade-off for eliminating the
  OS-level failure class. Documented in the README as a known fragility tied to
  Chrome's and Onshape's own behavior, worth re-validating after major Chrome or
  Onshape updates.
- [Pinning the extension ID via `manifest.json`'s `key` field is easy to get wrong
  once (a typo silently produces a different, unpinned ID)] → Mitigation:
  `gate.py` logs the origin of every rejected handshake, so a mismatch is visible
  in the log immediately rather than presenting as "the channel never connects."

## Migration Plan

No data migration — this is a local daemon plus an unpacked extension with no
persisted state beyond the config file. Rollout is: pull the branch, reload the
extension at `chrome://extensions` (required for the `manifest.json` `key` change
and the new `content.js`/`background.js` code, same as any extension code change),
and restart the service (`setup.cmd`/`setup.sh`, which already detects code/config
drift and restarts automatically — confirmed working this session). Rollback is
`git checkout main` plus the same extension-reload-and-restart cycle, since the
change is entirely additive/removal within this repo with no external state to
unwind.

## Open Questions

- ~~Does Onshape's zoom respond to an untrusted synthetic `wheel` event dispatched
  on the canvas?~~ **Spiked 2026-09-04, inconclusive.** Tested live against
  `cad.onshape.com` (a real document, `WristMountedPhaser`) via remote browser
  automation: neither an untrusted `canvas.dispatchEvent(new WheelEvent(...))` (four
  parameter variants: plain, `ctrlKey`, `deltaMode: LINE`, and dispatched on
  `#viewerdiv`/`document`/`window` instead of the canvas) **nor** a CDP-driven,
  `isTrusted: true` synthetic scroll (the automation tool's own `scroll` action, at
  up to 20 ticks) produced any visible zoom change. A real keyboard shortcut
  (`shift+z`, zoom in) issued through the same automation channel *did* visibly
  zoom the model, confirming the page and canvas were live and responsive
  throughout — so the negative result is not an artifact of a frozen or
  disconnected session. Because the *trusted* CDP scroll failed identically to the
  untrusted one, this test could not isolate "Onshape rejects untrusted wheel
  events" from "CDP-synthesized wheel events don't carry whatever a real
  mouse/trackpad sends" — a known category of issue with WebGL/three.js-style
  wheel-zoom controls and browser-automation-driven wheel input generally,
  unrelated to `isTrusted`, which is exactly what this turned out to be.
  **Resolved 2026-09-04 (task 10.2), confirmed working**: turning the wheel on
  real gated-mouse hardware against a live Onshape tab, through the finished
  channel and `content.js`'s actual `gcWheel`, zoomed correctly — the delta
  sign/scale guessed at implementation time needed no correction. The remote
  browser automation's negative result was specifically an automation artifact,
  as suspected; untrusted dispatch itself was never the problem.
- ~~Does Onshape's clear-selection respond to untrusted synthetic
  `keydown`/`keyup` for Space dispatched on the document?~~ **Spiked 2026-09-04,
  confirmed working.** Selected a face live in the same document, dispatched
  `document.dispatchEvent(new KeyboardEvent('keydown', {key:' ', code:'Space', ...}))`
  followed by the matching `keyup`, both untrusted — the selection cleared
  immediately, both on a small vertex selection and, repeated for confirmation, on
  a large, clearly-visible face selection. This is the same clear-selection
  behavior a real Space press produces (confirmed against Onshape's own keyboard
  shortcuts panel: `space` → `Clear selection`, Onshape-controlled/locked). No
  further validation needed for this one.
- Is a fixed `manifest.json` `key` sufficient, or does Chrome's unpacked-extension
  ID assignment have an edge case (e.g., per-profile) that still needs a runtime
  fallback check?
