# Onshape trackball gate

Restricts one of two mice so it **only works while onshape.com is frontmost in
Chrome**, and turns its motion into Onshape navigation. Everywhere else — other
tabs, other windows, the desktop — that mouse does nothing at all.

| Left mouse | Onshape sees | Result |
| --- | --- | --- |
| move | synthetic middle-drag | **pan** |
| right button + move | real right-drag | **rotate** |
| wheel | wheel | zoom |
| left button | left click | select |

## How it works

The daemon grabs the chosen mouse exclusively (`EVIOCGRAB`), so X11 never sees the
real device, and replays its events onto a virtual clone — but only while the gate
is open. The gate needs **two** signals to agree:

- **X11** says the focused window belongs to Chrome, tracked event-driven via
  `xprop -root -spy _NET_ACTIVE_WINDOW`.
- **A Chrome extension** says the frontmost Chrome window's active tab is on
  `onshape.com`. It reports only on the *tab*, never on focus — the daemon owns that,
  so refocusing Chrome reopens the gate instantly instead of waiting for a push.

Neither is sufficient alone: X11 cannot see a tab's URL, and the extension's MV3
service worker gets suspended while Chrome sits in the background. Window titles
were ruled out early — Onshape's sign-in page is titled just "Sign in", and
document tabs are named after the document.

Panning is synthesised by holding the middle button down (Onshape's stock
`View manipulation` mapping). A mouse never reports "I stopped moving", so a
timeout ends the stroke — see `pan_idle_release_ms` below.

It **fails closed**: if the daemon or extension stops, the mouse goes dead rather
than becoming unrestricted.

## Requirements

- X11 session (focus tracking uses `xprop`; there is no Wayland equivalent)
- Google Chrome
- `python3-evdev` — `sudo apt install python3-evdev`
- `sudo` access, for a one-time udev rule and group change

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

## Configuration

`~/.config/onshape-trackball/config`, created on first run:

```ini
device      = /dev/input/by-id/usb-PixArt_USB_Optical_Mouse-event-mouse
pan_gesture = ctrl_right
```

Every setting is documented inline in the file itself, which `setup.sh` generates and
keeps up to date — that file is the reference, not this list.

The two that matter most: `pan_gesture` picks which Onshape gesture is synthesised
(`ctrl_right`, or `middle` for the original middle-drag), and `pan_idle_release_ms` is
how long a pan stroke stays live after you stop moving. Bad values warn and fall back
rather than stopping the daemon.

After editing, run `./setup.sh` (it notices the drift and restarts) or:

```bash
systemctl --user restart onshape-mouse-gate.service
```

## Everyday commands

```bash
./setup.sh --status         # what is working, at a glance
./setup.sh --reconfigure    # switch to the other mouse
./setup.sh --help           # full usage
curl -s localhost:47653/status

systemctl --user stop onshape-mouse-gate.service    # temporarily normal mouse
```

## Troubleshooting

`curl -s localhost:47653/status` reports the live gate state; `./setup.sh --status`
interprets it for you.

| Symptom | Look at |
| --- | --- |
| Mouse completely dead | `gate_open` — needs `chrome_focused` **and** `onshape_tab` |
| Gate never opens | `seconds_since_extension_push` is `null` → extension not loaded |
| Mouse works everywhere | Daemon not running, so nothing is grabbing it |
| Motion does not pan | `PAN_BUTTON` in `gate.py` must match Onshape's `View manipulation` preference |
| Changes not taking effect | `daemon settings match the config` in `--status` |

You cannot click *into* Onshape with the gated mouse — it is inert until Onshape is
already frontmost, so focus the window with your other mouse first. Both mice also
share one cursor; the gated one simply stops contributing motion outside Onshape.

## Uninstall

```bash
./setup.sh --uninstall
```

Removes the service, unit and config. Asks separately before touching shared state
(the udev rule, your `input` group membership) and defaults to keeping both. Never
touches this directory. Remove the extension yourself at `chrome://extensions`.

## Files

| File | Purpose |
| --- | --- |
| `setup.sh` | Installer, status board, reconfigure, uninstall |
| `gate.py` | The daemon: grab, gate, translate |
| `pick-mouse.py` | Lists mice, or detects one by movement |
| `extension/` | Chrome MV3 extension reporting the active tab |
| `test_translator.py` | Pan/rotate state machine — 8 cases |
| `test_config.py` | Config parsing, clamping, fallbacks — 11 cases |

```bash
python3 test_translator.py && python3 test_config.py
```

Both run without hardware or permissions: the translator tests drive synthetic
events against a stubbed virtual device.
