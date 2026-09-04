"""Drive Translator with synthetic events against a stub channel."""
import os, sys, time, types

src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gate.py')).read()
src = src.replace('if __name__ == "__main__":', 'if False:')
ns = {}
exec(compile(src, 'gate.py', 'exec'), ns)
Translator = ns['Translator']

# Codes come from the module under test, not from evdev directly, so this suite runs
# on Windows as well — where evdev cannot be installed at all. The values are the
# same either way; backend_linux asserts as much at import.
ecodes = ns['ecodes']


class InputEvent:
    """evdev's constructor signature over the three fields Translator reads."""
    __slots__ = ('sec', 'usec', 'type', 'code', 'value')

    def __init__(self, sec, usec, etype, code, value):
        self.sec, self.usec = sec, usec
        self.type, self.code, self.value = etype, code, value

# The cases below are about gesture mechanics and use small synthetic motions, so
# the dead zone is off for them. It has its own section at the end.
ns['PAN_DEADZONE'] = 0

# And for which gesture the right button performs: the cases below are written
# against the original mapping (bare motion pans, the button rotates) and are about
# gesture mechanics, not this setting. Its own section at the end flips it.
ns['PAN_REQUIRES_RIGHT_BUTTON'] = False


class StubChannel:
    """Records every message sent, in order. `connected` defaults True — the one
    sink there is now, so nothing here is ever silently dropped unless a case sets
    it False deliberately."""
    def __init__(self, connected=True):
        self.log = []
        self.connected = connected

    def send(self, message):
        self.log.append(message)


def ev(t, c, v): return InputEvent(0, 0, t, c, v)
def motion(dx): return [ev(ecodes.EV_REL, ecodes.REL_X, dx), ev(ecodes.EV_SYN, 0, 0)]
def button(code, val): return [ev(ecodes.EV_KEY, code, val), ev(ecodes.EV_SYN, 0, 0)]

PRESS_PAN = {"type": "press", "gesture": "pan"}
PRESS_ROTATE = {"type": "press", "gesture": "rotate"}
RELEASE = {"type": "release"}


def run(name, events, expect, post=None):
    """Compares press/release/tap/click/wheel messages, which is where the real
    hazards live — gesture ordering, hand-offs, the middle button never appearing.
    Motion messages are filtered out: pinning their exact values here made these
    cases break on every unrelated change to ROTATE_SCALE etc. — motion has its own
    dedicated section further down.
    """
    channel = StubChannel()
    tr = Translator(channel)
    for e in events: tr.handle(e)
    if post: post(tr)
    got = [m for m in channel.log if m.get('type') != 'motion']
    ok = got == expect
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"      expected: {expect}")
        print(f"      got     : {got}")
        print(f"      full log: {channel.log}")
    return ok

# The gesture cases below need a region the cursor is allowed to occupy: a pan
# will not start without one, because a press with nowhere known-safe to land is
# exactly what puts the right button on a toolbar.
class StubGate:
    """geometry() is the Chrome window; view_rect() is what the cursor must stay
    inside — the region verified to be 3D view and nothing else. They differ so a test
    can tell which one the code actually consulted.

    view_rect() deliberately does not fall back to the window when no canvas is known:
    the daemon does not either, because a pan press needs somewhere known to be safe.
    Passing canvas=None is therefore how a test says "no safe region right now"."""
    def __init__(self, geom, canvas=None):
        self._geom = geom
        self._canvas = canvas
    def geometry(self): return self._geom
    def view_rect(self): return self._canvas

WINDOW = (0, 0, 1920, 1080)          # x, y, w, h
CENTRE = (960, 540)

ns['GATE'] = StubGate(WINDOW, WINDOW)

results = []

# 1. Bare motion opens a pan stroke and holds it.
results.append(run("motion alone starts a pan drag",
    motion(5) + motion(7),
    [PRESS_PAN]))

# 2. Going idle past PAN_IDLE_RELEASE ends the stroke.
def idle_then_tick(tr):
    tr._last_motion -= 1.0
    tr.tick()
results.append(run("idle releases the pan gesture",
    motion(5),
    [PRESS_PAN, RELEASE],
    post=idle_then_tick))

# 3. The physical right button hands the same drag over to rotate: the current
# stroke closes and immediately reopens as rotate, without a duplicate press.
results.append(run("the right button hands the same drag off to rotate",
    motion(5) + button(ecodes.BTN_RIGHT, 1) + motion(9),
    [PRESS_PAN, RELEASE, PRESS_ROTATE]))

