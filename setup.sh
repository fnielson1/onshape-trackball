#!/usr/bin/env bash
#
# Setup / resume installer for the Onshape trackball gate.
#
# Safe to run repeatedly. Every step is checked before it is attempted, so re-running
# after the required logout/login picks up exactly where it left off.
#
#   ./setup.sh                 install, prompting where needed
#   ./setup.sh --status        show what is done and what is left, change nothing
#   ./setup.sh --reconfigure   re-pick which mouse is the left one
#   ./setup.sh --device PATH   set the left mouse non-interactively
#   ./setup.sh --uninstall     remove the service, unit and config
#   ./setup.sh --yes           assume yes for confirmations (sudo may still prompt)
#
# Run ./setup.sh --help for the full usage text.
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/onshape-trackball"
CONFIG_FILE="$CONFIG_DIR/config"
LEGACY_DEVICE_FILE="$CONFIG_DIR/device"
DEFAULT_PAN_IDLE_MS=150
DEFAULT_RECENTER_MARGIN=35
DEFAULT_DEADZONE_PX=20
DEFAULT_YIELD_DEADZONE_PX=20
UDEV_RULE="/etc/udev/rules.d/99-onshape-mouse.rules"
UNIT_NAME="onshape-mouse-gate.service"
UNIT_PATH="$HOME/.config/systemd/user/$UNIT_NAME"
PORT=47653

if [[ -t 1 ]]; then
  BOLD=$'\e[1m'; RED=$'\e[31m'; GRN=$'\e[32m'; YEL=$'\e[33m'; DIM=$'\e[2m'; OFF=$'\e[0m'
else
  BOLD=; RED=; GRN=; YEL=; DIM=; OFF=
fi

usage() {
  cat <<EOF
${BOLD}Onshape trackball gate${OFF}

Restricts one mouse so it only works while onshape.com is frontmost in Chrome, and
turns its motion into Onshape navigation: move pans (Ctrl+right-drag), right-button
+ move rotates, wheel zooms, left click clears the selection.

Safe to run repeatedly. Every step is checked before it is attempted, so
re-running after the required logout/login resumes where it left off.

${BOLD}USAGE${OFF}
  $0 [options]

${BOLD}OPTIONS${OFF}
  -s, --status        Show what is done and what is left; change nothing.
      --reconfigure   Re-pick which mouse is the left one.
      --device PATH   Set the left mouse non-interactively. Takes a
                      /dev/input/by-id/...-event-mouse path.
      --uninstall     Stop and remove the service, unit and config. Offers to
                      remove the udev rule and 'input' group membership too.
  -y, --yes           Assume yes for confirmations; sudo may still prompt.
                      Cannot choose a mouse for you, and never removes you
                      from the 'input' group.
  -h, --help          This text.

${BOLD}INSTALL STEPS${OFF}
  1. Permissions   udev rule, /dev/uinput mode, 'input' group    (sudo)
  2. Re-login      required before the group takes effect
  3. Choose mouse  pick from a list, or 'd' to detect by moving it
  4. Service       install and start the systemd user unit
  5. Mouse grab    confirm the daemon has the device
  6. Extension     load the Chrome extension (done by hand, in Chrome)

${BOLD}EXAMPLES${OFF}
  $0                  Install, or resume an interrupted install.
  $0 --status         Check health at a glance.
  $0 --reconfigure    Switch to the other mouse.
  $0 --uninstall

${BOLD}CONFIGURATION${OFF}
  \$XDG_CONFIG_HOME/onshape-trackball/config, created on first run.
  Holds the chosen mouse and pan_idle_release_ms (default ${DEFAULT_PAN_IDLE_MS}ms) —
  how long a pan stroke stays live after you stop moving. Edit it, then:
    systemctl --user restart $UNIT_NAME

${BOLD}ONCE INSTALLED${OFF}
  curl -s localhost:$PORT/status
  systemctl --user status $UNIT_NAME
  systemctl --user stop $UNIT_NAME    Temporarily restore a normal mouse.
EOF
}

ASSUME_YES=0
STATUS_ONLY=0
RECONFIGURE=0
UNINSTALL=0
DEVICE_OVERRIDE=""

