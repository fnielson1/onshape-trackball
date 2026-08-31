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

// Suppress the menu outright rather than only reporting it. Set false to go back to
// diagnosis alone; reports carry `suppressed` either way, so nothing goes dark.
const SUPPRESS_CONTEXT_MENU = true;

// What separates a gesture from a click. The daemon guarantees at least MIN_DRAG_PX
// (12) of travel before releasing, and a hand opening a menu moves a pixel or two at
// most, so anything past this came from a drag rather than a click.
const SUPPRESS_DRAG_PX = 5;

let offset = null;
let pointerHits = new Set();
// The last region we reported, in viewport coordinates. The daemon gets it in
// screen coordinates; contextmenu events arrive in viewport ones.
let lastSafeViewport = null;
let lastPointerCheck = 0;

// Where the right button went down, and the furthest the cursor has been from it since.
// Measured from the press rather than accumulated along the path, so a stroke that
// wanders out and back still counts as the drag it was.
//
// The button state cannot simply be cleared on mouseup: Windows dispatches contextmenu
// *after* the release, so clearing there would erase the evidence a moment before it is
// needed. It is kept, with the time of the last transition, and only counted as current
// while the button is down or just after — long enough for the menu that follows a
// release, short enough that a Menu-key press minutes later is not blamed on it.
const RIGHT_BUTTON_GRACE = 500;

let rightDownAt = null;
let rightDragMax = 0;
let rightDown = false;
let lastRightActivity = 0;

addEventListener("mousedown", event => {
  if (event.button !== 2) return;
  rightDownAt = { x: event.clientX, y: event.clientY };
  rightDragMax = 0;
  rightDown = true;
  lastRightActivity = Date.now();
}, { capture: true, passive: true });

addEventListener("mouseup", event => {
  if (event.button !== 2) return;
  rightDown = false;
  lastRightActivity = Date.now();
}, { capture: true, passive: true });

addEventListener("mousemove", event => {
  offset = { x: event.screenX - event.clientX, y: event.screenY - event.clientY };

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
// event first, and that event knows everything worth knowing: where it fired, what it
// fired on, and whether anyone suppressed it. Reported so the daemon can line it up
// against what it was doing at that instant.
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

// Why this can be decided here, with no word from the daemon: the two things that mark
// a menu as ours are both on the event itself.
//
//   Ctrl is still down. The daemon releases the button first and only drops Ctrl a
//   MODIFIER_SETTLE later, so every menu raised by a pan arrives with ctrlKey set. That
//   holds even when the drag was too short to look like a drag, which is the one case
//   distance alone cannot catch.
//
//   The button was dragged. Covers the rotate half of the gesture, where Ctrl has
//   deliberately been dropped, and anything else that releases the button on the move.
//
// A right-click that is genuinely a click — no Ctrl, no travel — matches neither and is
// left completely alone, which matters because both mice share one cursor and the page
// has no way to tell them apart. Suppression keys on the shape of the gesture instead,
// and a human clicking for a menu does not make that shape.
//
// Requiring a right-button press first also leaves the keyboard routes (Menu key,
// Shift+F10) untouched: they raise a contextmenu with no mousedown before it.
function suppressionReason(event) {
  if (!SUPPRESS_CONTEXT_MENU) return null;
  if (!rightDownAt) return null;
  if (!rightDown && Date.now() - lastRightActivity > RIGHT_BUTTON_GRACE) return null;
  if (event.ctrlKey) return "ctrl held — the pan gesture still had its modifier down";
  if (rightDragMax >= SUPPRESS_DRAG_PX) {
    return `right button dragged ${Math.round(rightDragMax)}px — a gesture, not a click`;
  }
  return null;
}

addEventListener("contextmenu", event => {
  const canvas = biggestCanvas();
  const region = lastSafeViewport;
  const reason = suppressionReason(event);

  if (reason) {
    // preventDefault stops Chrome's own menu whatever else runs afterwards, since it
    // cannot be undone. stopPropagation is what stops Onshape's: its listeners sit on
    // elements below this one, so cutting the event off here means they never see it.
    event.preventDefault();
    event.stopPropagation();
  }

  const info = {
    at: Date.now(),
    x: Math.round(event.clientX),
    y: Math.round(event.clientY),
    onCanvas: Boolean(canvas && event.target === canvas),
    target: describe(event.target),
    dragPx: Math.round(rightDragMax),
    ctrl: Boolean(event.ctrlKey),
    suppressed: Boolean(reason),
    suppressedWhy: reason,
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
    // whether Chrome is running the current script or a stale one. 6 added contextmenu
    // reporting; 7 started suppressing them. Below 7 and the menus still get through.
    v: 7,
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
