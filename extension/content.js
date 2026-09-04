// Reports the usable 3D view region in *screen* coordinates, so the daemon can pen
// the cursor inside it.
//
// The canvas's own rect is not good enough. Onshape stacks controls on top of the
// canvas (the panel toggle on the left, the tool strip on the right, the measurement
// bar along the bottom, the view cube floating in a corner, a context toolbar next to
// whatever is selected), and when the feature list is collapsed the canvas extends
// underneath the slide-out entirely. Those are ordinary DOM elements, so they do not
// suppress Chrome's context menu the way the canvas does — a pan whose right-button
// release lands on one opens a menu, and a press that lands on a toolbar button
// activates it.
//
// So we ask the page what is actually on top. That happens in two stages, because the
// two halves cost wildly different amounts:
//
//   1. Discovery. elementFromPoint over a coarse grid, to find *which* elements sit
//      over the canvas. Hit testing is the expensive part, so the grid is kept
//      deliberately cheap: it only has to land on each overlay once, not trace it.
//
//   2. Solving. Each overlay found is measured with getBoundingClientRect — exact,
//      and free. The largest overlay-free rectangle is then found on a grid far finer
//      than any hit-test grid we could afford, because by that point it is pure
//      arithmetic with no DOM access at all.
//
// The previous version walked rays outward from the canvas centre and binary-searched
// the last point still reporting the canvas. That had two failure modes, both of which
// put the cursor over live UI:
//
//   - A ray whose sample row started *on* an overlay returned a reach of 0, and the
//     result was the minimum across samples — so a single sample landing on the
//     feature tree collapsed the whole probe, and it fell back to the canvas's own
//     rect: every overlay included. With the tree open that was the common case, not
//     an edge case.
//   - The search assumed the canvas was contiguous along each ray, and short-circuited
//     when the far end was canvas. An island floating over the middle of the view —
//     the view cube, a context toolbar — was stepped straight over.
//
// The viewport-to-screen offset is not guessed from window.outerHeight: a MouseEvent
// carries both screenX and clientX, and their difference is exactly that offset.

const MIN_CANVAS_AREA = 10000;   // ignore icon-sized canvases
const REPORT_INTERVAL = 1000;

// The discovery grid. Every cell is a hit test, so this is the whole cost of the probe
// and the one number worth tuning.
//
// The spacing is what sets the guarantee: an overlay is found only if a sample lands on
// it, so anything smaller than the spacing in *either* axis can slip between samples.
// 40x24 over a maximised 1920x1080 window puts a sample every 48x40 px, which covers
// what actually causes trouble — a context toolbar is around 44px tall, the view cube
// around 110px square, the feature tree and tool strip far larger. Raising these
// tightens the guarantee and costs hit tests linearly.
const DISCOVERY_COLS = 40;
const DISCOVERY_ROWS = 24;

// Resolution of the clear-rectangle search. Costs nothing per cell — obstacles are
// exact rectangles by this point — so it is set fine enough that quantisation is never
// the limiting error.
const SOLVE_CELL = 4;

// Trimmed off every edge of the result. Covers the solver's quantisation and the
// sub-pixel slop in getBoundingClientRect, so the region reported sits inside the
// clear area rather than flush against something live.
const SAFE_INSET = 8;

// A region smaller than this in either axis is not worth panning in: the daemon would
// spend the whole stroke recentring. Reporting nothing is better — it fails closed.
const MIN_SAFE = 120;

// A hit test lands on a toolbar *button*, not the toolbar. Growing the block out to the
// button's container covers the gaps between buttons, which are just as live as the
// buttons themselves. Bounded both ways so a stray walk cannot swallow the view: at
// most this many levels, and never past a parent disproportionately larger than the
// child it came from.
const BLOCK_GROW_LEVELS = 4;
const BLOCK_GROW_AREA = 6;

// Re-probing on every DOM mutation would be far too eager on a live modelling app, and
// waiting the full REPORT_INTERVAL is too slow: a toolbar that appears mid-stroke gets
// up to a second under the pointer. Coalesce mutations and probe at this rate at most.
const PROBE_MIN_INTERVAL = 250;