while (( $# )); do
  case "$1" in
    --yes|-y)      ASSUME_YES=1 ;;
    --status|-s)   STATUS_ONLY=1 ;;
    --reconfigure) RECONFIGURE=1 ;;
    --uninstall)   UNINSTALL=1 ;;
    --device)      shift; [[ $# -gt 0 ]] || { echo "--device needs a path" >&2; exit 2; }; DEVICE_OVERRIDE="$1" ;;
    --help|-h)     usage; exit 0 ;;
    *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

ok()    { printf '  %s✓%s %s\n' "$GRN" "$OFF" "$1"; }
bad()   { printf '  %s✗%s %s\n' "$RED" "$OFF" "$1"; }
todo()  { printf '  %s·%s %s\n' "$YEL" "$OFF" "$1"; }
head_() { printf '\n%s%s%s\n' "$BOLD" "$1" "$OFF"; }
note()  { printf '    %s%s%s\n' "$DIM" "$1" "$OFF"; }
die()   { printf '\n%sError:%s %s\n' "$RED" "$OFF" "$1" >&2; exit 1; }

confirm() {
  (( ASSUME_YES )) && return 0
  local reply
  read -r -p "$1 [Y/n] " reply
  [[ -z $reply || $reply =~ ^[Yy] ]]
}

# Default-no variant, for changes to state other things might share. --yes
# deliberately does NOT auto-accept these.
confirm_risky() {
  (( ASSUME_YES )) && return 1
  local reply
  read -r -p "$1 [y/N] " reply
  [[ $reply =~ ^[Yy] ]]
}

# ---------------------------------------------------------------- state checks

have_udev_rule()  { [[ -f $UDEV_RULE ]]; }
uinput_perms_ok() { [[ "$(stat -c '%G %a' /dev/uinput 2>/dev/null || true)" == "input 660" ]]; }
in_group_file()   { getent group input | grep -qE "(:|,)$USER(,|$)"; }
group_active()    { id -nG 2>/dev/null | tr ' ' '\n' | grep -qx input; }
unit_installed()  { [[ -f $UNIT_PATH ]]; }
unit_enabled()    { systemctl --user is-enabled --quiet "$UNIT_NAME" 2>/dev/null; }
unit_active()     { systemctl --user is-active  --quiet "$UNIT_NAME" 2>/dev/null; }
daemon_up()       { curl -fsS --max-time 2 "http://127.0.0.1:$PORT/status" >/dev/null 2>&1; }

config_get() {  # key -> value, or nothing
  [[ -f $CONFIG_FILE ]] || return 1
  sed -n "s|^[[:space:]]*$1[[:space:]]*=[[:space:]]*||p" "$CONFIG_FILE" \
    | sed 's|[[:space:]]*$||' | grep -v '^$' | head -1
}

config_set() {  # key value
  ensure_config
  if grep -qE "^[[:space:]]*$1[[:space:]]*=" "$CONFIG_FILE"; then
    sed -i "s|^[[:space:]]*$1[[:space:]]*=.*|$1 = $2|" "$CONFIG_FILE"
  else
    printf '%s = %s\n' "$1" "$2" >> "$CONFIG_FILE"
  fi
}

# Creates the config on first run, carrying over a device chosen under the older
# single-purpose "device" file so an existing install is not disturbed.
# Every setting is documented in exactly one place: this function. ensure_config
# builds a fresh file from it, and ensure_config_keys appends whatever an older
# config is missing. Keeping one source avoids the two copies drifting apart.
config_block() {
  case "$1" in
    device)
      cat <<EOF
# Which mouse is gated. Set by setup.sh; change it with:  setup.sh --reconfigure
device = ${2:-}
EOF
      ;;
    left_click_key)
      cat <<EOF

# What the gated mouse's left button does.
#
# Because the cursor is penned in the middle of the view, a real left click would
# just select whatever geometry happens to be under it — rarely what you want from a
# navigation mouse. Onshape clears the whole selection on space, so the button taps
# that instead, and the click itself is swallowed.
#
#   space   clear the selection (Onshape's own shortcut)
#   esc     send Escape instead
#   none    pass the click straight through, as an ordinary left click
left_click_key = space
EOF
      ;;
    pan_requires_right_button)
      cat <<EOF

# Which of pan and rotate the gated mouse's right button performs.
#
# One of the two is bracketed by the right button's own press and release; the
# other is driven by bare motion instead, started once it clears pan_deadzone_px
# and ended by pan_idle_release_ms after it stops, since bare motion has no
# button of its own to mark either.
#
#   true    hold the right button to pan, move without it to rotate
#   false   move to pan, hold the right button to rotate — the original mapping
pan_requires_right_button = true
EOF
      ;;
    pan_restore_cursor)
      cat <<EOF

# When panning ends, snap the cursor back to where it was before panning began.
#
# The cursor's position while panning is a side effect of the drag and of edge
# recentring, not something meant to persist — this puts it back so a click right
# after a pan lands where it would have without the pan in between. Only for a
# genuine end: a hand-off where the same drag carries on as rotate under the
# still-held button does not move the cursor, since the user's hand is still
# actively dragging it.
pan_restore_cursor = true
EOF
      ;;
    pan_idle_release_ms)
      cat <<EOF

