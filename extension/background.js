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

async function push(windowId) {
  const onshape = await activeTabIsOnshape(windowId);
  try {
    await fetch(ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ onshape })
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
