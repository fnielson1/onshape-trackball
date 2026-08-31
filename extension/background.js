// Reports to the local daemon whether the frontmost Chrome window's active tab is
// on onshape.com.
//
// Deliberately says NOTHING about whether Chrome itself is focused. The daemon
// tracks that from X11, event-driven and race-free. An earlier version also tested
// window.focused here, which meant alt-tabbing back into Chrome could latch a stale
// "false" until the next 30s heartbeat — with the gate failing closed, the mouse was
// dead in between.
//
// Pushes on every real transition; the alarm is only a liveness heartbeat so the
// daemon can fail closed if this extension stops running.

const ENDPOINT = "http://127.0.0.1:47653/state";
const ONSHAPE_HOST = /(^|\.)onshape\.com$/i;

async function activeTabIsOnshape(windowId) {
  try {
    // Prefer the id the event handed us: querying "last focused" during a focus
    // transition is exactly where the old race lived.
    if (windowId === undefined || windowId === chrome.windows.WINDOW_ID_NONE) {
      const win = await chrome.windows.getLastFocused();
      if (!win) return false;
      windowId = win.id;
    }

    const tabs = await chrome.tabs.query({ active: true, windowId });
    const tab = tabs && tabs[0];
    if (!tab || !tab.url) return false;

    return ONSHAPE_HOST.test(new URL(tab.url).hostname);
  } catch {
    return false;
  }
}

// Latest canvas rect from the content script, in screen coordinates. Relayed through
// here because a content script's fetch is subject to the page's CSP, which would
// block a call to localhost; the service worker is not.
let canvasRect = null;
let canvasDiag = null;

// MV3 suspends the service worker freely and its module state does not survive, so
// the rect has to be parked somewhere durable or it is lost on every restart —
// leaving the daemon on the whole-window fallback until the content script next
// reports, which can be a while when Chrome throttles timers in a background tab.
chrome.storage.session.get(["canvasRect", "canvasDiag"]).then(stored => {
  if (canvasRect === null && stored.canvasRect) canvasRect = stored.canvasRect;
  if (canvasDiag === null && stored.canvasDiag) canvasDiag = stored.canvasDiag;
}).catch(() => {});

// Context-menu reports ride along on the next push and are then dropped. They are
// diagnostics, so none of this is worth persisting or retrying: the daemon logs each
// one as it arrives, and a report lost to a service-worker restart is a report about an
// event the user already saw happen.
let pendingContextMenus = [];

chrome.runtime.onMessage.addListener((message, sender) => {
  if (!message) return;

  if (message.contextmenu) {
    pendingContextMenus.push(message.contextmenu);
    if (pendingContextMenus.length > 20) pendingContextMenus.shift();
    // Pushed immediately rather than on the next heartbeat: the daemon lines these up
    // against what it was doing at the time, and that has moved on within a second.
    push(sender.tab && sender.tab.windowId);
    return;
  }

  if (!("canvas" in message)) return;
  canvasRect = message.canvas;
  if (message.diag) canvasDiag = message.diag;
  chrome.storage.session.set({ canvasRect, canvasDiag }).catch(() => {});
  push(sender.tab && sender.tab.windowId);
});

async function push(windowId) {
  const onshape = await activeTabIsOnshape(windowId);
  // Claimed before the await below, so a second push cannot send them twice.
  const contextmenu = pendingContextMenus;
  pendingContextMenus = [];
  try {
    await fetch(ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        onshape,
        // Sent regardless of `onshape`. Blanking it whenever the frontmost tab is
        // not Onshape threw away a perfectly good rect every time you alt-tabbed,
        // and the content script only refreshes it once a second — and not at all
        // while Chrome throttles timers in a background tab.
        canvas: canvasRect,
        diag: canvasDiag,
        contextmenu
      })
    });
  } catch {
    // Daemon not running. Nothing to do; it fails closed on its own.
  }
}

chrome.tabs.onActivated.addListener(info => push(info.windowId));
chrome.tabs.onUpdated.addListener((_id, changeInfo, tab) => {
  // Only the URL settling actually changes our answer.
  if (changeInfo.url || changeInfo.status === "complete") push(tab && tab.windowId);
});
chrome.tabs.onRemoved.addListener(() => push());
chrome.windows.onFocusChanged.addListener(windowId => push(windowId));
chrome.runtime.onStartup.addListener(() => push());
chrome.runtime.onInstalled.addListener(() => push());

chrome.alarms.create("heartbeat", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(() => push());

push();