# 4. Releasing it ends the gesture, and the next movement starts a fresh pan.
results.append(run("pan resumes after the right button is released",
    button(ecodes.BTN_RIGHT, 1) + motion(3) + button(ecodes.BTN_RIGHT, 0) + motion(4),
    [PRESS_ROTATE, RELEASE, PRESS_PAN]))

# 5. The gate closing mid-stroke must strand nothing open.
results.append(run("gate close releases the button",
    motion(5),
    [PRESS_PAN, RELEASE],
    post=lambda tr: tr.release_all()))

# 6. Including a button the user is physically holding.
results.append(run("gate close releases a real held button",
    button(ecodes.BTN_RIGHT, 1),
    [PRESS_ROTATE, RELEASE],
    post=lambda tr: tr.release_all()))

# 7. Wheel is zoom: forwarded as its own message, and it starts no gesture.
results.append(run("wheel is forwarded and starts no gesture",
    [ev(ecodes.EV_REL, ecodes.REL_WHEEL, 1), ev(ecodes.EV_SYN, 0, 0)],
    [{"type": "wheel", "code": "REL_WHEEL", "value": 1}]))

# 8. The left button ends the stroke, then taps space to clear the selection. The
# click itself never reaches the page.
results.append(run("left button ends the stroke and taps space",
    motion(5) + button(ecodes.BTN_LEFT, 1),
    [PRESS_PAN, RELEASE, {"type": "tap", "key": "space"}]))

# Passing the click through instead is one config value away.
saved_code = ns['LEFT_CLICK_CODE']
ns['LEFT_CLICK_CODE'] = None
results.append(run("with left_click_key = none, the click passes through",
    motion(5) + button(ecodes.BTN_LEFT, 1),
    [PRESS_PAN, RELEASE, {"type": "click", "code": "LEFT", "value": 1}]))
ns['LEFT_CLICK_CODE'] = saved_code

# Nothing anywhere may ever touch the middle button: two presses inside the
# double-click window are a double middle-click, which Onshape reads as Zoom to Fit.
_channel = StubChannel()
_tr = Translator(_channel)
for _e in (motion(5) + button(ecodes.BTN_RIGHT, 1) + button(ecodes.BTN_RIGHT, 0)
           + motion(3) + button(ecodes.BTN_LEFT, 1)):
    _tr.handle(_e)
_tr.release_all()
_no_middle = not any(m.get('code') == 'MIDDLE' for m in _channel.log)
print(f"{'PASS' if _no_middle else 'FAIL'}  the middle button is never touched, by any path")
if not _no_middle:
    print(f"      log={_channel.log}")
results.append(_no_middle)

# --- gesture hand-off, in more detail --------------------------------------------
# Bundled with the ordering cases above under the old (OS-injection) design, because
# a real Ctrl device and a real button device could reach Chrome out of order. There
# is only one sink now, and every message is already atomic and ordered — nothing
# left to race — but the hand-off logic itself still deserves direct coverage.

def gesture_setup():
    channel = StubChannel()
    tr = Translator(channel)
    ns['GATE'] = StubGate(WINDOW, WINDOW)
    return tr, channel

tr, channel = gesture_setup()
for e in motion(5):
    tr.handle(e)
channel.log.clear()
tr.handle(ev(ecodes.EV_KEY, ecodes.BTN_RIGHT, 1))
handover = (channel.log == [RELEASE, PRESS_ROTATE]
            and not tr._panning and tr._stroke_open and tr._gesture == "rotate")
print(f"{'PASS' if handover else 'FAIL'}  physical right hands off without a duplicate press")
if not handover:
    print(f"      panning={tr._panning}, stroke_open={tr._stroke_open}, "
          f"gesture={tr._gesture}, log={channel.log}")
results.append(handover)

channel.log.clear()
tr.handle(ev(ecodes.EV_KEY, ecodes.BTN_RIGHT, 0))
released = (channel.log == [RELEASE] and not tr._stroke_open and tr._gesture is None)
print(f"{'PASS' if released else 'FAIL'}  releasing right ends the gesture exactly once")
if not released:
    print(f"      stroke_open={tr._stroke_open}, log={channel.log}")
results.append(released)

# A wheel turn ends an open pan first, so it reaches the page as a clean scroll
# rather than a scroll with the pan button still held.
tr, channel = gesture_setup()
for e in motion(5):
    tr.handle(e)
channel.log.clear()
tr.handle(ev(ecodes.EV_REL, ecodes.REL_WHEEL, 1))
wheel_ends_pan = (channel.log == [RELEASE, {"type": "wheel", "code": "REL_WHEEL", "value": 1}]
                   and not tr._panning)