// The grid finds overlays down to its spacing and nothing smaller. The one point that
// has to be right whatever the size is the point the cursor is actually on — and that
// costs exactly one hit test, so it is checked directly rather than inferred.
//
// This is the difference between "the region was clear when we last swept it" and
// "there is nothing under the pointer": a small widget that no grid sample landed on,
// or one that appeared since the last sweep, is caught here the moment the cursor
// reaches it. Whatever is found is fed back in as a blocker, so the reported region
// shrinks to exclude it and the daemon recentres the cursor off it.
const POINTER_CHECK_INTERVAL = 30;

// Bounded, because it is keyed on live elements and this page runs for hours.
const MAX_POINTER_HITS = 32;

// A right-button stroke that ends with next to no net motion reads to Onshape as an
// ordinary right-click, not a drag, and it opens its own canvas context menu in
// response — see gcEndGesture and the contextmenu listener below. These bound how
// closely a contextmenu event has to follow our own synthetic release, in both space
// and time, to be confidently attributed to that release rather than to the other
// mouse's genuine right-click landing on the canvas by coincidence.
const MENU_SUPPRESS_WINDOW_MS = 250;
const MENU_SUPPRESS_TOLERANCE_PX = 3;

// A right-button release lands here or closer to its own press reads to Onshape as a
// plain click rather than a drag, which is what actually opens its canvas menu — not
// the tolerance above, which only covers the browser-level contextmenu event itself.
// gcEndGesture nudges every such release out past this distance, so Onshape never
// sees a release close enough to its press to read as a click at all, regardless of
// how little the trackball itself moved — a quick tap, or the last fragment of a
// drag right before pan_idle_release_ms cuts it off, both included.
const FORCE_DRAG_MIN_PX = 6;

let offset = null;
let pointerHits = new Set();
// The last region we reported, in viewport coordinates. The daemon gets it in
// screen coordinates; contextmenu events arrive in viewport ones.
let lastSafeViewport = null;
let lastPointerCheck = 0;

// Where the right button went down, and the furthest the cursor has been from it since
// — purely diagnostic now (the `dragPx` field on a contextmenu report below), so a
// human reading --status can tell a real drag from a real click. Measured from the
// press rather than accumulated along the path, so a stroke that wanders out and back
// still counts as the drag it was.
let rightDownAt = null;
let rightDragMax = 0;

// Where and when gcEndGesture last dispatched a synthetic right-button mouseup — see
// MENU_SUPPRESS_WINDOW_MS above and the contextmenu listener below, which use this to
// tell Onshape's own canvas menu reacting to *that* release apart from a genuine
// right-click by the other mouse.
let lastSyntheticRelease = null;

// isTrusted excludes this extension's own synthetic dispatch (see the gesture
// synthesis section near the end of this file) from all three listeners below. This
// used to matter for suppression too — Chrome could raise its own context menu from
// the gated mouse's real, OS-injected Ctrl+right-drag, and untangling "our gesture"
// from "a genuine right-click" needed exactly this kind of tracking. That mechanism is
// gone — see README's "Stopping them" section for why — but suppression itself is
// back below, keyed on lastSyntheticRelease instead: Chrome's own menu is still
// unreachable from synthetic input, but Onshape's own in-page canvas menu isn't, and
// reacts to our own dispatch as if it were a real click. What's left of this
// isTrusted-based tracking is diagnostic only.

addEventListener("mousedown", event => {
  if (!event.isTrusted || event.button !== 2) return;
  rightDownAt = { x: event.clientX, y: event.clientY };
  rightDragMax = 0;
}, { capture: true, passive: true });

addEventListener("mousemove", event => {
  if (!event.isTrusted) {
    // Not nothing, though: this is the gated mouse's own virtual position, and
    // it is what the other mouse's on-page icon needs telling apart from — see
    // gcOtherIcon below.
    return;
  }

  offset = { x: event.screenX - event.clientX, y: event.screenY - event.clientY };

  if (gcActive && gcOtherIcon) gcPositionIcon(gcOtherIcon, event.clientX, event.clientY);

  // Before the throttle below: the drag has to be measured on every move, not on the
  // one-in-thirty that the region probe cares about.
  if (rightDownAt) {
    const dx = event.clientX - rightDownAt.x;
    const dy = event.clientY - rightDownAt.y;
    const moved = Math.sqrt(dx * dx + dy * dy);
    if (moved > rightDragMax) rightDragMax = moved;
  }

  const now = Date.now();
  if (now - lastPointerCheck < POINTER_CHECK_INTERVAL) return;
  lastPointerCheck = now;

  const canvas = biggestCanvas();
  if (!canvas) return;

  const under = document.elementFromPoint(event.clientX, event.clientY);
  if (!under || under === canvas) return;
  if (pointerHits.has(under)) return;

  if (pointerHits.size >= MAX_POINTER_HITS) pointerHits.clear();
  pointerHits.add(under);
  markDirty();
}, { capture: true, passive: true });

