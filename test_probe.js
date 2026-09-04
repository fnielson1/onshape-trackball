// Drives extension/content.js's view probe against simulated Onshape layouts.
//
// The probe's one job is a promise: every point of the rect it reports belongs to the
// canvas and nothing else. So rather than assert on particular coordinates — which
// would just restate the implementation — each case rebuilds the layout, takes the
// rect the probe returns, and hit-tests every point of it back through the same
// layout. Any point that is not the canvas is a point where a real pan would press a
// real button.
//
// Run with: node test_probe.js

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const CONTENT = path.join(__dirname, "extension", "content.js");

const VIEWPORT = { w: 1920, h: 1080 };

// The canvas runs the full width of the window, *under* the left slide-out. That is
// exactly why its own rect is not a safe answer.
const CANVAS = { left: 0, top: 60, width: 1920, height: 960 };

// Onshape's furniture, as it actually sits: some anchored to the edges of the view,
// some floating free over the middle of it.
const FEATURE_TREE = { name: "feature-tree", left: 0, top: 60, width: 260, height: 960 };
const TOOL_STRIP = { name: "tool-strip", left: 1860, top: 60, width: 60, height: 960 };
const MEASURE_BAR = { name: "measure-bar", left: 0, top: 990, width: 1920, height: 30 };
const VIEW_CUBE = { name: "view-cube", left: 1700, top: 110, width: 110, height: 110 };
const CONTEXT_BAR = { name: "context-toolbar", left: 1100, top: 400, width: 240, height: 44 };
const CENTRE_POPOVER = { name: "popover", left: 1300, top: 500, width: 200, height: 90 };

function buildLayout(overlays) {
  // One flat element per overlay, each its own parent, so blockFor's container walk
  // has somewhere to stop. Mirrors a toolbar button inside a toolbar.
  const canvasEl = {
    tag: "canvas",
    parentElement: null,
    contains: other => other === canvasEl,
    getBoundingClientRect: () => ({
      left: CANVAS.left, top: CANVAS.top, width: CANVAS.width, height: CANVAS.height,
    }),
  };

  const elements = overlays.map(o => {
    const el = {
      tag: o.name,
      parentElement: null,
      contains: other => other === el,
      getBoundingClientRect: () => ({
        left: o.left, top: o.top, width: o.width, height: o.height,
      }),
    };
    return el;
  });

  function elementFromPoint(x, y) {
    if (x < 0 || y < 0 || x >= VIEWPORT.w || y >= VIEWPORT.h) return null;
    for (let i = 0; i < overlays.length; i++) {
      const o = overlays[i];
      if (x >= o.left && x < o.left + o.width && y >= o.top && y < o.top + o.height) {
        return elements[i];
      }
    }
    if (x >= CANVAS.left && x < CANVAS.left + CANVAS.width
        && y >= CANVAS.top && y < CANVAS.top + CANVAS.height) {
      return canvasEl;
    }
    return null;              // page background, outside the canvas
  }

  const body = { tag: "body" };
  const documentElement = { tag: "html" };

  const document = {
    body,
    documentElement,
    elementFromPoint,
    querySelectorAll: () => [canvasEl],
    // Every simulated element is live for the life of the case; nothing is torn down.
    contains: () => true,
  };

  return { document, canvasEl, elementFromPoint };
}

function loadProbe(layout, listeners = {}, clock = null) {
  const sandbox = {
    document: layout.document,
    innerWidth: VIEWPORT.w,
    innerHeight: VIEWPORT.h,
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    setInterval() {},
    setTimeout() {},
    clearTimeout() {},
    // A settable clock, so the grace window after a button release can be tested
    // without the suite sleeping through it.
    Date: clock ? { now: () => clock.t } : Date,
    MutationObserver: class { observe() {} disconnect() {} },
    chrome: { runtime: { sendMessage() {}, onMessage: { addListener() {} } } },
    console,
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);

  const source = fs.readFileSync(CONTENT, "utf8")
    + "\n;globalThis.__probe = { safeRect, largestClearRect, biggestCanvas };";
  vm.runInContext(source, sandbox);
  return sandbox.__probe;
}

// Every point of `rect` must hit the canvas. Steps by 2px, plus the exact edges,
// because the edges are where an off-by-one puts the cursor on a toolbar.
function audit(rect, layout) {
  const bad = [];
  if (!rect) return bad;

  const xs = [];
  for (let x = rect.left; x < rect.right; x += 2) xs.push(x);
  xs.push(rect.left, rect.right - 0.01);

  const ys = [];
  for (let y = rect.top; y < rect.bottom; y += 2) ys.push(y);
  ys.push(rect.top, rect.bottom - 0.01);

  for (const y of ys) {
    for (const x of xs) {
      if (layout.elementFromPoint(x, y) !== layout.canvasEl) {
        bad.push({ x: Math.round(x), y: Math.round(y) });
        if (bad.length > 4) return bad;
      }
    }
  }
  return bad;
}

