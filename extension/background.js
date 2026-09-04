// Reports to the local daemon whether the frontmost Chrome window's active tab is
// on onshape.com, and relays the daemon's own channel — every translated gesture,
// pushed to us over a local WebSocket — to content.js, which is what actually
// dispatches the synthetic DOM events. A content script cannot open this connection
// itself: the page's own CSP would block a request to localhost. The service worker
// is not subject to that.
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
const CHANNEL_URL = "ws://127.0.0.1:47654/";
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

chrome.tabs.onActivated.addListener(info => { push(info.windowId); ensureChannel(); });
chrome.tabs.onUpdated.addListener((_id, changeInfo, tab) => {
  // Only the URL settling actually changes our answer.
  if (changeInfo.url || changeInfo.status === "complete") push(tab && tab.windowId);
  ensureChannel();
});
chrome.tabs.onRemoved.addListener(() => push());
chrome.windows.onFocusChanged.addListener(windowId => { push(windowId); ensureChannel(); });
chrome.runtime.onStartup.addListener(() => { push(); ensureChannel(); });
chrome.runtime.onInstalled.addListener(() => { push(); ensureChannel(); });

chrome.alarms.create("heartbeat", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(() => { push(); ensureChannel(); });

push();

// --- the channel: every translated gesture, relayed to content.js ----------------
//
// There is no OS-level fallback left for rotate, pan, zoom or clear-selection — see
// README's "How it works" — so a slow reconnect here means a fully inert mouse, not
// just a degraded one. Reconnecting is therefore opportunistic on every event above
// that already wakes this service worker, not just the 30s alarm: in practice the
// tightest of these is content.js's own once-a-second canvas-rect report, which
// reaches onMessage below and piggybacks a reconnect attempt on its way through.

let channelSocket = null;
let channelConnecting = false;

function ensureChannel() {
  if (channelConnecting) return;
  if (channelSocket && channelSocket.readyState <= WebSocket.OPEN) return;
  channelConnecting = true;
  let ws;
  try {
    ws = new WebSocket(CHANNEL_URL);
  } catch {
    channelConnecting = false;
    return;
  }
  ws.onopen = () => {
    channelConnecting = false;
    channelSocket = ws;
  };
  ws.onmessage = event => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }
    relayToOnshapeTabs(message);
  };
  ws.onclose = () => {
    channelConnecting = false;
    if (channelSocket !== ws) return;   // already superseded by a newer connection
    channelSocket = null;
    // The daemon dying (killed, crashed, service restarting) is exactly as
    // urgent as it explicitly saying "gate closed": content.js has no other way
    // to find out the channel is gone, and without this it would keep believing
    // the gate is open — real cursor still hidden, an in-progress gesture never
    // getting the synthetic mouseup that ends it on Onshape's own side.
    relayToOnshapeTabs({ type: "gate", open: false });
  };
  ws.onerror = () => {
    // onclose always follows an error on a WebSocket; nothing extra to do here.
  };
}

// Relayed to every onshape.com tab rather than computed down to the one active tab,
// matching the extension's existing pattern for canvas-rect reports: content.js's
// own document.hasFocus() check is what decides which single tab actually acts on
// it, so a background tab safely ignores a gesture message that reaches it too.
async function relayToOnshapeTabs(message) {
  let tabs;
  try {
    tabs = await chrome.tabs.query({ url: "*://*.onshape.com/*" });
  } catch {
    return;
  }
  for (const tab of tabs) {
    if (tab.id === undefined) continue;
    chrome.tabs.sendMessage(tab.id, { gate: message }).catch(() => {});
  }
}

chrome.runtime.onMessage.addListener(() => { ensureChannel(); });

ensureChannel();