// A context menu is browser UI, not DOM, so there is nothing to find after the fact —
// by the time you see it, what caused it is gone. But the page gets the `contextmenu`
// event first, and that event knows everything worth knowing: where it fired and what
// it fired on. Reported so the daemon can line it up against what it was doing at
// that instant — purely diagnostic now, see the note above the mousedown listener.
function describe(element) {
  if (!element) return "(none)";
  if (element.nodeType !== 1) return String(element.nodeName || "(node)");

  let out = element.tagName.toLowerCase();
  if (element.id) out += "#" + element.id;

  const cls = (element.getAttribute && element.getAttribute("class") || "").trim();
  if (cls) out += "." + cls.split(/\s+/).slice(0, 3).join(".");

  const role = element.getAttribute && element.getAttribute("role");
  if (role) out += `[role=${role}]`;
  return out.slice(0, 120);
}

addEventListener("contextmenu", event => {
  const canvas = biggestCanvas();
  const region = lastSafeViewport;
  const onCanvas = Boolean(canvas && event.target === canvas);

  // Attribute this menu to our own gesture only if it lines up, tightly, with the
  // release gcEndGesture just fired — not on gcActive/panning state alone, which
  // stays true across the whole gate session and would just as happily match the
  // other mouse's real right-click on the canvas. See MENU_SUPPRESS_WINDOW_MS.
  const ours = onCanvas && lastSyntheticRelease
    && Date.now() - lastSyntheticRelease.at <= MENU_SUPPRESS_WINDOW_MS
    && Math.abs(event.clientX - lastSyntheticRelease.x) <= MENU_SUPPRESS_TOLERANCE_PX
    && Math.abs(event.clientY - lastSyntheticRelease.y) <= MENU_SUPPRESS_TOLERANCE_PX;

  if (ours) {
    // preventDefault stops it if Onshape's own handler is gated on this event the
    // normal way; the Escape tap is the fallback for if it isn't — Onshape dispatched
    // this event itself off our synthetic mouseup, so its menu may already be
    // committed to opening regardless of what happens to the event object. Escape is
    // a near-universal "close this overlay" convention, including in Onshape's own UI.
    event.preventDefault();
    gcTapKey("esc");
  }

  const info = {
    at: Date.now(),
    x: Math.round(event.clientX),
    y: Math.round(event.clientY),
    onCanvas,
    target: describe(event.target),
    dragPx: Math.round(rightDragMax),
    ctrl: Boolean(event.ctrlKey),
    // Whether the point was inside the region we had told the daemon was safe. This is
    // what separates "our region was wrong" from "the cursor was somewhere it should
    // not have been" — two very different bugs with the same symptom.
    inRegion: region
      ? (event.clientX >= region.left && event.clientX < region.right
         && event.clientY >= region.top && event.clientY < region.bottom)
      : null,
    region: region
      ? [Math.round(region.left), Math.round(region.top),
         Math.round(region.width), Math.round(region.height)]
      : null,
  };

  // defaultPrevented is not final until every other handler has run, and Onshape's run
  // after ours. Reading it on the next task tells us what the user actually got: still
  // false here means a menu really did appear, whoever raised it.
  setTimeout(() => {
    info.prevented = event.defaultPrevented;
    try {
      chrome.runtime.sendMessage({ contextmenu: info });
    } catch {
      // Extension reloading. A diagnostic is not worth retrying for.
    }
  }, 0);
}, { capture: true });

function rect(left, top, right, bottom) {
  return { left, top, right, bottom, width: right - left, height: bottom - top };
}

function fromDom(r) {
  return rect(r.left, r.top, r.left + r.width, r.top + r.height);
}