# How long a pan stroke stays live after you stop moving, in milliseconds.
#
# A mouse never says "I stopped", so this timeout is what ends a stroke: the
# pan button is released this long after the last motion. Without it the button
# would stay down for ever.
#
#   lower   strokes end sooner; brief pauses split one pan into several
#   higher  the button stays held longer after you stop moving
#
# Accepted range is 20-2000; anything outside is clamped.
pan_idle_release_ms = $DEFAULT_PAN_IDLE_MS
EOF
      ;;
    pan_deadzone_px)
      cat <<EOF

# How far the gated mouse must travel before a pan actually starts.
#
# Measured as net displacement, not distance travelled, so jitter that wanders out
# and back never trips it — only a deliberate push does. Each stroke earns its own
# dead zone, and a nudge that goes nowhere expires rather than banking toward the
# next one.
#
# Panning only. Rotating, zooming and the left button are unaffected, and the cursor
# keeps tracking your hand throughout — it just is not panning yet.
#
# Accepted range 0-500; 0 starts panning on the first movement.
pan_deadzone_px = $DEFAULT_DEADZONE_PX
EOF
      ;;
    pan_recenter)
      cat <<EOF

# Panning drags the real cursor, so a long sweep runs out of screen and the pan
# dies. With recentring on, the cursor is warped back to the middle of the view
# whenever it comes within pan_recenter_margin_px of an edge, making a pan
# effectively unlimited. The pan button is briefly lifted around the warp so the
# jump is not read as one huge pan.
#
# The edge is the usable 3D view's edge, not the Chrome window's. The extension
# probes the page to find the region that genuinely belongs to the view — the canvas
# minus the controls Onshape stacks on top of it — and the daemon pens the cursor
# inside that. This is what keeps the cursor off the feature tree and the tool
# strips, which are ordinary DOM elements and so do not suppress Chrome's context
# menu the way the canvas does. If the extension stops reporting, it falls back to
# the whole window after a few seconds.
#
# Set pan_recenter to false to get the old behaviour: pan until you hit the edge,
# then lift and reposition.
pan_recenter = true
pan_recenter_margin_px = $DEFAULT_RECENTER_MARGIN
EOF
      ;;
    pan_yield_to_other_mice)
      cat <<EOF

# Both mice drive one shared X11 pointer, so while a pan stroke is live the held
# pan button applies to whatever your other mouse does too: its motion pans, and
# its wheel arrives as wheel-with-button-held instead of a clean scroll.
#
# With this on, the other mice are watched read-only (never grabbed, so they keep
# working normally) and any activity on one drops the pan stroke immediately.
# Panning resumes shortly after they go quiet.
#
# Turn it off if resting your hand on the other mouse interrupts panning too eagerly.
pan_yield_to_other_mice = true
EOF
      ;;
    pan_yield_deadzone_px)
      cat <<EOF

# How far the *other* mouse must travel before its motion counts as deliberate and
# drops the pan or rotate the gated mouse is holding.
#
# Net displacement, measured the same way as pan_deadzone_px. Below this, resting a
# hand on the other mouse or bumping it in passing does not interrupt the stroke. A
# button press or a wheel turn on it always interrupts immediately, regardless of
# this setting — neither happens by accident.
#
# Accepted range 0-500; 0 yields on the very first movement, which was the only
# behaviour before this setting existed.
pan_yield_deadzone_px = $DEFAULT_YIELD_DEADZONE_PX
EOF
      ;;
  esac
}

CONFIG_KEYS=(device left_click_key pan_requires_right_button pan_restore_cursor
             pan_deadzone_px pan_idle_release_ms pan_recenter
             pan_yield_to_other_mice pan_yield_deadzone_px)

# Creates the config on first run, carrying over a device chosen under the older
# single-purpose "device" file so an existing install is not disturbed.
ensure_config() {
  [[ -f $CONFIG_FILE ]] && return 0

  mkdir -p "$CONFIG_DIR"
  local legacy=""
  if [[ -s $LEGACY_DEVICE_FILE ]]; then
    legacy="$(grep -vE '^[[:space:]]*(#|$)' "$LEGACY_DEVICE_FILE" | head -1 || true)"
  fi

  {
    echo "# Onshape trackball gate — configuration."
    echo "#"
    echo "# Apply changes with:"
    echo "#   systemctl --user restart $UNIT_NAME"
    echo
    local key
    for key in "${CONFIG_KEYS[@]}"; do
      config_block "$key" "$legacy"
    done
  } > "$CONFIG_FILE"

  ok "created $CONFIG_FILE"
  if [[ -n $legacy ]]; then
    rm -f "$LEGACY_DEVICE_FILE"
    note "migrated your mouse choice from the old 'device' file"
  fi
}