print(f"{'PASS' if wheel_ends_pan else 'FAIL'}  a wheel turn ends an open pan first")
if not wheel_ends_pan:
    print(f"      panning={tr._panning}, log={channel.log}")
results.append(wheel_ends_pan)

# Gate close releases both the button-held and bare-motion cases cleanly.
tr, channel = gesture_setup()
for e in motion(5):
    tr.handle(e)
channel.log.clear()
tr.release_all()
clean = not tr._stroke_open and not tr._panning and channel.log == [RELEASE]
print(f"{'PASS' if clean else 'FAIL'}  gate close releases the open stroke cleanly")
if not clean:
    print(f"      stroke_open={tr._stroke_open}, panning={tr._panning}, log={channel.log}")
results.append(clean)

# --- canvas rect ----------------------------------------------------------------
# The cursor should be penned inside the 3D canvas, not merely inside the window:
# Onshape's toolbars and feature tree are inside the window but outside the canvas,
# and a right-button release over one of them opens a context menu.

parse_rect = ns['parse_rect']
for label, raw, want in [
    ("a well-formed rect", {"x": 10, "y": 20, "w": 300, "h": 400}, (10, 20, 300, 400)),
    ("null", None, None),
    ("a missing field", {"x": 1, "y": 2, "w": 3}, None),
    ("a non-numeric field", {"x": "a", "y": 2, "w": 3, "h": 4}, None),
    ("a zero-area rect", {"x": 1, "y": 2, "w": 0, "h": 4}, None),
    ("a negative size", {"x": 1, "y": 2, "w": -5, "h": 4}, None),
]:
    got = parse_rect(raw)
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  parse_rect rejects/accepts {label}")
    if not ok:
        print(f"      expected {want}, got {got}")
    results.append(ok)

WINDOW_RECT = (0, 0, 1920, 1080)
CANVAS_RECT = (400, 200, 900, 700)

gate = ns['Gate']()
gate.set_chrome_focused(True, WINDOW_RECT, "0x1")
# The window is not an acceptable substitute for the view: it contains the tab strip,
# the bookmarks bar and the feature tree. With no canvas known there is no safe region,
# and saying so is the only honest answer.
no_canvas = gate.view_rect() is None
print(f"{'PASS' if no_canvas else 'FAIL'}  view_rect reports nothing when no canvas is known")
results.append(no_canvas)

gate.set_canvas(CANVAS_RECT)
prefers = gate.view_rect() == CANVAS_RECT
print(f"{'PASS' if prefers else 'FAIL'}  view_rect prefers the canvas when it is fresh")
results.append(prefers)

gate._canvas_at -= (ns['CANVAS_STALE_AFTER'] + 1)
stale = gate.view_rect() is None
print(f"{'PASS' if stale else 'FAIL'}  view_rect reports nothing once the canvas goes stale")
results.append(stale)

# --- diagnosing a context menu ----------------------------------------------------
# A context menu that opens mid-pan is the one failure with no trace: it is browser UI,
# it is gone by the time you look, and the state that caused it has moved on. The page
# reports the event; the daemon pairs it with what it was doing. What matters is that
# the pairing names a *distinct* cause for each case, because the fixes differ.

class StubTranslator:
    def __init__(self, panning, last_release=None):
        self._panning = panning
        self.last_release = last_release

def classify(panning, last_release, event):
    g = ns['Gate']()
    g.translator = StubTranslator(panning, last_release)
    g.record_context_menu([event])
    return g._context_menus[-1]

fresh = lambda: {"at": time.monotonic()}

cases = [
    ("an overlay inside the region we called safe",
     True, fresh(),
     {"onCanvas": False, "inRegion": True, "target": "div.toolbar", "x": 900, "y": 500},
     "probe missed it"),

    ("an overlay outside the region",
     True, fresh(),
     {"onCanvas": False, "inRegion": False, "target": "div.tree", "x": 100, "y": 500},
     "should not have been there"),

    ("the canvas itself",
     True, fresh(),
     {"onCanvas": True, "inRegion": True, "target": "canvas", "x": 900, "y": 500},
     "Onshape's own canvas menu"),

    ("nothing to do with us",
     False, None,
     {"onCanvas": False, "inRegion": None, "target": "div.menu", "x": 10, "y": 10},
     "not during a pan"),
]

for label, panning, release, event, expect in cases:
    record = classify(panning, release, event)
    ok = expect in record["why"]
    print(f"{'PASS' if ok else 'FAIL'}  context menu on {label} is diagnosed")
    if not ok:
        print(f"      expected {expect!r} in why; got {record['why']!r}")
    results.append(ok)