function clip(r, area) {
  const left = Math.max(r.left, area.left);
  const top = Math.max(r.top, area.top);
  const right = Math.min(r.right, area.right);
  const bottom = Math.min(r.bottom, area.bottom);
  if (right <= left || bottom <= top) return null;
  return rect(left, top, right, bottom);
}

function biggestCanvas() {
  let best = null;
  let bestArea = MIN_CANVAS_AREA;
  for (const canvas of document.querySelectorAll("canvas")) {
    const r = canvas.getBoundingClientRect();
    const area = r.width * r.height;
    if (area > bestArea) {
      bestArea = area;
      best = canvas;
    }
  }
  return best;
}

// The area an overlay actually denies us: the element hit, grown to its container so
// the dead space between a toolbar's buttons is covered too.
function blockFor(element, canvas) {
  let node = element;
  let box = fromDom(element.getBoundingClientRect());

  for (let level = 0; level < BLOCK_GROW_LEVELS; level++) {
    const parent = node.parentElement;
    if (!parent || parent === document.body || parent === document.documentElement) break;
    // An ancestor of the canvas encloses the whole view; growing into one would block
    // everything and report no safe region at all.
    if (parent.contains(canvas)) break;

    const parentBox = fromDom(parent.getBoundingClientRect());
    if (parentBox.width <= 0 || parentBox.height <= 0) break;
    if (parentBox.width * parentBox.height
        > BLOCK_GROW_AREA * Math.max(1, box.width * box.height)) break;

    node = parent;
    box = parentBox;
  }

  return box;
}

function discoverBlocks(canvas, area) {
  const blocks = [];
  const seen = new Set();

  for (let j = 0; j < DISCOVERY_ROWS; j++) {
    const y = area.top + (j + 0.5) * area.height / DISCOVERY_ROWS;
    for (let i = 0; i < DISCOVERY_COLS; i++) {
      const x = area.left + (i + 0.5) * area.width / DISCOVERY_COLS;

      const hit = document.elementFromPoint(x, y);
      if (!hit || hit === canvas) continue;
      if (seen.has(hit)) continue;
      seen.add(hit);

      const block = clip(blockFor(hit, canvas), area);
      if (block) blocks.push(block);
    }
  }

  return blocks;
}

// Largest axis-aligned rectangle inside `area` touching none of `blocks`, by the
// standard largest-rectangle-in-histogram sweep over a rasterised grid.
function largestClearRect(area, blocks, minWidth, minHeight) {
  const cols = Math.max(1, Math.round(area.width / SOLVE_CELL));
  const rows = Math.max(1, Math.round(area.height / SOLVE_CELL));
  const cellW = area.width / cols;
  const cellH = area.height / rows;

  const blocked = new Uint8Array(cols * rows);
  for (const b of blocks) {
    const i0 = Math.max(0, Math.floor((b.left - area.left) / cellW));
    const i1 = Math.min(cols - 1, Math.ceil((b.right - area.left) / cellW) - 1);
    const j0 = Math.max(0, Math.floor((b.top - area.top) / cellH));
    const j1 = Math.min(rows - 1, Math.ceil((b.bottom - area.top) / cellH) - 1);
    for (let j = j0; j <= j1; j++) {
      const base = j * cols;
      for (let i = i0; i <= i1; i++) blocked[base + i] = 1;
    }
  }

  const minCols = Math.max(1, Math.ceil(minWidth / cellW));
  const minRows = Math.max(1, Math.ceil(minHeight / cellH));

  const heights = new Int32Array(cols);
  const stack = new Int32Array(cols + 1);
  let best = null;
  let bestArea = 0;

  for (let j = 0; j < rows; j++) {
    const base = j * cols;
    for (let i = 0; i < cols; i++) heights[i] = blocked[base + i] ? 0 : heights[i] + 1;

    let sp = 0;
    for (let i = 0; i <= cols; i++) {
      const h = i < cols ? heights[i] : 0;
      while (sp > 0 && heights[stack[sp - 1]] >= h) {
        const height = heights[stack[--sp]];
        const left = sp === 0 ? 0 : stack[sp - 1] + 1;
        const width = i - left;
        if (width >= minCols && height >= minRows && width * height > bestArea) {
          bestArea = width * height;
          best = { i0: left, i1: i - 1, j0: j - height + 1, j1: j };
        }
      }
      stack[sp++] = i;
    }
  }

  if (!best) return null;
  return rect(
    area.left + best.i0 * cellW,
    area.top + best.j0 * cellH,
    area.left + (best.i1 + 1) * cellW,
    area.top + (best.j1 + 1) * cellH,
  );
}