const CASES = [
  {
    name: "a clear view reports almost all of the canvas",
    overlays: [],
    expect: r => r && r.width > 1800 && r.height > 900,
    why: "nothing is in the way, so the region should be the canvas less the inset",
  },
  {
    name: "edge chrome is excluded",
    overlays: [FEATURE_TREE, TOOL_STRIP, MEASURE_BAR],
    expect: r => r && r.left >= 260 && r.right <= 1860 && r.bottom <= 990,
    why: "the feature tree, tool strip and measurement bar all sit over the canvas",
  },
  {
    name: "a floating view cube is excluded",
    overlays: [FEATURE_TREE, VIEW_CUBE],
    expect: r => r,
    why: "an island the old ray probe stepped straight over",
  },
  {
    name: "a context toolbar over the middle of the view is excluded",
    overlays: [FEATURE_TREE, CONTEXT_BAR],
    expect: r => r,
    why: "the old probe short-circuited past it whenever the far end was canvas",
  },
  {
    name: "an island straddling the centre row is excluded",
    overlays: [FEATURE_TREE, CENTRE_POPOVER],
    expect: r => r,
    why: "sits directly on the ray the old probe measured from",
  },
  {
    name: "the whole lot at once",
    overlays: [FEATURE_TREE, TOOL_STRIP, MEASURE_BAR, VIEW_CUBE, CONTEXT_BAR],
    expect: r => r,
    why: "a realistic document: edge chrome and floating overlays together",
  },
  {
    name: "a modal over the view reports nothing rather than guessing",
    overlays: [{ name: "modal", left: 0, top: 60, width: 1920, height: 960 }],
    expect: r => r === null,
    why: "no safe region exists, and failing closed is the point",
  },
  {
    name: "a nearly-covered view reports nothing rather than a sliver",
    overlays: [{ name: "modal", left: 0, top: 60, width: 1920, height: 900 }],
    expect: r => r === null,
    why: "what is left is too thin to pan in; MIN_SAFE rejects it",
  },
];

let failures = 0;
let checks = 0;

for (const c of CASES) {
  checks++;
  const layout = buildLayout(c.overlays);
  const probe = loadProbe(layout);

  const found = probe.safeRect(layout.canvasEl);
  const r = found ? found.safe : null;

  const shown = r
    ? `left=${Math.round(r.left)} top=${Math.round(r.top)} ` +
      `right=${Math.round(r.right)} bottom=${Math.round(r.bottom)} ` +
      `(${Math.round(r.width)}x${Math.round(r.height)})`
    : "null";

  const bad = audit(r, layout);
  const shapeOk = Boolean(c.expect(r));

  if (bad.length) {
    failures++;
    const p = bad.map(b => `(${b.x},${b.y})`).join(" ");
    console.log(`FAIL  ${c.name}`);
    console.log(`        rect ${shown}`);
    console.log(`        not canvas at ${p}${bad.length > 4 ? " ..." : ""}`);
  } else if (!shapeOk) {
    failures++;
    console.log(`FAIL  ${c.name}`);
    console.log(`        rect ${shown}`);
    console.log(`        ${c.why}`);
  } else {
    console.log(`PASS  ${c.name}`);
    console.log(`        ${shown}`);
  }
}

// A widget smaller than the discovery spacing is the one thing the grid cannot promise
// to find — so the cursor landing on it has to be what catches it. This is the literal
// requirement: whatever the sweep did or did not see, nothing may be under the pointer
// but canvas.
{
  // 20px, well under the ~48x40 grid spacing, and deliberately parked in a gap: over
  // this canvas the samples land on x = 24 + 48i and y = 80 + 40j, so 990..1010 by
  // 650..670 has no sample anywhere in it. The grid cannot see this widget at all.
  const WIDGET = { name: "tiny-widget", left: 990, top: 650, width: 20, height: 20 };
  const layout = buildLayout([FEATURE_TREE, WIDGET]);
  const listeners = {};
  const probe = loadProbe(layout, listeners);

  const before = probe.safeRect(layout.canvasEl);
  const covered = r => r && r.left < WIDGET.left + WIDGET.width && r.right > WIDGET.left
                        && r.top < WIDGET.top + WIDGET.height && r.bottom > WIDGET.top;
  const missedByGrid = covered(before && before.safe);

  // The cursor arrives on it. clientX/clientY are what the page sees; screenX/screenY
  // only matter for the offset, so any consistent pair will do.
  const move = listeners.mousemove && listeners.mousemove[0];
  if (move) {
    move({ isTrusted: true, clientX: WIDGET.left + 10, clientY: WIDGET.top + 10,
           screenX: WIDGET.left + 10, screenY: WIDGET.top + 70 });
  }

  checks++;
  const after = probe.safeRect(layout.canvasEl);
  const stillCovered = covered(after && after.safe);

  if (!move) {
    failures++;
    console.log("FAIL  no mousemove listener registered; pointer check is not wired up");
  } else if (stillCovered) {
    failures++;
    console.log("FAIL  a widget under the pointer is still inside the reported region");
    console.log(`        region ${after ? `${Math.round(after.safe.width)}x${Math.round(after.safe.height)}` : "null"}`);
  } else {
    const note = missedByGrid
      ? "grid missed it, the pointer check caught it"
      : "grid happened to sample it";
    console.log(`PASS  a widget under the pointer is excluded (${note})`);
  }
}

// The solver itself, on a case small enough to check by eye: a single block dead
// centre must yield one of the four surrounding bands, not a rect spanning the block.
{
  const probe = loadProbe(buildLayout([]));
  const area = { left: 0, top: 0, right: 100, bottom: 100, width: 100, height: 100 };
  const block = { left: 40, top: 0, right: 60, bottom: 100, width: 20, height: 100 };
  checks++;
  const clear = probe.largestClearRect(area, [block], 1, 1);
  const spansBlock = clear && clear.left < 60 && clear.right > 40;
  if (spansBlock) {
    failures++;
    console.log("FAIL  solver spans a block it should have avoided");
  } else {
    console.log("PASS  solver avoids a block splitting the area");
  }
}

console.log();
console.log(failures ? `${failures} FAILED of ${checks}` : `${checks}/${checks} passed`);
process.exit(failures ? 1 : 0);
