// Reports to the local daemon whether the focused Chrome window's active tab is on
// onshape.com. Pushes on every real transition; the alarm is only a liveness heartbeat
// so the daemon can fail closed if this extension stops running.

const ENDPOINT = "http://127.0.0.1:47653/state";
const ONSHAPE_HOST = /(^|\.)onshape\.com$/i;

async function onshapeIsFrontmost() {
  let win;
  try {
    win = await chrome.windows.getLastFocused();
  } catch {
    return false;
  }
  // Chrome itself is in the background (user alt-tabbed away).
  if (!win || !win.focused) return false;

  let tabs;
  try {
    tabs = await chrome.tabs.query({ active: true, windowId: win.id });
  } catch {
    return false;
  }
  const tab = tabs && tabs[0];
  if (!tab || !tab.url) return false;

  try {
    return ONSHAPE_HOST.test(new URL(tab.url).hostname);
  } catch {
    return false;
  }
}

async function push() {
  const onshape = await onshapeIsFrontmost();
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

chrome.tabs.onActivated.addListener(push);
chrome.tabs.onUpdated.addListener((_id, changeInfo) => {
  // Only the URL settling actually changes our answer.
  if (changeInfo.url || changeInfo.status === "complete") push();
});
chrome.tabs.onRemoved.addListener(push);
chrome.windows.onFocusChanged.addListener(push);
chrome.runtime.onStartup.addListener(push);
chrome.runtime.onInstalled.addListener(push);

chrome.alarms.create("heartbeat", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(push);

push();