// The region that genuinely belongs to the 3D view, in viewport coordinates, or null
// if there is not a usable one. Null is a real answer and the daemon acts on it: no
// safe region means no panning, which is the whole point. There is deliberately no
// fall back to the canvas's own rect or to the window — both of those *are* the bug.
function safeRect(canvas) {
  const viewport = rect(0, 0, innerWidth, innerHeight);
  const area = clip(fromDom(canvas.getBoundingClientRect()), viewport);
  if (!area || area.width < MIN_SAFE || area.height < MIN_SAFE) return null;

  const blocks = discoverBlocks(canvas, area);

  // Anything the cursor has actually been on, whether or not the grid ever sampled it.
  // Re-measured every time rather than cached as a rect: these elements move, and the
  // ones that have been torn down since must stop blocking the view.
  for (const hit of Array.from(pointerHits)) {
    if (!document.contains(hit)) {
      pointerHits.delete(hit);
      continue;
    }
    const block = clip(blockFor(hit, canvas), area);
    if (block) blocks.push(block);
  }

  const clear = largestClearRect(
    area, blocks, MIN_SAFE + 2 * SAFE_INSET, MIN_SAFE + 2 * SAFE_INSET);
  if (!clear) return null;

  const safe = rect(
    clear.left + SAFE_INSET,
    clear.top + SAFE_INSET,
    clear.right - SAFE_INSET,
    clear.bottom - SAFE_INSET,
  );
  if (safe.width < MIN_SAFE || safe.height < MIN_SAFE) return null;

  return { safe, blocks: blocks.length, area };
}

let cached = { rect: null, diag: null };

function probe() {
  const canvas = biggestCanvas();
  const found = canvas ? safeRect(canvas) : null;
  const usable = found && found.safe;

  const rectOut = (usable && offset) ? {
    x: Math.round(usable.left + offset.x),
    y: Math.round(usable.top + offset.y),
    w: Math.round(usable.width),
    h: Math.round(usable.height)
  } : null;

  // Reported even when there is no rect: silence is ambiguous, and looks identical to
  // the content script not running at all.
  const diag = {
    // Bumped whenever this file changes, so it is obvious from outside the browser
    // whether Chrome is running the current script or a stale one. 6 added
    // contextmenu reporting; 7 started suppressing them; 8 dropped that suppression
    // again — nothing here is OS-level input any more, on either mouse, so there was
    // nothing left for it to protect against, and it risked eating the other mouse's
    // genuine right-clicks instead (see README's "Stopping them" section); 9
    // reintroduced it narrowly, keyed on lastSyntheticRelease rather than on
    // ctrlKey/dragPx, so it can no longer match anything but our own release; 10
    // added FORCE_DRAG_MIN_PX, which stops Onshape's canvas menu from being
    // triggered at all rather than reacting after the fact.
    v: 10,
    canvases: document.querySelectorAll("canvas").length,
    offset: Boolean(offset),
    usable: usable ? [Math.round(usable.width), Math.round(usable.height)] : null,
    blocks: found ? found.blocks : null,
    canvasArea: found ? [Math.round(found.area.width), Math.round(found.area.height)] : null,
    viewport: [innerWidth, innerHeight]
  };

  lastSafeViewport = usable || null;
  cached = { rect: rectOut, diag };
  return cached;
}

function send() {
  try {
    chrome.runtime.sendMessage({ canvas: cached.rect, diag: cached.diag });
  } catch {
    // Extension reloading; the next tick will retry.
  }
}

// The probe is the expensive thing on this page, so it runs only when the layout could
// actually have changed, and never more than once per PROBE_MIN_INTERVAL. Onshape
// mutates the DOM continuously while a model is open — reacting to each mutation, or
// even re-probing on every heartbeat, would put a hit-test sweep on the main thread
// several times a second for no gain, since the overlays sit still.
//
// The daemon's rect goes stale after CANVAS_STALE_AFTER, so the heartbeat still has to
// *send* every REPORT_INTERVAL. It just sends the cached answer when nothing moved.
let dirty = true;
let probeTimer = null;
let lastProbe = 0;