# An existing config predates later settings; append what is missing so the file
# always documents every knob the daemon actually reads.
ensure_config_keys() {
  [[ -f $CONFIG_FILE ]] || return 0
  local key
  for key in "${CONFIG_KEYS[@]}"; do
    grep -qE "^[[:space:]]*$key[[:space:]]*=" "$CONFIG_FILE" && continue
    config_block "$key" >> "$CONFIG_FILE"
    ok "added $key to $CONFIG_FILE"
  done
}

configured_device() {
  local dev
  dev="$(config_get device || true)"
  if [[ -z $dev && -s $LEGACY_DEVICE_FILE ]]; then
    dev="$(grep -vE '^[[:space:]]*(#|$)' "$LEGACY_DEVICE_FILE" | head -1 || true)"
  fi
  [[ -n $dev ]] && printf '%s\n' "$dev"
}
device_configured() { [[ -n "$(configured_device || true)" ]]; }

configured_pan_ms() { config_get pan_idle_release_ms || printf '%s' "$DEFAULT_PAN_IDLE_MS"; }
configured_recenter_margin() { config_get pan_recenter_margin_px || printf '%s' "$DEFAULT_RECENTER_MARGIN"; }

mouse_name() {
  "$REPO_DIR/pick-mouse.py" --list 2>/dev/null \
    | awk -F'\t' -v p="$1" '$1==p{print $2; found=1} END{if(!found) print "unrecognised device"}'
}

# Guards against a service still running an older copy from a previous location:
# systemd keeps a deleted unit loaded until daemon-reload, and a running python
# process survives its script being moved or deleted.
service_exec_ok() {
  local execstart
  execstart="$(systemctl --user show -p ExecStart --value "$UNIT_NAME" 2>/dev/null || true)"
  [[ -n $execstart && $execstart == *"$REPO_DIR/gate.py"* ]]
}
running_exec_ok() {
  local pid
  pid="$(systemctl --user show -p MainPID --value "$UNIT_NAME" 2>/dev/null || true)"
  [[ -n $pid && $pid != 0 ]] || return 1
  tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -qF "$REPO_DIR/gate.py"
}

# Catches an edited gate.py that the running daemon predates. Path and config
# comparisons both miss this: a python process keeps running its old in-memory code
# no matter what the file on disk now says.
running_build_fresh() {
  local pid started src
  pid="$(systemctl --user show -p MainPID --value "$UNIT_NAME" 2>/dev/null || true)"
  [[ -n $pid && $pid != 0 ]] || return 1
  started="$(date -d "$(ps -o lstart= -p "$pid" 2>/dev/null)" +%s 2>/dev/null)" || return 1
  [[ -n $started ]] || return 1
  src="$(stat -c %Y "$REPO_DIR/gate.py" 2>/dev/null)" || return 1
  (( started >= src ))
}

daemon_settings_current() {
  daemon_up || return 1
  [[ "$(status_field device 2>/dev/null || true)" == "$(configured_device)" ]] || return 1
  [[ "$(status_field pan_idle_release_ms 2>/dev/null || true)" == "$(configured_pan_ms)" ]] || return 1
  [[ "$(status_field pan_recenter_margin_px 2>/dev/null || true)" == "$(configured_recenter_margin)" ]] || return 1
  [[ "$(status_field left_click_key 2>/dev/null || true)" == "$(config_get left_click_key || printf 'space')" ]] || return 1
  [[ "$(status_field pan_deadzone_px 2>/dev/null || true)" == "$(config_get pan_deadzone_px || printf '%s' "$DEFAULT_DEADZONE_PX")" ]] || return 1
  [[ "$(status_field pan_yield_deadzone_px 2>/dev/null || true)" == "$(config_get pan_yield_deadzone_px || printf '%s' "$DEFAULT_YIELD_DEADZONE_PX")" ]] || return 1
  # pan_requires_right_button and pan_restore_cursor are not checked here:
  # status_field prints a JSON boolean through Python's str(), which renders
  # True/False rather than the lowercase true/false the config file and
  # setup_helper.py's own drift check use — the same reason pan_recenter and
  # pan_yield_to_other_mice are absent above.
}

status_field() {
  local json
  json="$(curl -fsS --max-time 2 "http://127.0.0.1:$PORT/status" 2>/dev/null)" || return 1
  printf '%s' "$json" | python3 -c "
import json,sys
d=json.load(sys.stdin)
v=d.get('$1')
print('' if v is None else v)
" 2>/dev/null
}

device_attached() { [[ "$(status_field device_attached 2>/dev/null || true)" == "True" ]]; }
extension_seen()  { [[ -n "$(status_field seconds_since_extension_push 2>/dev/null || true)" ]]; }

