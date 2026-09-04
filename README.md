# Onshape trackball gate

Restricts one of two mice so it **only works while onshape.com is frontmost in
Chrome**, and turns its motion into Onshape navigation. Everywhere else — other
tabs, other windows, the desktop — that mouse does nothing at all.

| Left mouse | Onshape sees | Result |
| --- | --- | --- |
| right button + move | synthetic Ctrl + right-drag | **pan** |
| move | plain right-drag | **rotate** |
| wheel | wheel | zoom |
| left button | space | **clear the selection** |

Which gesture the right button maps to is configurable — see `pan_requires_right_button`
under [Configuration](#configuration). Rotate's sensitivity is configurable too — see
`rotate_scale`; it defaults to half of the raw, unscaled motion this used to send.

## How it works

The daemon grabs the chosen mouse exclusively, so the desktop never sees the real
device — but nothing it decides ever reaches the OS as real input. Every translated
action (rotate, pan, zoom, clear-selection) is instead sent over a small local
WebSocket channel to a Chrome extension, which dispatches the matching **untrusted**
DOM event directly on Onshape's canvas or page — a plain `MouseEvent`/`WheelEvent`/
`KeyboardEvent` built and fired by a content script, not a synthetic hardware
event. Confirmed live: Chrome never raises its own context menu in response to
synthetic dispatch, even for a drag well under the size that would trigger one from
real hardware, so there is no OS-level side effect left to work around.

This runs only while the gate is open. The gate needs **two** signals to agree:

- **The desktop** says the focused window belongs to Chrome, tracked event-driven
  rather than polled.
- **A Chrome extension** says the frontmost Chrome window's active tab is on
  `onshape.com`. It reports only on the *tab*, never on focus — the daemon owns that,
  so refocusing Chrome reopens the gate instantly instead of waiting for a push.

Neither is sufficient alone: the window manager cannot see a tab's URL, and the
extension's MV3 service worker gets suspended while Chrome sits in the background.
Window titles were ruled out early — Onshape's sign-in page is titled just "Sign
in", and document tabs are named after the document.

Both halves of the gate are platform-specific, and everything above them is not.
`gate.py` holds the translator, the channel, the gate and the config; `backend_linux.py`
and `backend_windows.py` hold only what genuinely differs — capturing the mouse and
tracking focus. Translated output is identical on both, since it never touches the
OS at all:

| | Linux | Windows |
| --- | --- | --- |
| Exclusive grab | `EVIOCGRAB` | Interception driver |
| Focus | `xprop -root -spy` | `SetWinEventHook` |
| Service | systemd user unit | scheduled task |

Pan is synthesised as a Ctrl-tagged right-drag — one of Onshape's two documented pan
gestures — with `ctrlKey` set directly on the synthetic event rather than coming from
a real held key. The other pan gesture, middle-drag, is deliberately unused: panning
presses the same button repeatedly, and two presses inside the double-click window
are a double middle-click, which Onshape reads as Zoom to Fit.

One of pan and rotate is driven by the right button being physically held —
bracketed by its own press and release, so it needs no further help — and the other
by bare motion, which has no button of its own to mark where it starts or ends. Which
is which is `pan_requires_right_button`; by default the held gesture is pan.

A mouse never reports "I stopped moving", so whichever gesture bare motion drives
ends on a timeout instead of a release — see `pan_idle_release_ms` below.

Because the motion-driven gesture holds the right button down and drags it, whatever
the extension dispatches it onto receives that press and that release. On the canvas
that is pan or rotate as configured; on any ordinary DOM element it would be a
context menu, or a toolbar button being clicked. So the content script only ever
dispatches onto the region that is *verified* to be 3D view and nothing else, and the
gesture simply refuses to start without one. It finds that region by hit-testing a
coarse grid to discover which elements overlay the canvas, measuring each one
exactly, and then solving for the largest overlay-free rectangle — which is what
catches the things that float over the middle of the view, like the view cube and the
context toolbar, as well as the chrome anchored to its edges.

When no such region can be found — a dialog covering the view, the content script not
running — the daemon **refuses to pan** and ends the stroke if one is already open.
There is deliberately no fallback to the canvas's own rect or to the Chrome window:
both of those contain the very elements the region exists to stay off.

It **fails closed**: if the daemon, the extension, or the channel between them stops,
the mouse goes dead rather than becoming unrestricted. There is no fallback path any
more — the earlier design behind this project fell back to real OS-level injection
when the channel was unavailable, but that is exactly the failure class the channel
exists to remove, so losing the channel now means losing rotate/pan/zoom/clear-selection
entirely until it reconnects, not a degraded version of them. `curl -s
localhost:47653/status` reports `channel_connected` and
`seconds_since_channel_send` for exactly this.

### Two cursors

Neither mouse ever moves the real, system cursor: the gated mouse never did (its
motion only ever became rotate/pan on the canvas, or a key tap, or a wheel event),
and now the channel means it does not even move it as a side effect. So both mice
get a cursor of their own on the page, drawn by the extension while the gate is
open: a small dot for the gated mouse's own virtual position, and another,
differently-coloured one for whatever the real system cursor is currently doing —
the extension tells the two apart by `event.isTrusted`, since everything it
dispatches itself is untrusted by construction. The real cursor glyph is hidden
(`cursor: none`) for the same window, so there is only ever one visible pointer
where you are actually looking.

## Requirements

**Linux**

- X11 session (focus tracking uses `xprop`; there is no Wayland equivalent)
- Google Chrome
- `python3-evdev` — `sudo apt install python3-evdev`
- `sudo` access, for a one-time udev rule and group change

**Windows**

- Windows 10 or 11
- Google Chrome
- Python 3 on `PATH` (the Microsoft Store stub is not enough — `setup.cmd` checks)
- The [Interception](https://github.com/oblitum/Interception) driver, plus
  administrator rights and a reboot to install it

## Install

```bash
./setup.sh
```

Re-run it any time. Every step is checked before it is attempted, so it resumes
rather than repeating work.

1. **Permissions** *(sudo)* — installs a udev rule making `/dev/uinput`
   group-writable, and adds you to the `input` group.
2. **Log out and back in** — the group only takes effect on a fresh session. A new
   terminal is not enough; the systemd user manager has to restart too. The script
   stops here and tells you, then continues from step 3 on the next run.
3. **Choose the mouse** — pick from a list, or press `d` and *move* the mouse you
   want gated. Detection is the reliable option; device names are easy to mix up.
4. **Service** — installs and starts a systemd user unit.
5. **Mouse grab** — confirms the daemon actually has the device.
6. **Extension** — load it by hand: `chrome://extensions` → **Developer mode** →
   **Load unpacked** → this repo's `extension/` directory.

Step 6 is the only manual part, and Chrome will nag about developer-mode
extensions on startup. That is unavoidable for unpacked extensions.

### Windows

```bat
setup.cmd
```

Same character as `setup.sh` — re-run it any time, every step is checked before it
is attempted, and it resumes rather than repeating work.

1. **Driver** *(administrator)* — installs Interception. Download it from
   [oblitum/Interception](https://github.com/oblitum/Interception), run
   `install-interception.exe /install` from an elevated prompt, and put
   `interceptor.dll` next to `gate.py`.
2. **Reboot** — the driver only takes effect on a fresh boot. The script stops
   here and tells you, then continues from step 3 on the next run. This is the
   Windows counterpart of the Linux udev-rule-and-re-login gate.
3. **Configuration** — creates `%APPDATA%\onshape-trackball\config`.
4. **Choose the mouse** — pick from a list, or press `d` and *move* the mouse you
   want gated. Detection is the reliable option; device names are easy to mix up.
5. **Service** — registers and starts a scheduled task that runs at logon.
6. **Mouse grab** — confirms the daemon actually has the device.
7. **Extension** — exactly as on Linux, loaded by hand from `extension\`.

Why a kernel driver: Windows has no user-space way to take one mouse away from
every application. Raw Input can tell you *which* device moved but cannot suppress
it, and a low-level hook can suppress but does not carry device identity — so the
cursor would still jump before anything could stop it. Interception sits above the
mouse class driver, which is what makes the gated mouse genuinely dead rather than
merely ignored.

**Known limitation:** anti-cheat drivers (Vanguard, EasyAntiCheat, BattlEye) and
some endpoint-security products either block input filter drivers or refuse to run
alongside them. `setup.cmd` names the ones it can detect, but there is no
workaround this project can offer.

## Configuration

`~/.config/onshape-trackball/config` on Linux,
`%APPDATA%\onshape-trackball\config` on Windows, created on first run:

```ini
device               = /dev/input/by-id/usb-PixArt_USB_Optical_Mouse-event-mouse
left_click_key       = space
pan_idle_release_ms  = 150
```

Same keys and same format on both. Only `device` differs in shape: a
`/dev/input/by-id` path on Linux, an Interception hardware ID
(`HID\VID_046D&PID_C52B&...`) on Windows. It is the hardware ID rather than the
device number because Interception renumbers devices across reboots, and a number
would quietly start gating a different mouse.

Every setting is documented inline in the file itself, which the setup script
generates and keeps up to date — that file is the reference, not this list. Bad
values warn and fall back rather than stopping the daemon.

After editing, re-run the setup script (it notices the drift and restarts) or:

```bash
systemctl --user restart onshape-mouse-gate.service    # Linux
```
```bat
schtasks /End /TN "Onshape trackball gate" & schtasks /Run /TN "Onshape trackball gate"
```

## Everyday commands

```bash
./setup.sh --status         # what is working, at a glance
./setup.sh --reconfigure    # switch to the other mouse
./setup.sh --help           # full usage
curl -s localhost:47653/status

systemctl --user stop onshape-mouse-gate.service    # temporarily normal mouse
```

On Windows the same options, through `setup.cmd`:

```bat
setup.cmd --status
setup.cmd --reconfigure
setup.cmd --help
curl -s localhost:47653/status

schtasks /End /TN "Onshape trackball gate"          :: temporarily normal mouse
```

## Troubleshooting

`curl -s localhost:47653/status` reports the live gate state; `./setup.sh --status`
interprets it for you.

| Symptom | Look at |
| --- | --- |
| Mouse completely dead | `gate_open` — needs `chrome_focused` **and** `onshape_tab` |
| Gate never opens | `seconds_since_extension_push` is `null` → extension not loaded |
| Mouse works everywhere | Daemon not running, so nothing is grabbing it |
| Gate is open but nothing happens — no rotate, pan, zoom or clear-selection | `channel_connected` is `false` → the extension's WebSocket to the daemon is down. Most often the extension's service worker was suspended by Chrome; it should reconnect within a second or two of any activity (switching tabs, moving the mouse over an Onshape tab). If it does not, reload the extension |
| Cursor strays onto the feature tree while navigating, or a gesture opens on a toolbar | `canvas_diag.v` is below 8 → Chrome is running a stale content script. **Reload the extension** at `chrome://extensions`, then **reload the Onshape tab** — Chrome does not inject content scripts into pages that were already open when the extension was loaded |
| Motion does not pan or rotate, everything else works | `canvas_rect` is `null` → no verified view region, so the motion-driven gesture is refused on purpose. Either the content script is not running (see above) or something is covering the view |
| Motion does not pan | Onshape's `View manipulation` preference must accept Ctrl + right-drag |
| A context menu opened while panning or rotating | `context_menus` — see below |
| Changes not taking effect | `daemon settings match the config` in `--status` |

### A context menu opened while panning or rotating

Nothing here suppresses a menu any more — see below — so this is diagnostic only,
for tracking down *why* one appeared. It leaves no trace on its own: the menu is
browser UI rather than anything in the page, and by the time you have seen it the
state that caused it is gone. So the content script reports every `contextmenu`
event as it fires, and the daemon records it next to what it was doing at that
instant. Newest first:

```bash
curl -s localhost:47653/status | python3 -c "import sys,json;[print(e['at'],'|',e['why'],'|',e['target']) for e in json.load(sys.stdin)['context_menus']]"
```

Each entry carries a `why`:

| `why` says | Meaning |
| --- | --- |
| on an overlay **INSIDE** the region we reported safe | The probe missed something — `target` names the element it missed. Raise `DISCOVERY_COLS`/`DISCOVERY_ROWS` in `content.js`; the overlay was smaller than the sample spacing |
| on an overlay **outside** the region | The *other* mouse's own real right-click landed on an overlay — expected, not a bug: only the reported region is ever verified safe |
| **on the canvas** | Most likely Onshape's own canvas menu, reacting to our synthetic dispatch — it opens straight out of Onshape's own mouseup handling. `content.js` now suppresses this one (see below) |
| **not during a pan** | Not this mouse at all — the other mouse's real right-click, or Menu-key/Shift+F10 |

`menu_shown` says whether a menu actually reached the user — either because
something on the page called `preventDefault` on its own, or because `content.js`
recognised it as its own and suppressed it — and `in_reported_region` plus
`at_point` say exactly where it landed relative to the region the daemon had been
given.

### Stopping them

Chrome's own, OS-drawn context menu: nothing does, and nothing needs to. An earlier
version of this project injected the gated mouse's gestures as *real*, OS-trusted
input, indistinguishable from hardware, so Chrome would raise its own menu for a
right-tap the same as it would for real hardware, and suppression was keyed on the
shape of the gesture (`ctrlKey` still set, or the button dragged past a few pixels)
to tell that apart from a genuine right-click. Every translated action now goes out
as an **untrusted** synthetic DOM event instead — confirmed live, Chrome never
raises its own context menu in response to one, even for a drag well under the size
that would trigger it from real hardware. The gated mouse cannot produce a real
Chrome context menu any more, on purpose or by accident, so there is nothing left
for that old heuristic to protect against — and keeping it around would only risk
swallowing the *other* mouse's own genuine right-clicks, which is a worse bug than
the one it used to fix. `content.js`'s `mousedown`/`mouseup`/`mousemove` tracking
still explicitly excludes this extension's own synthetic dispatch via
`event.isTrusted`, so what's left of that old tracking (`dragPx`, `ctrl` in the
table above) describes only the other mouse's real activity.

Onshape's own, in-page canvas menu is a different animal: it isn't Chrome UI, and
it opens because Onshape's own mouseup handling treats a right-button press and
release with next to no motion between them as a plain right-click — which is
exactly what a pan or rotate stroke looks like when it ends before the cursor has
moved, including the last fragment of an otherwise ordinary drag right before
`pan_idle_release_ms` cuts it off.

`content.js` now stops this at the source rather than reacting to it:
`gcEndGesture` compares the release position against where `gcBeginGesture` opened
the stroke (`gcPressPos`), and if the two are closer than `FORCE_DRAG_MIN_PX`, fires
one extra synthetic `mousemove` to push the release out past that distance before
releasing — so Onshape sees a real drag on every stroke, never a click, regardless
of how little the trackball itself moved. `gcVirtual` itself is left untouched by
this, so it cannot drift over a run of near-still taps; only where that one release
lands is nudged. Because it changes what Onshape sees rather than intercepting
Onshape's *reaction* to what it saw, this doesn't depend on guessing right about
Onshape's own menu-rendering internals the way suppression does.

The `contextmenu` listener still carries a suppression fallback from before this,
kept as defense in depth rather than removed: `gcEndGesture` also records the
position and time of its own synthetic release (`lastSyntheticRelease`), and the
listener calls `preventDefault` plus taps Escape when a menu event lands on the
canvas within `MENU_SUPPRESS_TOLERANCE_PX` and `MENU_SUPPRESS_WINDOW_MS` of that
release — tight enough that the other mouse's own right-click on the canvas would
have to land on almost the same pixel in the same quarter-second to be mistaken for
ours.

You cannot click *into* Onshape with the gated mouse — it is inert until Onshape is
already frontmost, so focus the window with your other mouse first.

## Uninstall

```bash
./setup.sh --uninstall      # Linux
```
```bat
setup.cmd --uninstall       :: Windows
```

Removes the service and config. Asks separately before touching shared state — the
udev rule and your `input` group membership on Linux, the Interception driver on
Windows — and defaults to keeping it. Never touches this directory. Remove the
extension yourself at `chrome://extensions`.

## Files

| File | Purpose |
| --- | --- |
| `setup.sh` | Linux installer, status board, reconfigure, uninstall |
| `setup.cmd` | The same on Windows |
| `setup_helper.py` | What `setup.cmd` delegates JSON, HTTP and enumeration to |
| `gate.py` | The daemon: translator, the WebSocket channel, gate, config, status server |
| `backend.py` | Picks a platform backend and defines the interface |
| `backend_linux.py` | evdev exclusive capture, `xprop` focus |
| `backend_windows.py` | Interception exclusive capture, focus and Raw Input |
| `interceptor.py` | ctypes binding for `interceptor.dll` |
| `codes.py` | Input event constants, so the core needs no evdev |
| `pick-mouse.py` | Lists mice, or detects one by movement (Linux) |
| `extension/` | Chrome MV3 extension: reports the active tab, relays the channel, and dispatches every synthetic gesture |
| `test_translator.py` | Pan/rotate state machine, channel messages, view-region fail-closed |
| `test_config.py` | Config parsing, clamping, fallbacks |
| `test_probe.js` | The content script's view-region probe, against simulated layouts |

```bash
python3 test_translator.py && python3 test_config.py && node test_probe.js
```

The Python suites run on either platform without hardware, permissions or the driver:
the translator tests drive synthetic events against a stubbed virtual device, and the
event codes come from `codes.py` rather than from evdev.

`test_probe.js` needs only Node. It builds simulated Onshape layouts — edge chrome,
a floating view cube, a context toolbar over the middle of the view — runs the real
`content.js` against each, then hit-tests every point of the region it reported back
through the same layout. Any point that is not the canvas is a point where a real pan
would press a real button, so the assertion is the guarantee itself rather than a
restatement of the implementation.