function markDirty() {
  dirty = true;
  if (probeTimer !== null) return;
  const wait = Math.max(0, PROBE_MIN_INTERVAL - (Date.now() - lastProbe));
  probeTimer = setTimeout(() => {
    probeTimer = null;
    refresh();
    send();
  }, wait);
}

function refresh() {
  if (!dirty) return;
  dirty = false;
  lastProbe = Date.now();
  probe();
}

new MutationObserver(markDirty).observe(document.documentElement, {
  childList: true,
  subtree: true,
  attributes: true,
  attributeFilter: ["class", "style", "hidden", "aria-hidden"],
});

setInterval(() => { refresh(); send(); }, REPORT_INTERVAL);
addEventListener("resize", markDirty);
addEventListener("scroll", markDirty, { capture: true, passive: true });
probe();
send();

// ===================================================================================
// Gesture synthesis: every translated action the daemon decides on — rotate, pan,
// zoom, clear-selection — arrives here as a channel message relayed through
// background.js, and is dispatched as an untrusted DOM event directly on Onshape's
// canvas or document. Nothing here is real OS-level input, and nothing here ever
// touches the real system cursor.
//
// Confirmed live: Chrome never raises its own context menu in response to synthetic
// `dispatchEvent`, even for a drag well under the old click threshold, so none of
// the ordering/settle/nudge machinery a real synthetic release used to need applies
// here — see the module docstring's history in gate.py for what that used to cost.
//
// Wheel is the one piece of this that is NOT yet confirmed against a live Onshape
// document (see design.md's Open Questions) — the delta sign and scale below are a
// reasonable guess, not a validated one.
// ===================================================================================

const GC_CURSOR_SIZE = 14;
const GC_GATED_COLOR = "#2f8fff";
const GC_OTHER_COLOR = "#ff5757";

let gcActive = false;        // is the gate open?
let gcGesture = null;        // "pan" | "rotate" | null: the currently open drag
// The gated mouse's own position, in the same viewport coordinates lastSafeViewport
// uses. Deliberately never clamped: dispatchEvent targets the canvas directly
// regardless of whether these coordinates nominally fall inside its on-screen rect,
// so unlike a real cursor there is no edge to run out of. Only the on-page *icon* is
// clamped, below, for cosmetic reasons.
let gcVirtual = null;
// Where gcBeginGesture opened the current stroke, so gcEndGesture can tell how far
// it travelled — see FORCE_DRAG_MIN_PX.
let gcPressPos = null;
let gcGatedIcon = null;
let gcOtherIcon = null;
let gcCursorNoneStyle = null;

function gcMakeIcon(color) {
  const el = document.createElement("div");
  el.style.cssText = (
    "position: fixed; left: 0; top: 0; width: " + GC_CURSOR_SIZE + "px; height: "
    + GC_CURSOR_SIZE + "px; margin-left: " + (-GC_CURSOR_SIZE / 2) + "px; margin-top: "
    + (-GC_CURSOR_SIZE / 2) + "px; border-radius: 50%; background: " + color + "; "
    + "border: 2px solid #fff; box-shadow: 0 0 2px rgba(0,0,0,0.6); "
    + "pointer-events: none; z-index: 2147483647; display: none;"
  );
  document.documentElement.appendChild(el);
  return el;
}

function gcEnsureIcons() {
  if (gcGatedIcon) return;
  gcGatedIcon = gcMakeIcon(GC_GATED_COLOR);
  gcOtherIcon = gcMakeIcon(GC_OTHER_COLOR);
}

function gcPositionIcon(el, x, y) {
  el.style.left = x + "px";
  el.style.top = y + "px";
}

function gcClampToSafeRect(x, y) {
  const region = lastSafeViewport;
  if (!region) return { x, y };
  return {
    x: Math.max(region.left, Math.min(region.right, x)),
    y: Math.max(region.top, Math.min(region.bottom, y)),
  };
}

function gcSeedPosition() {
  const region = lastSafeViewport;
  if (region) {
    return { x: region.left + region.width / 2, y: region.top + region.height / 2 };
  }
  const canvas = biggestCanvas();
  if (canvas) {
    const r = canvas.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  }
  return { x: innerWidth / 2, y: innerHeight / 2 };
}