# ---------------------------------------------------------------- preflight

preflight() {
  head_ "Preflight"

  [[ "${XDG_SESSION_TYPE:-}" == "x11" ]] \
    || die "this needs an X11 session (found '${XDG_SESSION_TYPE:-unknown}'). Focus tracking uses xprop, which has no Wayland equivalent."
  ok "X11 session"

  for cmd in python3 xprop curl systemctl sudo awk; do
    command -v "$cmd" >/dev/null || die "missing required command: $cmd"
  done
  ok "required commands present"

  python3 -c 'import evdev' 2>/dev/null \
    || die "python3 evdev module missing. Install it with:  sudo apt install python3-evdev"
  ok "python3-evdev"

  for f in gate.py pick-mouse.py extension/manifest.json extension/background.js; do
    [[ -f "$REPO_DIR/$f" ]] || die "missing project file: $REPO_DIR/$f"
  done
  ok "project files in $REPO_DIR"

  local count
  count="$("$REPO_DIR/pick-mouse.py" --list 2>/dev/null | wc -l)"
  if (( count > 0 )); then
    ok "$count mouse device(s) detected"
  else
    todo "no mice detected under /dev/input/by-id"
    note "Plug both mice in before choosing which one is the left."
  fi
}

# ---------------------------------------------------------------- status board

show_status() {
  head_ "Current state"

  have_udev_rule  && ok "udev rule installed"            || todo "udev rule not installed"
  uinput_perms_ok && ok "/dev/uinput group-writable"     || todo "/dev/uinput not group-writable"
  in_group_file   && ok "user in 'input' group"          || todo "user not in 'input' group"

  if group_active; then
    ok "'input' group active in this session"
  elif in_group_file; then
    todo "'input' group NOT active yet — logout/login required"
  else
    todo "'input' group not active"
  fi

  local dev
  dev="$(configured_device || true)"
  if [[ -n $dev ]]; then
    ok "left mouse chosen: $(mouse_name "$dev")"
    note "$dev"
    [[ -e $dev ]] || bad "  ...but that device is not present right now"
  else
    todo "left mouse not chosen yet"
  fi

  if [[ -f $CONFIG_FILE ]]; then
    ok "config file present (pan idle $(configured_pan_ms) ms, recentre margin $(configured_recenter_margin) px)"
    note "$CONFIG_FILE"
  else
    todo "config file not created yet"
  fi

  unit_installed && ok "systemd unit installed"          || todo "systemd unit not installed"
  unit_enabled   && ok "service enabled at login"        || todo "service not enabled"
  unit_active    && ok "service running"                 || todo "service not running"

  if unit_active; then
    running_exec_ok && ok "service running this repo's build" \
                    || bad "service running a STALE build from another path"
  fi

  if daemon_up; then
    ok "daemon responding on port $PORT"
    device_attached && ok "mouse grabbed by daemon"      || todo "mouse NOT grabbed (permissions or unplugged)"
    daemon_settings_current && ok "daemon settings match the config" \
                            || bad "daemon is running stale settings — needs a restart"
    running_build_fresh && ok "daemon is running the current gate.py" \
                       || bad "daemon predates the current gate.py — needs a restart"
    extension_seen  && ok "Chrome extension reporting"   || todo "Chrome extension not reporting"
  else
    todo "daemon not responding on port $PORT"
    todo "Chrome extension status unknown (daemon down)"
  fi
}

all_done() {
  have_udev_rule && uinput_perms_ok && in_group_file && group_active \
    && [[ -f $CONFIG_FILE ]] && device_configured \
    && unit_installed && unit_enabled && unit_active \
    && service_exec_ok && running_exec_ok \
    && daemon_up && device_attached && daemon_settings_current \
    && running_build_fresh && extension_seen
}

# ---------------------------------------------------------------- steps

step_privileged() {
  if have_udev_rule && uinput_perms_ok && in_group_file; then
    return 0
  fi

  head_ "Step 1 — permissions (needs sudo)"
  echo "  This grants your user the ability to grab the mouse and create a virtual"
  echo "  input device, so the daemon can run unprivileged. It will:"
  have_udev_rule  || echo "    • install $UDEV_RULE"
  uinput_perms_ok || echo "    • set /dev/uinput to group 'input', mode 0660"
  in_group_file   || echo "    • add '$USER' to the 'input' group"
  echo
  confirm "  Proceed?" || { echo "  Skipped. Nothing changed."; exit 0; }

  if ! have_udev_rule; then
    sudo tee "$UDEV_RULE" >/dev/null <<'RULE'
# Let members of the "input" group create virtual input devices, so the
# onshape-mouse gate daemon can run as a normal user instead of root.
KERNEL=="uinput", GROUP="input", MODE="0660"
RULE
    sudo udevadm control --reload-rules
    ok "udev rule installed"
  fi

  if ! uinput_perms_ok; then
    # Apply to the live node too, so uinput itself needs no reboot.
    sudo chgrp input /dev/uinput
    sudo chmod 0660 /dev/uinput
    ok "/dev/uinput permissions set"
  fi

  if ! in_group_file; then
    sudo usermod -aG input "$USER"
    ok "'$USER' added to the 'input' group"
  fi
}

