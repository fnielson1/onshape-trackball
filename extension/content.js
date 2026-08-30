// Reports the usable 3D view region in *screen* coordinates, so the daemon can pen
// the cursor inside it.
//
// The canvas's own rect is not good enough. Onshape stacks controls on top of the
// canvas (the panel toggle on the left, the tool strip on the right, the measurement
// bar along the bottom), and when the feature list is collapsed the canvas extends
// underneath the slide-out entirely. Those are ordinary DOM elements, so they do not
// suppress Chrome's context menu the way the canvas does — a pan whose right-button
// release lands on one opens a menu.
//
// So instead of trusting geometry, we ask the page what is actually on top:
// elementFromPoint is walked outward from the canvas centre, binary-searching the
// last point still reporting the canvas. Whatever is stacked where, this finds the
// region that genuinely belongs to the view. Measured at ~10ms on a real document.
//
// The viewport-to-screen offset is not guessed from window.outerHeight: a MouseEvent
// carries both screenX and clientX, and their difference is exactly that offset.

const MIN_CANVAS_AREA = 10000;   // ignore icon-sized canvases
const REPORT_INTERVAL = 1000;

// Sampled across each edge rather than only at the midpoint, so a control tucked
// against one end is not stepped over. Kept off the extreme corners: a single corner
// widget would otherwise shrink the whole rect across its full width.
const SAMPLES = [0.1, 0.3, 0.5, 0.7, 0.9];

let offset = null;

addEventListener("mousemove", event => {
  offset = { x: event.screenX - event.clientX, y: event.screenY - event.clientY };
}, { capture: true, passive: true });

function biggestCanvas() {
  let best = null;
  let bestArea = MIN_CANVAS_AREA;
  for (const canvas of document.querySelectorAll("canvas")) {
    const rect = canvas.getBoundingClientRect();
    const area = rect.width * rect.height;
    if (area > bestArea) {
      bestArea = area;
      best = canvas;
    }
  }
  return best;
}

// How far the canvas remains the topmost element travelling (dx, dy) from (x, y).
function reach(canvas, x, y, dx, dy, limit) {
  const on = (px, py) => document.elementFromPoint(px, py) === canvas;
  if (!on(x, y)) return 0;

  let good = 0;
  let bad = Math.max(1, Math.floor(limit));
  if (on(x + dx * bad, y + dy * bad)) return bad;

  while (bad - good > 2) {
    const mid = (good + bad) >> 1;
    if (on(x + dx * mid, y + dy * mid)) good = mid; else bad = mid;
  }
  return good;
}

function usableRect(canvas) {
  const r = canvas.getBoundingClientRect();
  const cx = Math.round(r.left + r.width / 2);
  const cy = Math.round(r.top + r.height / 2);

  // A dialog over the middle of the view leaves nothing to measure from.
  if (document.elementFromPoint(cx, cy) !== canvas) return null;

  const ys = SAMPLES.map(f => Math.round(r.top + r.height * f));
  const xs = SAMPLES.map(f => Math.round(r.left + r.width * f));

  const left = Math.min(...ys.map(y => reach(canvas, cx, y, -1, 0, cx)));
  const right = Math.min(...ys.map(y => reach(canvas, cx, y, 1, 0, innerWidth - cx)));
  const up = Math.min(...xs.map(x => reach(canvas, x, cy, 0, -1, cy)));
  const down = Math.min(...xs.map(x => reach(canvas, x, cy, 0, 1, innerHeight - cy)));

  if (left + right < 50 || up + down < 50) return null;
  return { left: cx - left, top: cy - up, width: left + right, height: up + down };
}

function canvasOwnRect(canvas) {
  const r = canvas.getBoundingClientRect();
  if (r.width < 50 || r.height < 50) return null;
  return { left: r.left, top: r.top, width: r.width, height: r.height };
}

function report() {
  const canvas = biggestCanvas();

  // Probing finds the region that is genuinely reachable — the canvas minus whatever
  // Onshape stacks on it. When it cannot (a dialog over the middle of the view), fall
  // back to the canvas's own rect rather than all the way out to the window: staying
  // inside the canvas is the whole point, and the window includes the feature tree.
  const usable = canvas && (usableRect(canvas) || canvasOwnRect(canvas));

  const rect = (usable && offset) ? {
    x: Math.round(usable.left + offset.x),
    y: Math.round(usable.top + offset.y),
    w: Math.round(usable.width),
    h: Math.round(usable.height)
  } : null;

  // Reported even when there is no rect: silence is ambiguous, and looks identical
  // to the content script not running at all.
  const diag = {
    // Bumped whenever this file changes, so it is obvious from outside the browser
    // whether Chrome is running the current script or a stale one.
    v: 4,
    canvases: document.querySelectorAll("canvas").length,
    offset: Boolean(offset),
    usable: usable ? [Math.round(usable.width), Math.round(usable.height)] : null,
    probed: Boolean(canvas && usableRect(canvas)),
    viewport: [innerWidth, innerHeight]
  };

  try {
    chrome.runtime.sendMessage({ canvas: rect, diag });
  } catch {
    // Extension reloading; the next tick will retry.
  }
}

setInterval(report, REPORT_INTERVAL);
addEventListener("resize", report);
report();