// While the gate is open, the real cursor glyph is hidden and these two on-page
// icons stand in for it: one for the gated mouse's virtual position, one for the
// other mouse's real one. Neither mouse has ever had a real, visible cursor of its
// own before this — both shared one — so this is what lets you see both at once.
//
// Not gated on document.hasFocus() the way a gesture itself is: with more than one
// onshape.com tab open, every one of them shows the icons while the gate is open
// anywhere, not just the frontmost tab. Cosmetic only — gcBeginGesture's own check
// is what actually keeps a background tab from acting on a stroke.
function gcSetActive(active) {
  gcActive = active;
  if (active) {
    gcEnsureIcons();
    if (!gcCursorNoneStyle) {
      gcCursorNoneStyle = document.createElement("style");
      gcCursorNoneStyle.textContent = "*{cursor:none!important}";
    }
    if (!gcCursorNoneStyle.parentNode) document.documentElement.appendChild(gcCursorNoneStyle);
    if (!gcVirtual) gcVirtual = gcSeedPosition();
    const shown = gcClampToSafeRect(gcVirtual.x, gcVirtual.y);
    gcPositionIcon(gcGatedIcon, shown.x, shown.y);
    gcGatedIcon.style.display = "block";
    gcOtherIcon.style.display = "block";
  } else {
    // A clean release, not a direct reset: if a stroke was still open (the gate
    // closing mid-gesture — alt-tab, focus loss), Onshape's own drag state would
    // otherwise stay stuck open forever, with no matching mouseup ever coming.
    gcEndGesture();
    gcVirtual = null;
    if (gcCursorNoneStyle && gcCursorNoneStyle.parentNode) {
      gcCursorNoneStyle.parentNode.removeChild(gcCursorNoneStyle);
    }
    if (gcGatedIcon) gcGatedIcon.style.display = "none";
    if (gcOtherIcon) gcOtherIcon.style.display = "none";
  }
}

function gcFireMouse(canvas, type, x, y, ctrlKey) {
  canvas.dispatchEvent(new MouseEvent(type, {
    bubbles: true, cancelable: true, view: window,
    clientX: x, clientY: y, screenX: x, screenY: y,
    button: 2, buttons: type === "mouseup" ? 0 : 2, ctrlKey,
  }));
}

function gcBeginGesture(gesture) {
  // Guards against more than one onshape.com tab: background.js relays to every
  // one of them, and only the tab actually in front should act on it.
  if (!document.hasFocus()) return;
  const canvas = biggestCanvas();
  if (!canvas) return;
  if (!gcVirtual) gcVirtual = gcSeedPosition();
  gcGesture = gesture;
  gcPressPos = { x: gcVirtual.x, y: gcVirtual.y };
  gcFireMouse(canvas, "mousedown", gcVirtual.x, gcVirtual.y, gesture === "pan");
}

function gcApplyMotion(dx, dy) {
  if (!gcGesture || !gcVirtual) return;
  const canvas = biggestCanvas();
  if (!canvas) return;
  gcVirtual = { x: gcVirtual.x + (dx || 0), y: gcVirtual.y + (dy || 0) };
  gcFireMouse(canvas, "mousemove", gcVirtual.x, gcVirtual.y, gcGesture === "pan");
  if (gcGatedIcon) {
    const shown = gcClampToSafeRect(gcVirtual.x, gcVirtual.y);
    gcPositionIcon(gcGatedIcon, shown.x, shown.y);
  }
}