# The native menu actually appearing is a different fact from the event firing: Onshape
# suppresses the ones it handles itself, and only the unsuppressed ones are the browser
# menu the user complains about.
suppressed = classify(True, fresh(),
                      {"onCanvas": True, "inRegion": True, "target": "canvas",
                       "x": 900, "y": 500, "prevented": True})
shown = classify(True, fresh(),
                 {"onCanvas": True, "inRegion": True, "target": "canvas",
                  "x": 900, "y": 500, "prevented": False})
distinguishes = (suppressed["menu_shown"] is False
                 and shown["menu_shown"] is True)
print(f"{'PASS' if distinguishes else 'FAIL'}  a suppressed menu is told apart from one the browser showed")
results.append(distinguishes)

# Only the most recent reports are kept, or --status becomes a log file.
g = ns['Gate']()
g.translator = StubTranslator(True, fresh())
g.record_context_menu([{"onCanvas": True, "inRegion": True, "target": f"c{i}",
                        "x": 1, "y": 1} for i in range(ns['CONTEXT_MENU_HISTORY'] + 15)])
bounded = len(g._context_menus) == ns['CONTEXT_MENU_HISTORY']
print(f"{'PASS' if bounded else 'FAIL'}  the context-menu history stays bounded")
if not bounded:
    print(f"      kept {len(g._context_menus)}")
results.append(bounded)

# Junk off the network must not take the daemon down with it.
g = ns['Gate']()
g.translator = StubTranslator(True, fresh())
try:
    g.record_context_menu(["nonsense", None, 42, {}])
    survived = True
except Exception as exc:
    survived = False
    print(f"      raised {exc!r}")
print(f"{'PASS' if survived else 'FAIL'}  malformed context-menu reports are ignored, not fatal")
results.append(survived)

# --- left click clears the selection ---------------------------------------------
# The gated mouse has no real, clickable cursor of its own any more, so a real left
# click would be meaningless anyway. Onshape clears the whole selection on space.

tr, channel = gesture_setup()
for e in motion(5):
    tr.handle(e)