step_relogin_gate() {
  group_active && return 0

  head_ "Step 2 — log out and back in"
  cat <<EOF
  Your user is in the 'input' group now, but this session started before that
  and still carries the old group list. A new terminal is NOT enough: the
  systemd user manager also has to restart, which only happens on a full
  logout/login.

  ${BOLD}Log out, log back in, then run this script again.${OFF}
  It will detect what is already done and continue from Step 3.
EOF
  exit 0
}

save_device() {
  config_set device "$1"
  ok "left mouse set to: $(mouse_name "$1")"
  note "$1"
}

detect_device() {
  # The daemon grabs its device exclusively, so it must be stopped for detection
  # to see anything. step_service starts it again afterwards.
  if unit_active; then
    systemctl --user stop "$UNIT_NAME"
    note "stopped the service so the mouse can be read"
  fi

  echo
  printf '  %sMove the LEFT mouse now%s — waiting up to 10 seconds...\n' "$BOLD" "$OFF"
  local found
  if found="$("$REPO_DIR/pick-mouse.py" --detect 10)" && [[ -n $found ]]; then
    printf '  detected: %s%s%s\n' "$BOLD" "$(mouse_name "$found")" "$OFF"
    note "$found"
    if confirm "  Use this one?"; then
      save_device "$found"
      return 0
    fi
  else
    todo "no movement detected"
  fi
  return 1
}