function gcEndGesture() {
  if (!gcGesture) return;
  const canvas = biggestCanvas();
  if (canvas && gcVirtual) {
    let releaseX = gcVirtual.x, releaseY = gcVirtual.y;

    // See FORCE_DRAG_MIN_PX. gcVirtual itself is left untouched — this only shifts
    // where the release lands, not where the next gesture picks up from, so a run of
    // near-still taps cannot walk the real position anywhere over time.
    if (gcPressPos) {
      const dx = releaseX - gcPressPos.x;
      const dy = releaseY - gcPressPos.y;
      const dist = Math.hypot(dx, dy);
      if (dist < FORCE_DRAG_MIN_PX) {
        // Nothing to preserve the direction of when there was no motion at all —
        // any fixed direction works equally well there.
        const angle = dist > 0 ? Math.atan2(dy, dx) : 0;
        releaseX = gcPressPos.x + Math.cos(angle) * FORCE_DRAG_MIN_PX;
        releaseY = gcPressPos.y + Math.sin(angle) * FORCE_DRAG_MIN_PX;
        // An actual intervening mousemove, not just a teleported mouseup — Onshape's
        // own drag tracking, like ours, may care whether motion was reported at all,
        // not only where the release ended up.
        gcFireMouse(canvas, "mousemove", releaseX, releaseY, gcGesture === "pan");
      }
    }

    gcFireMouse(canvas, "mouseup", releaseX, releaseY, gcGesture === "pan");
    lastSyntheticRelease = { x: releaseX, y: releaseY, at: Date.now() };
  }
  gcGesture = null;
  gcPressPos = null;
}

// button indices follow the DOM's own MouseEvent.button convention: 0 left, 1
// middle, 2 right (never reached here — right always goes through gcBeginGesture/
// gcApplyMotion/gcEndGesture instead), 3 back, 4 forward.
const GC_BUTTON_INDEX = { LEFT: 0, MIDDLE: 1, SIDE: 3, EXTRA: 4 };

function gcClick(code, value) {
  if (!document.hasFocus()) return;
  const canvas = biggestCanvas();
  if (!canvas || !gcVirtual) return;
  const button = GC_BUTTON_INDEX[code];
  if (button === undefined) return;
  canvas.dispatchEvent(new MouseEvent(value ? "mousedown" : "mouseup", {
    bubbles: true, cancelable: true, view: window,
    clientX: gcVirtual.x, clientY: gcVirtual.y,
    screenX: gcVirtual.x, screenY: gcVirtual.y,
    button, buttons: value ? (1 << button) : 0,
  }));
}

const GC_KEY_INFO = {
  space: { key: " ", code: "Space", keyCode: 32 },
  esc: { key: "Escape", code: "Escape", keyCode: 27 },
};

function gcTapKey(name) {
  if (!document.hasFocus()) return;
  const info = GC_KEY_INFO[name];
  if (!info) return;
  const opts = Object.assign({ bubbles: true, cancelable: true }, info);
  document.dispatchEvent(new KeyboardEvent("keydown", opts));
  document.dispatchEvent(new KeyboardEvent("keyup", opts));
}

// UNVALIDATED — see design.md's Open Questions. REL_WHEEL/REL_HWHEEL count whole
// notches; a notch is conventionally 120 units of deltaY/deltaX, which is what the
// hi-res axes already report directly. The sign below assumes the same "up/left is
// negative" convention a real wheel event uses; if zoom comes out backwards during
// manual verification (tasks.md 10.2), flip it here.
function gcWheel(code, value) {
  if (!document.hasFocus()) return;
  const canvas = biggestCanvas();
  if (!canvas || !gcVirtual) return;
  const horizontal = code === "REL_HWHEEL" || code === "REL_HWHEEL_HI_RES";
  const hiRes = code === "REL_WHEEL_HI_RES" || code === "REL_HWHEEL_HI_RES";
  const delta = hiRes ? -value : -value * 120;
  const opts = {
    bubbles: true, cancelable: true, view: window,
    clientX: gcVirtual.x, clientY: gcVirtual.y,
    screenX: gcVirtual.x, screenY: gcVirtual.y,
    deltaMode: 0,
  };
  opts[horizontal ? "deltaX" : "deltaY"] = delta;
  canvas.dispatchEvent(new WheelEvent("wheel", opts));
}

chrome.runtime.onMessage.addListener(message => {
  if (!message || !message.gate) return;
  const m = message.gate;
  if (m.type !== "motion") console.log("[gate]", m);
  if (m.type === "gate") gcSetActive(Boolean(m.open));
  else if (m.type === "press") gcBeginGesture(m.gesture);
  else if (m.type === "motion") gcApplyMotion(m.dx, m.dy);
  else if (m.type === "release") gcEndGesture();
  else if (m.type === "tap") gcTapKey(m.key);
  else if (m.type === "click") gcClick(m.code, m.value);
  else if (m.type === "wheel") gcWheel(m.code, m.value);
});