channel.log.clear()
tr.handle(ev(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
tapped = (channel.log == [RELEASE, {"type": "tap", "key": "space"}] and tr.left_taps == 1)
print(f"{'PASS' if tapped else 'FAIL'}  a left click ends the pan and taps space")
if not tapped:
    print(f"      log={channel.log}")
results.append(tapped)

swallowed = not any(m.get('type') == 'click' for m in channel.log)
print(f"{'PASS' if swallowed else 'FAIL'}  the left click itself is swallowed")
if not swallowed:
    print(f"      log={channel.log}")
results.append(swallowed)

# The release is swallowed too, so it cannot tap twice.
channel.log.clear()
tr.handle(ev(ecodes.EV_KEY, ecodes.BTN_LEFT, 0))
once = channel.log == [] and tr.left_taps == 1
print(f"{'PASS' if once else 'FAIL'}  the button release taps nothing further")
if not once:
    print(f"      log={channel.log}, taps={tr.left_taps}")
results.append(once)

# left_click_key = none restores an ordinary click.
saved = ns['LEFT_CLICK_CODE']
ns['LEFT_CLICK_CODE'] = None
tr2, channel2 = gesture_setup()
tr2.handle(ev(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
passthrough = ({"type": "click", "code": "LEFT", "value": 1} in channel2.log
                and tr2.left_taps == 0)
print(f"{'PASS' if passthrough else 'FAIL'}  with the key disabled, the click passes through")
if not passthrough:
    print(f"      log={channel2.log}")
results.append(passthrough)
ns['LEFT_CLICK_CODE'] = saved

# --- the stale _right_down hole --------------------------------------------------
# If that flag went stale, ending a pan could skip the button release.

tr, channel = gesture_setup()
for e in motion(5):
    tr.handle(e)
tr._right_down = True              # pretend it went stale
channel.log.clear()
tr._end_pan()
released = channel.log == [RELEASE] and not tr._stroke_open
print(f"{'PASS' if released else 'FAIL'}  a stale right_down cannot strand the stroke open")
if not released:
    print(f"      stroke_open={tr._stroke_open}, log={channel.log}")
results.append(released)

# Closing the gate must clear the flag even on the idle path, which used to return
# early and leave it set.
tr2, channel2 = gesture_setup()
tr2._right_down = True             # idle: nothing panning, nothing held
tr2.release_all()
cleared = tr2._right_down is False
print(f"{'PASS' if cleared else 'FAIL'}  closing the gate clears right_down even when idle")
results.append(cleared)

# --- pan dead zone ---------------------------------------------------------------
# A pan should need a deliberate push, not a nudge. Measured as net displacement, so
# jitter that wanders out and back never trips it.

ns['PAN_DEADZONE'] = 10

def dz_setup():
    channel = StubChannel()
    tr = Translator(channel)
    ns['GATE'] = StubGate(WINDOW, WINDOW)
    return tr, channel

def pressed(channel):
    return PRESS_PAN in channel.log

# Under the threshold: no pan starts, and nothing is sent — there is no stroke to
# send motion into, and no real cursor left to track a hand through the dead zone
# with (see design.md's "one sink" decision).
tr, channel = dz_setup()
for e in motion(4) + motion(4):          # 8px total, still inside
    tr.handle(e)
inside = not tr._panning and not pressed(channel) and channel.log == []
print(f"{'PASS' if inside else 'FAIL'}  under the dead zone: no pan starts, nothing is sent")
if not inside:
    print(f"      panning={tr._panning}, log={channel.log}")
results.append(inside)

# Crossing it starts the pan.
for e in motion(4):                      # 12px total
    tr.handle(e)
crossed = tr._panning and pressed(channel)
print(f"{'PASS' if crossed else 'FAIL'}  crossing the dead zone starts the pan")
if not crossed:
    print(f"      panning={tr._panning}, log={channel.log}")
results.append(crossed)

# Jitter out and back nets zero, so it must never trip.
tr, channel = dz_setup()
for _ in range(6):
    for e in motion(8) + motion(-8):
        tr.handle(e)
jitter = not tr._panning and not pressed(channel)
print(f"{'PASS' if jitter else 'FAIL'}  jitter that returns to origin never starts a pan")
if not jitter:
    print(f"      panning={tr._panning}, travel=({tr._travel_x},{tr._travel_y})")
results.append(jitter)

# Diagonal counts as distance, not per-axis.
tr, channel = dz_setup()
for e in ([ev(ecodes.EV_REL, ecodes.REL_X, 8), ev(ecodes.EV_REL, ecodes.REL_Y, 8),
           ev(ecodes.EV_SYN, 0, 0)]):
    tr.handle(e)
diagonal = tr._panning and pressed(channel)
print(f"{'PASS' if diagonal else 'FAIL'}  a diagonal push crosses on distance, not per axis")
if not diagonal:
    print(f"      panning={tr._panning}, travel=({tr._travel_x},{tr._travel_y})")
results.append(diagonal)

# Each stroke earns its own dead zone.
tr, channel = dz_setup()
for e in motion(12):
    tr.handle(e)
tr._end_pan()
channel.log.clear()
for e in motion(4):
    tr.handle(e)
re_armed = not tr._panning and not pressed(channel)
print(f"{'PASS' if re_armed else 'FAIL'}  the dead zone re-arms after a stroke ends")
if not re_armed:
    print(f"      panning={tr._panning}, log={channel.log}")
results.append(re_armed)

# A stale nudge expires rather than combining with a later one.
tr, channel = dz_setup()
for e in motion(8):
    tr.handle(e)
tr._last_motion = time.monotonic() - (ns['PAN_IDLE_RELEASE'] + 0.01)
tr.tick()
expired = tr._travel_x == 0 and tr._travel_y == 0
print(f"{'PASS' if expired else 'FAIL'}  a stale nudge expires instead of accumulating")
if not expired:
    print(f"      travel=({tr._travel_x},{tr._travel_y})")
results.append(expired)

# Zero disables it entirely.
ns['PAN_DEADZONE'] = 0
tr, channel = dz_setup()
for e in motion(1):
    tr.handle(e)
disabled = tr._panning and pressed(channel)
print(f"{'PASS' if disabled else 'FAIL'}  pan_deadzone_px = 0 starts panning immediately")
results.append(disabled)

# --- nothing under the pointer but canvas ----------------------------------------
# A pan opens a stroke that Onshape reads as a real drag, so it may only ever start
# inside a region the extension has verified is 3D view and nothing else. No region,
# no pan: the alternative is a right-button release on a toolbar, which opens a
# context menu or activates whatever it lands on.

ns['PAN_DEADZONE'] = 0

# No safe region at all: nothing is sent, no gesture opens.
channel = StubChannel()
tr = Translator(channel)
ns['GATE'] = StubGate(WINDOW, None)
for e in motion(5) + motion(7):
    tr.handle(e)
no_press = not tr._panning and channel.log == []
print(f"{'PASS' if no_press else 'FAIL'}  no verified view region: nothing is sent, no pan starts")
if not no_press:
    print(f"      panning={tr._panning} log={channel.log}")
results.append(no_press)

# And it is not a permanent refusal: the region coming back re-enables panning.
ns['GATE'] = StubGate(WINDOW, WINDOW)
for e in motion(5):
    tr.handle(e)
recovers = tr._panning and PRESS_PAN in channel.log
print(f"{'PASS' if recovers else 'FAIL'}  panning resumes once a view region is reported again")
results.append(recovers)

# The region vanishing mid-stroke — a dialog opening over the view, or the extension
# going quiet — must let go of the stroke rather than keep it open on whatever just
# appeared.
channel = StubChannel()
tr = Translator(channel)
ns['GATE'] = StubGate(WINDOW, WINDOW)
for e in motion(5):
    tr.handle(e)
started = tr._panning
channel.log.clear()
ns['GATE'] = StubGate(WINDOW, None)      # the safe region disappears
for e in motion(3):
    tr.handle(e)
let_go = started and not tr._panning and RELEASE in channel.log
print(f"{'PASS' if let_go else 'FAIL'}  the view region vanishing mid-stroke releases the stroke")
if not let_go:
    print(f"      started={started} panning={tr._panning} log={channel.log}")
results.append(let_go)

# --- pan_requires_right_button: swapping which gesture the button performs -------
# Everything above ran with the original mapping (bare motion pans, the button
# rotates). This flips it: the button should now bracket pan, and bare motion should
# rotate. Idle release and hand-off are exactly what the cases above already
# covered, so most of these only check which gesture opens. The dead zone is the
# one thing that does NOT carry over unchanged — it applies only to whichever
# gesture bare motion drives when that is pan, so flipping the mapping flips
# whether it applies at all, and that gets its own dedicated case below.

ns['PAN_REQUIRES_RIGHT_BUTTON'] = True
ns['PAN_DEADZONE'] = 0
ns['GATE'] = StubGate(WINDOW, WINDOW)

results.append(run("bare motion rotates instead of panning",
    motion(5) + motion(7),
    [PRESS_ROTATE]))

# Rotate gets no dead zone at all under this mapping — see the PAN_DEADZONE comment.
# A single, tiny sample must open the stroke immediately, not wait for 10px to
# accumulate; otherwise a small deliberate rotate loses exactly the distance Onshape's
# own click-vs-drag check needs to see it as a drag rather than a click.
ns['PAN_DEADZONE'] = 10
results.append(run("rotate ignores the dead zone entirely",
    motion(1),
    [PRESS_ROTATE]))
ns['PAN_DEADZONE'] = 0

results.append(run("holding the right button pans instead of rotating",
    button(ecodes.BTN_RIGHT, 1) + motion(9),
    [PRESS_PAN]))

results.append(run("releasing the button after a button-held pan ends the stroke",
    button(ecodes.BTN_RIGHT, 1) + motion(5) + button(ecodes.BTN_RIGHT, 0),
    [PRESS_PAN, RELEASE]))

# Idle release still ends a bare-motion rotate.
results.append(run("idle release ends a bare-motion rotate",
    motion(5),
    [PRESS_ROTATE, RELEASE],
    post=idle_then_tick))

# The hand-off now runs the other way: a rotate already under way (bare motion) must
# not be double-pressed when the button comes down — it closes and reopens as pan.
channel = StubChannel()
tr = Translator(channel)
for e in motion(5) + button(ecodes.BTN_RIGHT, 1) + motion(9):
    tr.handle(e)
handed_off_to_pan = ([m for m in channel.log if m.get('type') != 'motion']
                      == [PRESS_ROTATE, RELEASE, PRESS_PAN]
                      and not tr._panning and tr._stroke_open and tr._gesture == "pan")
print(f"{'PASS' if handed_off_to_pan else 'FAIL'}  the button hands a rotate off to pan without a double press")
if not handed_off_to_pan:
    print(f"      panning={tr._panning}, stroke_open={tr._stroke_open}, "
          f"gesture={tr._gesture}, log={channel.log}")
results.append(handed_off_to_pan)

# The button-held gesture (pan, under this mapping) must resume once the physical
# button never let go — a channel drop mid-hold used to require an actual
# release/press to recover from, because nothing reopened the stroke while
# _right_down stayed true throughout.
channel = StubChannel()
tr = Translator(channel)
for e in button(ecodes.BTN_RIGHT, 1) + motion(5):
    tr.handle(e)
tr.channel_disconnected()                    # the channel drops mid-hold
channel.connected = True                     # ...and comes back
channel.log.clear()
for e in motion(4):
    tr.handle(e)
resumed_on_button = ([m for m in channel.log if m.get('type') != 'motion'] == [PRESS_PAN]
                      and tr._stroke_open)
print(f"{'PASS' if resumed_on_button else 'FAIL'}  "
      "a button-held pan resumes after a channel drop without a real release/press")
if not resumed_on_button:
    print(f"      stroke_open={tr._stroke_open}, log={channel.log}")
results.append(resumed_on_button)

ns['PAN_REQUIRES_RIGHT_BUTTON'] = False    # restore the default this file exercises

# --- rotate scale ------------------------------------------------------------------
# Rotate is plain motion with no scaling of its own, so ROTATE_SCALE is what tones it
# down. Pan must never be touched by it — panning already has its own dedicated feel.

def rs_setup():
    channel = StubChannel()
    tr = Translator(channel)
    ns['GATE'] = StubGate(WINDOW, WINDOW)
    return tr, channel

def motion_dx(channel):
    return sum(m['dx'] for m in channel.log if m.get('type') == 'motion')

ns['PAN_REQUIRES_RIGHT_BUTTON'] = True     # bare motion rotates
ns['ROTATE_SCALE'] = 0.5

tr, channel = rs_setup()
for e in motion(10):
    tr.handle(e)
halved = motion_dx(channel) == 5
print(f"{'PASS' if halved else 'FAIL'}  rotate motion is scaled down by ROTATE_SCALE")
if not halved:
    print(f"      log={channel.log}")
results.append(halved)

# The fractional remainder carries across samples, so a slow rotate at a low scale
# still moves eventually instead of every sample individually truncating to zero.
ns['ROTATE_SCALE'] = 0.3
tr, channel = rs_setup()
for _ in range(10):
    for e in motion(1):
        tr.handle(e)
total_sent = motion_dx(channel)
# The running remainder is always under 1px in magnitude, by construction — except
# floating-point slop can push a sample that should just cross a whole pixel to just
# miss it instead, so the tolerance allows exactly that much.
carried = total_sent != 0 and abs(total_sent - 10 * ns['ROTATE_SCALE']) <= 1.0
print(f"{'PASS' if carried else 'FAIL'}  a sub-1px-per-sample rotate still accumulates instead of vanishing")
if not carried:
    print(f"      sent={total_sent}, log={channel.log}")
results.append(carried)

# 1.0 is raw, unscaled motion — how this behaved before the setting existed.
ns['ROTATE_SCALE'] = 1.0
tr, channel = rs_setup()
for e in motion(7):
    tr.handle(e)
raw_at_one = motion_dx(channel) == 7
print(f"{'PASS' if raw_at_one else 'FAIL'}  rotate_scale = 1.0 leaves motion untouched")
if not raw_at_one:
    print(f"      log={channel.log}")
results.append(raw_at_one)

# Pan (bare motion, with the original mapping) must be unaffected by ROTATE_SCALE.
ns['PAN_REQUIRES_RIGHT_BUTTON'] = False    # bare motion pans
ns['ROTATE_SCALE'] = 0.2
tr, channel = rs_setup()
for e in motion(10):
    tr.handle(e)
pan_unaffected = motion_dx(channel) == 10
print(f"{'PASS' if pan_unaffected else 'FAIL'}  pan motion ignores ROTATE_SCALE")
if not pan_unaffected:
    print(f"      log={channel.log}")
results.append(pan_unaffected)

# The button-held rotate (the original mapping's rotate) is scaled too.
tr, channel = rs_setup()
tr.handle(ev(ecodes.EV_KEY, ecodes.BTN_RIGHT, 1))
tr.handle(ev(ecodes.EV_SYN, 0, 0))
channel.log.clear()
for e in motion(10):
    tr.handle(e)
button_rotate_scaled = motion_dx(channel) == 2
print(f"{'PASS' if button_rotate_scaled else 'FAIL'}  a button-held rotate is scaled too")
if not button_rotate_scaled:
    print(f"      log={channel.log}")
results.append(button_rotate_scaled)

ns['PAN_REQUIRES_RIGHT_BUTTON'] = False    # restore the default this file exercises
ns['ROTATE_SCALE'] = 1.0                   # neutral, so an unrelated future case isn't surprised

# --- the channel itself -----------------------------------------------------------
# No fallback exists any more: a missing channel must refuse to open a gesture, and
# a channel that drops mid-gesture must not leave the daemon believing one is open.

tr = Translator(StubChannel(connected=False))
ns['GATE'] = StubGate(WINDOW, WINDOW)
for e in motion(5) + motion(7):
    tr.handle(e)
refused = not tr._panning and not tr._stroke_open
print(f"{'PASS' if refused else 'FAIL'}  a disconnected channel refuses to start a gesture")
if not refused:
    print(f"      panning={tr._panning}, stroke_open={tr._stroke_open}")
results.append(refused)

tr, channel = gesture_setup()
for e in motion(5):
    tr.handle(e)
mid_stroke = tr._stroke_open
tr.channel_disconnected()
disconnected_cleanly = mid_stroke and not tr._stroke_open and not tr._panning
print(f"{'PASS' if disconnected_cleanly else 'FAIL'}  a mid-gesture disconnect ends the stroke without retrying")
if not disconnected_cleanly:
    print(f"      mid_stroke={mid_stroke}, stroke_open={tr._stroke_open}, panning={tr._panning}")
results.append(disconnected_cleanly)

# Once reconnected, a fresh gesture opens normally.
tr._channel.connected = True
for e in motion(5):
    tr.handle(e)
reconnected = tr._panning and tr._stroke_open
print(f"{'PASS' if reconnected else 'FAIL'}  a fresh gesture opens normally once reconnected")
results.append(reconnected)

# --- the channel's wire protocol ---------------------------------------------
# New, non-trivial code with real correctness risk (frame masking, the handshake's
# Origin check) that StubChannel never touches — exercised directly against a real
# connected socket pair.

import socket as _socket, hashlib as _hashlib, base64 as _base64, os as _os

_ws_send_frame = ns['_ws_send_frame']
_ws_read_frame = ns['_ws_read_frame']
ChannelClass = ns['Channel']
EXPECTED_ORIGIN = ns['EXPECTED_ORIGIN']
WS_MAGIC = ns['WS_MAGIC']

# A text frame round-trips exactly. The server's own frames are never masked —
# only what a client sends needs unmasking, covered separately below.
a, b = _socket.socketpair()
try:
    _ws_send_frame(a, 0x1, b'{"type":"press"}')
    opcode, payload = _ws_read_frame(b)
    roundtrip_ok = opcode == 0x1 and payload == b'{"type":"press"}'
finally:
    a.close(); b.close()
print(f"{'PASS' if roundtrip_ok else 'FAIL'}  a text frame round-trips through the wire helpers")
results.append(roundtrip_ok)

# A masked client frame is unmasked correctly — built by hand, the way a real
# browser's WebSocket implementation sends one.
def _client_frame(payload):
    mask = _os.urandom(4)
    masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
    return bytes([0x81, 0x80 | len(payload)]) + mask + masked

a, b = _socket.socketpair()
try:
    a.sendall(_client_frame(b"hello"))
    opcode, payload = _ws_read_frame(b)
    unmask_ok = opcode == 0x1 and payload == b"hello"
finally:
    a.close(); b.close()
print(f"{'PASS' if unmask_ok else 'FAIL'}  a masked client frame is unmasked correctly")
results.append(unmask_ok)

def _send_handshake(sock, origin, key=b"dGhlIHNhbXBsZSBub25jZQ=="):
    sock.sendall((
        "GET / HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key.decode()}\r\n"
        f"Origin: {origin}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    ).encode())
    return key

# The handshake accepts the extension's own origin and computes the right accept key.
a, b = _socket.socketpair()
try:
    channel = ChannelClass(0)
    key = _send_handshake(a, EXPECTED_ORIGIN)
    channel._handshake(b)
    response = a.recv(4096).decode()
    expected_accept = _base64.b64encode(
        _hashlib.sha1(key + WS_MAGIC.encode()).digest()).decode()
    accepted = "101" in response.splitlines()[0] and expected_accept in response
finally:
    a.close(); b.close()
print(f"{'PASS' if accepted else 'FAIL'}  the handshake accepts the extension's origin")
if not accepted:
    print(f"      response={response!r}")
results.append(accepted)

# A mismatched origin is rejected outright — an arbitrary web page must not be able
# to open this and observe the gated mouse's raw motion.
a, b = _socket.socketpair()
try:
    channel = ChannelClass(0)
    _send_handshake(a, "https://evil.example")
    rejected = False
    try:
        channel._handshake(b)
    except ValueError:
        rejected = True
    response = a.recv(4096).decode()
    rejected = rejected and response.startswith("HTTP/1.1 403")
finally:
    a.close(); b.close()
print(f"{'PASS' if rejected else 'FAIL'}  a mismatched origin is rejected")
if not rejected:
    print(f"      response={response!r}")
results.append(rejected)

print()
print(f"{sum(results)}/{len(results)} passed")