step_choose_device() {
  head_ "Step 3 — which mouse is on the left"

  if [[ -n $DEVICE_OVERRIDE ]]; then
    [[ -e $DEVICE_OVERRIDE ]] || die "no such device: $DEVICE_OVERRIDE"
    save_device "$DEVICE_OVERRIDE"
    return 0
  fi

  local current
  current="$(configured_device || true)"

  if [[ -n $current ]] && (( ! RECONFIGURE )); then
    ok "already chosen: $(mouse_name "$current")"
    note "$current"
    note "Change it with:  $0 --reconfigure"
    return 0
  fi

  local paths=() names=() path name
  while IFS=$'\t' read -r path name; do
    paths+=("$path"); names+=("$name")
  done < <("$REPO_DIR/pick-mouse.py" --list)

  (( ${#paths[@]} )) || die "no mice found under /dev/input/by-id — plug them in and re-run"

  if (( ASSUME_YES )) && [[ -z $current ]]; then
    die "no mouse configured and --yes cannot guess. Re-run interactively, or pass --device PATH.
Available:
$(printf '  %s\n' "${paths[@]}")"
  fi

  echo "  Available mice:"
  local i
  for i in "${!paths[@]}"; do
    printf '    %s%d)%s %s\n' "$BOLD" "$((i+1))" "$OFF" "${names[$i]}"
    printf '       %s%s%s\n' "$DIM" "${paths[$i]}" "$OFF"
  done
  echo
  [[ -n $current ]] && { echo "  Currently: $(mouse_name "$current")"; echo; }

  while true; do
    local prompt="  Enter 1-${#paths[@]}, or 'd' to detect by moving the mouse"
    [[ -n $current ]] && prompt+=" [keep current]"
    local reply
    read -r -p "$prompt: " reply || reply=""

    if [[ -z $reply && -n $current ]]; then
      ok "keeping: $(mouse_name "$current")"
      return 0
    fi

    if [[ $reply == [Dd] ]]; then
      detect_device && return 0
      continue
    fi

    if [[ $reply =~ ^[0-9]+$ ]] && (( reply >= 1 && reply <= ${#paths[@]} )); then
      save_device "${paths[$((reply-1))]}"
      return 0
    fi

    echo "  Not a valid choice."
  done
}

step_service() {
  head_ "Step 4 — background service"

  local desired
  desired="$(cat <<EOF
[Unit]
Description=Gate the left-hand mouse to onshape.com only
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
# DISPLAY is already in the systemd user environment; XAUTHORITY is not.
Environment=XAUTHORITY=%t/gdm/Xauthority
ExecStart=$REPO_DIR/gate.py
Restart=always
RestartSec=2

[Install]
WantedBy=graphical-session.target
EOF
)"

  # Restart only when something actually changed. A gratuitous restart drops the
  # extension's last report, which makes Step 6 nag about a perfectly good setup.
  local need_restart=0

  mkdir -p "$(dirname "$UNIT_PATH")"
  if [[ ! -f $UNIT_PATH ]] || [[ "$(cat "$UNIT_PATH")" != "$desired" ]]; then
    printf '%s\n' "$desired" > "$UNIT_PATH"
    ok "unit written to $UNIT_PATH"
    need_restart=1
  else
    ok "unit already up to date"
  fi

  chmod +x "$REPO_DIR/gate.py" "$REPO_DIR/pick-mouse.py"
  systemctl --user daemon-reload
  systemctl --user enable "$UNIT_NAME" >/dev/null 2>&1 || true

  unit_active || need_restart=1
  if unit_active && ! running_exec_ok; then
    todo "replacing a service running from a different path"
    need_restart=1
  fi
  if ! daemon_up; then
    need_restart=1
  elif ! daemon_settings_current; then
    todo "config changed since the daemon started"
    need_restart=1
  elif ! running_build_fresh; then
    todo "gate.py changed since the daemon started"
    need_restart=1
  fi

  if (( ! need_restart )); then
    ok "daemon already running with the current settings"
    return 0
  fi

  systemctl --user restart "$UNIT_NAME"

  printf '  waiting for daemon'
  local i=0
  until daemon_up; do
    if (( ++i > 15 )); then
      echo
      bad "daemon did not come up"
      echo
      echo "  Recent log:"
      journalctl --user -u "$UNIT_NAME" -n 20 --no-pager | sed 's/^/    /'
      die "see the log above"
    fi
    printf '.'
    sleep 1
  done
  echo
  ok "daemon responding on port $PORT"
}

step_device() {
  head_ "Step 5 — mouse grab"

  if device_attached; then
    ok "daemon has grabbed $(mouse_name "$(configured_device)")"
    return 0
  fi

  local dev
  dev="$(configured_device || true)"
  if [[ -n $dev && ! -e $dev ]]; then
    todo "the chosen mouse is not plugged in"
    note "$dev"
    note "Plug it in; the daemon picks it up automatically."
    return 0
  fi

  bad "daemon could not grab the mouse"
  echo
  echo "  Recent log:"
  journalctl --user -u "$UNIT_NAME" -n 20 --no-pager | sed 's/^/    /'
  die "the device exists but was not grabbed; see the log above"
}

step_extension() {
  head_ "Step 6 — Chrome extension"

  if extension_seen; then
    ok "extension is reporting to the daemon"
    return 0
  fi

  cat <<EOF
  The daemon has never heard from the Chrome extension, so the gate will stay
  closed. Load it now:

    1. Open  ${BOLD}chrome://extensions${OFF}
    2. Turn on  ${BOLD}Developer mode${OFF}  (top right)
    3. Remove any existing broken "Onshape Mouse Gate" entry
    4. Click  ${BOLD}Load unpacked${OFF}
    5. Select  ${BOLD}$REPO_DIR/extension${OFF}

EOF

  if (( ASSUME_YES )); then
    todo "skipping the wait (--yes); re-run --status once it is loaded"
    return 0
  fi

  read -r -p "  Press Enter once loaded (or Ctrl-C to finish later)... " _ || return 0

  printf '  checking'
  local i=0
  until extension_seen; do
    if (( ++i > 20 )); then
      echo
      todo "still no report from the extension"
      note "Check chrome://extensions for errors on 'Onshape Mouse Gate',"
      note "then re-run:  $0 --status"
      return 0
    fi
    printf '.'
    sleep 1
  done
  echo
  ok "extension is reporting to the daemon"
}

# ---------------------------------------------------------------- uninstall

do_uninstall() {
  printf '%sOnshape trackball gate — uninstall%s\n' "$BOLD" "$OFF"

  head_ "Will remove"
  unit_active     && echo "    • stop $UNIT_NAME"
  unit_enabled    && echo "    • disable $UNIT_NAME"
  unit_installed  && echo "    • $UNIT_PATH"
  [[ -d $CONFIG_DIR ]] && echo "    • $CONFIG_DIR (your mouse choice)"
  if ! unit_active && ! unit_enabled && ! unit_installed && [[ ! -d $CONFIG_DIR ]]; then
    echo "    • nothing — no service or config found"
  fi

  head_ "Will ask about (needs sudo)"
  have_udev_rule  && echo "    • $UDEV_RULE" || echo "    • udev rule: already gone"
  in_group_file   && echo "    • '$USER' in the 'input' group" || echo "    • group membership: already gone"

  head_ "Will NOT touch"
  echo "    • $REPO_DIR (this directory and its files)"
  echo "    • the Chrome extension — remove that yourself at chrome://extensions"
  echo

  confirm "  Proceed?" || { echo "  Cancelled. Nothing changed."; exit 0; }

  head_ "Removing"

  if unit_active; then
    systemctl --user stop "$UNIT_NAME" && ok "service stopped"
  fi
  if unit_enabled; then
    systemctl --user disable "$UNIT_NAME" >/dev/null 2>&1 || true
    ok "service disabled"
  fi
  if unit_installed; then
    rm -f "$UNIT_PATH"
    ok "unit file removed"
  fi
  systemctl --user daemon-reload
  systemctl --user reset-failed "$UNIT_NAME" >/dev/null 2>&1 || true

  if [[ -d $CONFIG_DIR ]]; then
    rm -rf "$CONFIG_DIR"
    ok "config removed"
  fi

  # The gate is gone, so confirm the mouse is genuinely back under X control.
  if [[ -n "${DISPLAY:-}" ]] && command -v xinput >/dev/null; then
    if xinput list 2>/dev/null | grep -q "Onshape-gated Mouse"; then
      bad "a virtual 'Onshape-gated Mouse' is still registered with X"
      note "Something is still running; check: pgrep -af gate.py"
    else
      ok "no virtual device left behind; your mice are back to normal"
    fi
  fi

  if have_udev_rule; then
    head_ "udev rule"
    echo "  Removing this restores /dev/uinput to root-only at next boot. Keep it if"
    echo "  you use other tools that create virtual input devices."
    if confirm_risky "  Remove $UDEV_RULE?"; then
      sudo rm -f "$UDEV_RULE"
      sudo udevadm control --reload-rules
      sudo chgrp root /dev/uinput 2>/dev/null || true
      sudo chmod 0600 /dev/uinput 2>/dev/null || true
      ok "udev rule removed and /dev/uinput reset to root:root 0600"
    else
      todo "kept the udev rule"
    fi
  fi

  if in_group_file; then
    head_ "'input' group"
    echo "  input-remapper and similar tools may rely on this. It is harmless to keep,"
    echo "  and removal only takes effect after a logout/login."
    if confirm_risky "  Remove '$USER' from the 'input' group?"; then
      sudo gpasswd -d "$USER" input >/dev/null
      ok "removed from 'input' group (effective after logout/login)"
    else
      todo "left '$USER' in the 'input' group"
    fi
  fi

  head_ "Done"
  cat <<EOF
  The gate is uninstalled and both mice behave normally again.

  Still to do by hand:
    • chrome://extensions — remove "Onshape Mouse Gate"

  This directory was left untouched. Reinstall any time with:
    $0
EOF
}

# ---------------------------------------------------------------- main

if (( UNINSTALL )); then
  do_uninstall
  exit 0
fi

printf '%sOnshape trackball gate — setup%s\n' "$BOLD" "$OFF"
printf '%s%s%s\n' "$DIM" "$REPO_DIR" "$OFF"

# Before preflight, and before the status board. The config needs nothing but a
# writable home directory, while preflight dies on a missing evdev or a Wayland
# session -- so leaving it until later means the very installs that stop early are
# the ones left with no file to read or edit. Written first, it is always there.
if (( ! STATUS_ONLY )); then
  head_ "Configuration"
  ensure_config
  ensure_config_keys
  [[ -f $CONFIG_FILE ]] && ok "using $CONFIG_FILE"
fi

preflight

show_status

if (( STATUS_ONLY )); then
  echo
  all_done && echo "${GRN}Everything is set up.${OFF}" \
           || echo "${YEL}Setup is incomplete — run without --status to continue.${OFF}"
  exit 0
fi

if all_done && (( ! RECONFIGURE )) && [[ -z $DEVICE_OVERRIDE ]]; then
  head_ "Nothing to do"
  echo "  Everything is already set up."
  echo
  echo "  ${DIM}Change the mouse:  $0 --reconfigure${OFF}"
  echo "  ${DIM}Live state:        curl -s localhost:$PORT/status${OFF}"
  exit 0
fi

step_privileged
step_relogin_gate
step_choose_device
step_service
step_device
step_extension

head_ "Done"
cat <<EOF
  Your left mouse ($(mouse_name "$(configured_device)")) now works only while an
  onshape.com tab is frontmost in Chrome:

    move                     pan
    right button + move      rotate
    wheel                    zoom
    left button              select

  Settings live in $CONFIG_FILE
  (pan idle release is currently $(configured_pan_ms) ms).

  Useful commands:
    $0 --status
    $0 --reconfigure                     ${DIM}# pick a different mouse${OFF}
    curl -s localhost:$PORT/status
    systemctl --user stop $UNIT_NAME     ${DIM}# back to a normal mouse${OFF}
EOF
