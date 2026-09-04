"""Drive Translator with synthetic events against a stub virtual device."""
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

NAMES = {ecodes.BTN_LEFT:'LEFT', ecodes.BTN_RIGHT:'RIGHT', ecodes.BTN_MIDDLE:'MIDDLE',
         ecodes.KEY_LEFTCTRL:'CTRL', ecodes.KEY_SPACE:'SPACE'}

# The cases below are about gesture mechanics and use small synthetic motions, so
# the dead zone is off for them. It has its own section at the end.
ns['PAN_DEADZONE'] = 0

# Same for the other-mouse yield dead zone: a bare tr.yield_stroke() with no
# arguments must still read as an immediate, deliberate yield everywhere except its
# own dedicated section further down.
ns['PAN_YIELD_DEADZONE'] = 0

# And for which gesture the right button performs: the cases below are written
# against the original mapping (bare motion pans, the button rotates) and are about
# gesture mechanics, not this setting. Its own section at the end flips it.
ns['PAN_REQUIRES_RIGHT_BUTTON'] = False

class StubUI:
    def __init__(self, log=None): self.log = [] if log is None else log
    def write(self, t, c, v):
        if t == ecodes.EV_KEY:
            self.log.append(('KEY', NAMES.get(c, c), v))
        elif t == ecodes.EV_REL:
            axis = {ecodes.REL_X: 'X', ecodes.REL_Y: 'Y'}.get(c, 'WHEEL')
            self.log.append(('REL', axis, v))
        else:
            self.log.append((t, c, v))
    def write_event(self, e):
        if e.type == ecodes.EV_KEY: self.log.append(('KEY', NAMES.get(e.code, e.code), e.value))
        elif e.type == ecodes.EV_REL: self.log.append(('REL', 'X' if e.code==ecodes.REL_X else ('Y' if e.code==ecodes.REL_Y else 'WHEEL'), e.value))
        elif e.type == ecodes.EV_SYN: self.log.append(('SYN',))
    def syn(self): self.log.append(('SYN',))

def ev(t, c, v): return InputEvent(0, 0, t, c, v)
def motion(dx): return [ev(ecodes.EV_REL, ecodes.REL_X, dx), ev(ecodes.EV_SYN, 0, 0)]
def button(code, val): return [ev(ecodes.EV_KEY, code, val), ev(ecodes.EV_SYN, 0, 0)]

def run(name, events, expect_keys, post=None):
    """Compares the order of key/button events, which is where the real hazards live
    — Ctrl against the button, the middle button never appearing. Motion and SYN are
    filtered out: pinning those exactly made these cases break on every unrelated
    change. The syn-ordering hazard has its own dedicated case further down.
    """
    log = []
    ui, modifier = StubUI(log), StubUI(log)
    tr = Translator(ui, modifier)
    for e in events: tr.handle(e)
    if post: post(tr)
    got = [x for x in log if x[0] == 'KEY']
    ok = got == expect_keys
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"      expected: {expect_keys}")
        print(f"      got     : {got}")
        print(f"      full log: {log}")
    return ok

# The gesture cases below need a region the cursor is allowed to occupy: a pan
# will not start without one, because a press with nowhere known-safe to land is
# exactly what puts the right button on a toolbar. Here that region is the whole
# window; the recentring section further down uses a canvas smaller than the
# window, which is what makes it able to tell the two apart.
class StubPointer:
    def __init__(self, pos): self.ok = True; self._pos = pos; self.warps = []
    def position(self): return self._pos
    def warp(self, x, y): self.warps.append((x, y)); self._pos = (x, y)

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

# Stubbed together with the gate, and not optional. Once view_rect() returns a region,
# every motion event runs the edge check for real — and with the live POINTER that
# reads the actual mouse cursor, so a cursor sitting near a screen edge while the suite
# ran would insert a recentre's button cycle into cases that are only about gesture
# ordering. Parked in the middle, nothing is ever near an edge.
ns['POINTER'] = StubPointer(CENTRE)

results = []

# 1. Bare motion presses Ctrl then the right button, and holds them.
results.append(run("motion alone starts a pan drag",
    motion(5) + motion(7),
    [('KEY','CTRL',1), ('KEY','RIGHT',1)]))

# 2. Going idle past PAN_IDLE_RELEASE ends the stroke, button before Ctrl.
def idle_then_tick(tr):
    tr._last_motion -= 1.0
    tr.tick()
results.append(run("idle releases the pan gesture",
    motion(5),
    [('KEY','CTRL',1), ('KEY','RIGHT',1), ('KEY','RIGHT',0), ('KEY','CTRL',0)],
    post=idle_then_tick))

# 3. The physical right button hands the same drag over to rotate by dropping Ctrl.
# It must not press the button a second time.
results.append(run("right button drops Ctrl and rotates on the same drag",
    motion(5) + button(ecodes.BTN_RIGHT, 1) + motion(9),
    [('KEY','CTRL',1), ('KEY','RIGHT',1), ('KEY','CTRL',0)]))

# 4. Releasing it ends the gesture — tagged with Ctrl around the lift, since this was
# a rotate (the button-held gesture here, no Ctrl of its own) — and the next movement
# starts a fresh pan.
results.append(run("pan resumes after the right button is released",
    button(ecodes.BTN_RIGHT, 1) + motion(3) + button(ecodes.BTN_RIGHT, 0) + motion(4),
    [('KEY','RIGHT',1), ('KEY','CTRL',1), ('KEY','RIGHT',0), ('KEY','CTRL',0),
     ('KEY','CTRL',1), ('KEY','RIGHT',1)]))

# 5. The gate closing mid-stroke must strand neither the button nor the modifier.
results.append(run("gate close releases the button and Ctrl",
    motion(5),
    [('KEY','CTRL',1), ('KEY','RIGHT',1), ('KEY','RIGHT',0), ('KEY','CTRL',0)],
    post=lambda tr: tr.release_all()))

# 6. Including a button the user is physically holding — a rotate, here, so its
# release is tagged with Ctrl the same as any other.
results.append(run("gate close releases a real held button",
    button(ecodes.BTN_RIGHT, 1),
    [('KEY','RIGHT',1), ('KEY','CTRL',1), ('KEY','RIGHT',0), ('KEY','CTRL',0)],
    post=lambda tr: tr.release_all()))

# 7. Wheel is zoom: straight through, and it starts no gesture.
results.append(run("wheel passes through without panning",
    [ev(ecodes.EV_REL, ecodes.REL_WHEEL, 1), ev(ecodes.EV_SYN, 0, 0)],
    []))

# 8. The left button ends the stroke, then taps space to clear the selection. The
# click itself never reaches the page.
results.append(run("left button ends the stroke and taps space",
    motion(5) + button(ecodes.BTN_LEFT, 1),
    [('KEY','CTRL',1), ('KEY','RIGHT',1), ('KEY','RIGHT',0), ('KEY','CTRL',0),
     ('KEY','SPACE',1), ('KEY','SPACE',0)]))

# Passing the click through instead is one config value away.
saved_code = ns['LEFT_CLICK_CODE']
ns['LEFT_CLICK_CODE'] = None
results.append(run("with left_click_key = none, the click passes through",
    motion(5) + button(ecodes.BTN_LEFT, 1),
    [('KEY','CTRL',1), ('KEY','RIGHT',1), ('KEY','RIGHT',0), ('KEY','CTRL',0),
     ('KEY','LEFT',1)]))
ns['LEFT_CLICK_CODE'] = saved_code

# Nothing anywhere may ever touch the middle button: two presses inside the
# double-click window are a double middle-click, which Onshape reads as Zoom to Fit.
_probe = []
_ui, _mod = StubUI(_probe), StubUI(_probe)
_tr = Translator(_ui, _mod)
for _e in (motion(5) + button(ecodes.BTN_RIGHT, 1) + button(ecodes.BTN_RIGHT, 0)
           + motion(3) + button(ecodes.BTN_LEFT, 1)):
    _tr.handle(_e)
_tr.release_all()
_no_middle = not any(x[0] == 'KEY' and x[1] == 'MIDDLE' for x in _probe)
print(f"{'PASS' if _no_middle else 'FAIL'}  the middle button is never pressed, by any path")
if not _no_middle:
    print(f"      log={_probe}")
results.append(_no_middle)

# --- recentring ----------------------------------------------------------------
# The ordering here is the whole point: a warp landing while the pan button is
# still down would be read by Onshape as one enormous pan.


def run_recentre(name, pointer_at, expect_warps, expect_log):
    ui = StubUI(); tr = Translator(ui)
    ns['GATE'] = StubGate(WINDOW, WINDOW)
    # Start centred, so opening the pan stroke does not itself trigger a warp;
    # handle() calls _recenter_if_near_edge on every motion event.
    pointer = StubPointer(CENTRE)
    ns['POINTER'] = pointer
    for e in motion(5):
        tr.handle(e)
    ui.log.clear()
    pointer._pos = pointer_at          # now place it where the case wants it
    pointer.warps.clear()
    tr._last_edge_check = 0.0          # defeat the sampling throttle
    tr._recenter_if_near_edge()
    ok = [x for x in ui.log if x[0] == 'KEY'] == expect_log \
        and pointer.warps == expect_warps
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"      expected warps {expect_warps}, events {expect_log}")
        print(f"      got      warps {pointer.warps}, events {ui.log}")
    results.append(ok)

run_recentre("well inside the window: no warp, no events",
             (960, 540), [], [])

run_recentre("inside but past the margin: no warp",
             (200, 540), [], [])

run_recentre("near the left edge: button lifted, warp, button restored",
             (8, 540), [CENTRE],
             [('KEY','RIGHT',0), ('KEY','RIGHT',1)])

run_recentre("near the bottom edge: same handling",
             (960, 1072), [CENTRE],
             [('KEY','RIGHT',0), ('KEY','RIGHT',1)])

# Not panning still recentres — just without a button cycle, since nothing is held.
# Motion keeps flowing between strokes and through the yield cooldown, and without
# this the cursor wanders out of the view and over the feature tree.
ui = StubUI(); tr = Translator(ui)
ns['GATE'] = StubGate(WINDOW, WINDOW)
pointer = StubPointer((5, 5)); ns['POINTER'] = pointer
tr._last_edge_check = 0.0
tr._recenter_if_near_edge()
warped_bare = (pointer.warps == [CENTRE] and ui.log == [] and not tr._panning)
print(f"{'PASS' if warped_bare else 'FAIL'}  strays are recentred even with no stroke running")
if not warped_bare:
    print(f"      warps={pointer.warps}, events={ui.log}")
results.append(warped_bare)

# Inside the view with no stroke running: leave it alone.
ui = StubUI(); tr = Translator(ui)
ns['GATE'] = StubGate(WINDOW, WINDOW)
pointer = StubPointer(CENTRE); ns['POINTER'] = pointer
tr._last_edge_check = 0.0
tr._recenter_if_near_edge()
left_alone = pointer.warps == [] and ui.log == []
print(f"{'PASS' if left_alone else 'FAIL'}  no stroke and inside the view: left alone")
results.append(left_alone)

# A recentre stalls the read loop, so it must refresh the idle deadline or the
# timer tears down the stroke it just restored.
ui = StubUI(); tr = Translator(ui)
ns['GATE'] = StubGate(WINDOW, WINDOW)
pointer = StubPointer(CENTRE); ns['POINTER'] = pointer
for e in motion(5):
    tr.handle(e)
tr._last_motion = time.monotonic() - 1.0     # deadline already blown
pointer._pos = (8, 540)
tr._last_edge_check = 0.0
tr._recenter_if_near_edge()
ui.log.clear()
tr.tick()                                     # must NOT end the stroke
survived = tr._panning and ui.log == []
print(f"{'PASS' if survived else 'FAIL'}  recentre refreshes the idle deadline")
if not survived:
    print(f"      panning={tr._panning}, events after tick={ui.log}")
results.append(survived)

# Another mouse stirring must drop the stroke, so its wheel reaches the page as a
# clean scroll rather than wheel-with-button-held.
ui = StubUI(); tr = Translator(ui)
ns['GATE'] = StubGate(WINDOW, WINDOW)
ns['POINTER'] = StubPointer(CENTRE)
for e in motion(5):
    tr.handle(e)
ui.log.clear()
tr.yield_stroke()
yielded = (not tr._panning
           and [x for x in ui.log if x[0] == 'KEY'] == [('KEY','RIGHT',0)]
           and tr.yields == 1)
print(f"{'PASS' if yielded else 'FAIL'}  other mouse activity releases the pan button")
if not yielded:
    print(f"      panning={tr._panning}, yields={tr.yields}, events={ui.log}")
results.append(yielded)

# Yielding when nothing is happening must not emit a spurious release.
ui = StubUI(); tr = Translator(ui)
tr.yield_stroke()
quiet = ui.log == [] and tr.yields == 0
print(f"{'PASS' if quiet else 'FAIL'}  yielding while idle emits nothing")
results.append(quiet)

# Straight after a yield the cooldown holds panning off, so a wheel-zoom burst on
# the other mouse is not fought over press-by-press.
ui = StubUI(); tr = Translator(ui)
ns['GATE'] = StubGate(WINDOW, WINDOW)
ns['POINTER'] = StubPointer(CENTRE)
for e in motion(5):
    tr.handle(e)
tr.yield_stroke()
ui.log.clear()
for e in motion(4):
    tr.handle(e)
held_off = (not tr._panning) and ('KEY','RIGHT',1) not in ui.log
print(f"{'PASS' if held_off else 'FAIL'}  cooldown holds panning off right after a yield")
if not held_off:
    print(f"      panning={tr._panning}, events={ui.log}")
results.append(held_off)

# Once both the cooldown and the minimum press interval lapse, the next movement
# starts a fresh stroke.
tr._yield_until = time.monotonic() - 0.001
ui.log.clear()
for e in motion(4):
    tr.handle(e)
resumed = tr._panning and ('KEY','RIGHT',1) in ui.log
print(f"{'PASS' if resumed else 'FAIL'}  panning resumes once the cooldown lapses")
if not resumed:
    print(f"      panning={tr._panning}, events={ui.log}")
results.append(resumed)

# --- a press must never land outside the window --------------------------------
# Pressing outside the browser clicks another window, which takes focus away and
# closes the gate. Motion flows while a press is deferred, so the cursor really can
# be outside by press time.

OUTSIDE = (WINDOW[0] + WINDOW[2] + 200, WINDOW[1] + 100)   # right of the window

ui = StubUI(); tr = Translator(ui)
ns['GATE'] = StubGate(WINDOW, WINDOW)
pointer = StubPointer(OUTSIDE); ns['POINTER'] = pointer
tr._press_pan()
x, y = pointer._pos
pulled_in = (WINDOW[0] <= x <= WINDOW[0] + WINDOW[2]
             and WINDOW[1] <= y <= WINDOW[1] + WINDOW[3]
             and tr.presses_recentred == 1)
print(f"{'PASS' if pulled_in else 'FAIL'}  a press outside the window is pulled back inside")
if not pulled_in:
    print(f"      pressed at {pointer._pos} for window {WINDOW}")
results.append(pulled_in)

# A press already inside must not be disturbed.
ui = StubUI(); tr = Translator(ui)
ns['GATE'] = StubGate(WINDOW, WINDOW)
pointer = StubPointer(CENTRE); ns['POINTER'] = pointer
tr._press_pan()
undisturbed = pointer._pos == CENTRE and tr.presses_recentred == 0 and pointer.warps == []
print(f"{'PASS' if undisturbed else 'FAIL'}  a press already inside is left where it is")
if not undisturbed:
    print(f"      pos={pointer._pos}, warps={pointer.warps}")
results.append(undisturbed)

# --- Ctrl + right-drag gesture -------------------------------------------------
# Nothing here may ever touch the middle button: that is the entire point of the
# gesture, since Onshape maps double middle-click to Zoom to Fit.


class SharedUI(StubUI):
    """Logs into a shared list so ordering across the two virtual devices is visible."""
    def __init__(self, log): self.log = log

def ctrl_setup(pos=CENTRE):
    log = []
    ui, mod = SharedUI(log), SharedUI(log)
    tr = Translator(ui, mod)
    ns['GATE'] = StubGate(WINDOW, WINDOW)
    ns['POINTER'] = StubPointer(pos)
    return tr, log

def expect(name, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"      expected {want}")
        print(f"      got      {got}")
    results.append(ok)

# Ctrl must land before the button; the other order is a momentary plain right-drag,
# which Onshape rotates on.
tr, log = ctrl_setup()
for e in motion(5):
    tr.handle(e)
expect("ctrl_right: Ctrl is pressed before the right button",
       [x for x in log if x[0] == 'KEY'],
       [('KEY','CTRL',1), ('KEY','RIGHT',1)])

no_middle = not any(x[0] == 'KEY' and x[1] == 'MIDDLE' for x in log)
print(f"{'PASS' if no_middle else 'FAIL'}  ctrl_right: the middle button is never touched")
results.append(no_middle)

# And on release the button must lift before Ctrl, for the same reason.
log.clear()
tr._end_pan(syn=True)
expect("ctrl_right: the right button lifts before Ctrl",
       [x for x in log if x[0] == 'KEY'],
       [('KEY','RIGHT',0), ('KEY','CTRL',0)])

# Writing them in the right order is not enough: the button release must be FLUSHED
# before Ctrl goes, or X sees Ctrl lift first and the still-held button becomes a
# plain right-drag, which Onshape rotates on.
right_up = log.index(('KEY','RIGHT',0))
ctrl_up = log.index(('KEY','CTRL',0))
flushed = any(log[i] == ('SYN',) for i in range(right_up + 1, ctrl_up))
print(f"{'PASS' if flushed else 'FAIL'}  ctrl_right: the button release is flushed before Ctrl")
if not flushed:
    print(f"      log={log}")
results.append(flushed)

# Same on the way down: Ctrl must be flushed before the button press.
tr2, log2 = ctrl_setup()
for e in motion(5):
    tr2.handle(e)
ctrl_down = log2.index(('KEY','CTRL',1))
right_down = log2.index(('KEY','RIGHT',1))
down_ok = ctrl_down < right_down
print(f"{'PASS' if down_ok else 'FAIL'}  ctrl_right: Ctrl lands before the button press")
if not down_ok:
    print(f"      log={log2}")
results.append(down_ok)

# Pressing the physical right button mid-pan hands the same drag to rotate.
tr, log = ctrl_setup()
for e in motion(5):
    tr.handle(e)
log.clear()
tr.handle(ev(ecodes.EV_KEY, ecodes.BTN_RIGHT, 1))
handover = ([x for x in log if x[0] == 'KEY'] == [('KEY','CTRL',0)]
            and not tr._panning and tr._right_emitted)
print(f"{'PASS' if handover else 'FAIL'}  ctrl_right: physical right drops Ctrl without re-pressing")
if not handover:
    print(f"      panning={tr._panning}, right_emitted={tr._right_emitted}, log={log}")
results.append(handover)

# Releasing it ends the gesture exactly once. Ctrl was dropped for the hand-off
# above, so this release tags it back on around the button lift — the same signal
# pan's own release already gets for free — rather than manufacturing motion.
log.clear()
tr.handle(ev(ecodes.EV_KEY, ecodes.BTN_RIGHT, 0))
released = ([x for x in log if x[0] == 'KEY']
            == [('KEY','CTRL',1), ('KEY','RIGHT',0), ('KEY','CTRL',0)]
            and not tr._right_emitted and not tr._ctrl_down)
print(f"{'PASS' if released else 'FAIL'}  ctrl_right: releasing right ends the gesture once")
if not released:
    print(f"      right_emitted={tr._right_emitted}, log={log}")
results.append(released)

# Ctrl + wheel is browser page zoom, so the modifier must be gone first.
tr, log = ctrl_setup()
for e in motion(5):
    tr.handle(e)
log.clear()
tr.handle(ev(ecodes.EV_REL, ecodes.REL_WHEEL, 1))
keys = [x for x in log if x[0] == 'KEY']
wheel_at = log.index(('REL','WHEEL',1)) if ('REL','WHEEL',1) in log else -1
ctrl_up_at = log.index(('KEY','CTRL',0)) if ('KEY','CTRL',0) in log else -1
wheel_safe = (not tr._ctrl_down and ctrl_up_at >= 0 and wheel_at > ctrl_up_at)
print(f"{'PASS' if wheel_safe else 'FAIL'}  ctrl_right: Ctrl is released before a wheel event")
if not wheel_safe:
    print(f"      ctrl_held={tr._ctrl_down}, log={log}")
results.append(wheel_safe)

# Gate closing mid-pan must strand neither the button nor the modifier.
tr, log = ctrl_setup()
for e in motion(5):
    tr.handle(e)
log.clear()
tr.release_all()
clean = not tr._ctrl_down and not tr._right_emitted and not tr._panning
print(f"{'PASS' if clean else 'FAIL'}  ctrl_right: gate close releases both button and Ctrl")
if not clean:
    print(f"      ctrl_held={tr._ctrl_down}, right_emitted={tr._right_emitted}, log={log}")
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

fresh = lambda: {"at": time.monotonic(), "at_pos": (900, 500), "in_view_rect": True}

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

# The point of all this: a cursor comfortably inside the window but at the canvas
# edge must still be recentred, into the canvas rather than the window.
ui = StubUI(); tr = Translator(ui)
ns['GATE'] = StubGate(WINDOW_RECT, CANVAS_RECT)
pointer = StubPointer((CANVAS_RECT[0] + CANVAS_RECT[2] // 2, CANVAS_RECT[1] + CANVAS_RECT[3] // 2))
ns['POINTER'] = pointer
for e in motion(5):
    tr.handle(e)
pointer._pos = (CANVAS_RECT[0] + 5, CANVAS_RECT[1] + 300)   # just inside the canvas edge
pointer.warps.clear()
tr._last_edge_check = 0.0
tr._recenter_if_near_edge()
canvas_centre = (CANVAS_RECT[0] + CANVAS_RECT[2] // 2, CANVAS_RECT[1] + CANVAS_RECT[3] // 2)
to_canvas = pointer.warps == [canvas_centre]
print(f"{'PASS' if to_canvas else 'FAIL'}  recentres to the canvas centre, not the window centre")
if not to_canvas:
    print(f"      warps={pointer.warps}, expected [{canvas_centre}]")
results.append(to_canvas)

# The edge margin has to scale with pan speed. The check is throttled, so the ground
# a flick covers between two checks is unbounded by any fixed margin — which is how
# a quick pan used to sail past 20px and land on the feature tree.
#
# Both cases below put the cursor at the same spot, 60px inside the canvas edge:
# outside the static margin, inside the reach of the flick. Only the travel leading
# up to them differs, so the travel is the only thing that can change the verdict.
NEAR_EDGE = (CANVAS_RECT[0] + 60, CANVAS_RECT[1] + 300)

def recentre_after(travel):
    ui = StubUI(); tr = Translator(ui)
    ns['GATE'] = StubGate(WINDOW_RECT, CANVAS_RECT)
    pointer = StubPointer((CANVAS_RECT[0] + CANVAS_RECT[2] // 2,
                           CANVAS_RECT[1] + CANVAS_RECT[3] // 2))
    ns['POINTER'] = pointer
    for e in motion(5):
        tr.handle(e)
    # Hold the throttle closed so the motion below only accumulates travel, exactly
    # as it does between two real checks.
    tr._last_edge_check = time.monotonic()
    for e in motion(travel):
        tr.handle(e)
    pointer._pos = NEAR_EDGE
    pointer.warps.clear()
    tr._last_edge_check = 0.0
    tr._recenter_if_near_edge()
    return pointer.warps

crept = recentre_after(4)
stays = crept == []
print(f"{'PASS' if stays else 'FAIL'}  a slow pan near the edge is left alone")
if not stays:
    print(f"      warps={crept}, expected none")
results.append(stays)

flicked = recentre_after(200)
rescued = flicked == [canvas_centre]
print(f"{'PASS' if rescued else 'FAIL'}  a fast flick recentres before it clears the edge")
if not rescued:
    print(f"      warps={flicked}, expected [{canvas_centre}]")
results.append(rescued)

# A recentre must not drop Ctrl. Releasing and re-pressing it mid-stroke opened a
# window where X saw the button still down with Ctrl gone — a plain right-drag,
# which Onshape rotates on. Measured 26 such episodes in 25s of panning.
tr, log = ctrl_setup()
for e in motion(5):
    tr.handle(e)
ns['POINTER']._pos = (8, 540)
tr._last_edge_check = 0.0
log.clear()
tr._recenter_if_near_edge()
ctrl_events = [x for x in log if x[0] == 'KEY' and x[1] == 'CTRL']
held = tr._ctrl_down and ctrl_events == []
print(f"{'PASS' if held else 'FAIL'}  ctrl_right: a recentre keeps Ctrl held throughout")
if not held:
    print(f"      ctrl_down={tr._ctrl_down}, ctrl events={ctrl_events}")
results.append(held)

# The button, on the other hand, must still be lifted and re-pressed around the warp.
btn = [x for x in log if x[0] == 'KEY' and x[1] == 'RIGHT']
cycled = btn == [('KEY','RIGHT',0), ('KEY','RIGHT',1)]
print(f"{'PASS' if cycled else 'FAIL'}  ctrl_right: a recentre still cycles the button")
if not cycled:
    print(f"      button events={btn}")
results.append(cycled)

# The button-held gesture never sets _panning, so it needs its own coverage: this is
# exactly what pan_requires_right_button = true depends on for the sweep not to run
# out of screen while the button is held.
ns['PAN_REQUIRES_RIGHT_BUTTON'] = True
tr, log = ctrl_setup()
for e in button(ecodes.BTN_RIGHT, 1) + motion(5):
    tr.handle(e)
ns['POINTER']._pos = (8, 540)
tr._last_edge_check = 0.0
log.clear()
tr._recenter_if_near_edge()
btn = [x for x in log if x[0] == 'KEY' and x[1] == 'RIGHT']
ctrl_events = [x for x in log if x[0] == 'KEY' and x[1] == 'CTRL']
button_pan_recentres = (btn == [('KEY','RIGHT',0), ('KEY','RIGHT',1)]
                         and tr._ctrl_down and ctrl_events == [])
print(f"{'PASS' if button_pan_recentres else 'FAIL'}  "
      f"a button-held pan also recentres, Ctrl held throughout")
if not button_pan_recentres:
    print(f"      button events={btn}, ctrl_down={tr._ctrl_down}, ctrl events={ctrl_events}")
results.append(button_pan_recentres)

# Same for a button-held rotate (the original mapping): the button still cycles, and
# the lift is tagged with Ctrl for its own instant, same as any other rotate release —
# it lands back down before the button re-presses, so the drag Onshape sees is
# unaffected, and Ctrl is not held once the recentre finishes.
ns['PAN_REQUIRES_RIGHT_BUTTON'] = False
tr, log = ctrl_setup()
for e in button(ecodes.BTN_RIGHT, 1) + motion(5):
    tr.handle(e)
ns['POINTER']._pos = (8, 540)
tr._last_edge_check = 0.0
log.clear()
tr._recenter_if_near_edge()
btn = [x for x in log if x[0] == 'KEY' and x[1] == 'RIGHT']
ctrl_events = [x for x in log if x[0] == 'KEY' and x[1] == 'CTRL']
button_rotate_recentres = (btn == [('KEY','RIGHT',0), ('KEY','RIGHT',1)]
                            and not tr._ctrl_down
                            and ctrl_events == [('KEY','CTRL',1), ('KEY','CTRL',0)])
print(f"{'PASS' if button_rotate_recentres else 'FAIL'}  "
      f"a button-held rotate also recentres, tagging the lift with Ctrl and dropping it")
if not button_rotate_recentres:
    print(f"      button events={btn}, ctrl_down={tr._ctrl_down}, ctrl events={ctrl_events}")
results.append(button_rotate_recentres)

# --- left click clears the selection ---------------------------------------------
# The cursor is penned in the middle of the view, so a real left click would just
# select whatever geometry is under it. Onshape clears the whole selection on space.

tr, log = ctrl_setup()
for e in motion(5):
    tr.handle(e)
log.clear()
tr.handle(ev(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
keys = [x for x in log if x[0] == 'KEY']
tapped = ('KEY','SPACE',1) in keys and ('KEY','SPACE',0) in keys and tr.left_taps == 1
print(f"{'PASS' if tapped else 'FAIL'}  a left click taps space")
if not tapped:
    print(f"      log={log}")
results.append(tapped)

swallowed = not any(x[0] == 'KEY' and x[1] == 'LEFT' for x in log)
print(f"{'PASS' if swallowed else 'FAIL'}  the left click itself is swallowed")
if not swallowed:
    print(f"      log={log}")
results.append(swallowed)

# Ctrl must be gone before the tap, or it is Ctrl+space rather than space.
ctrl_up = next((i for i, x in enumerate(log) if x == ('KEY','CTRL',0)), None)
space_down = next((i for i, x in enumerate(log) if x == ('KEY','SPACE',1)), None)
ordered = ctrl_up is not None and space_down is not None and ctrl_up < space_down
print(f"{'PASS' if ordered else 'FAIL'}  the pan is released before the tap")
if not ordered:
    print(f"      log={log}")
results.append(ordered)

# The release is swallowed too, so it cannot tap twice.
log.clear()
tr.handle(ev(ecodes.EV_KEY, ecodes.BTN_LEFT, 0))
once = log == [] and tr.left_taps == 1
print(f"{'PASS' if once else 'FAIL'}  the button release taps nothing further")
if not once:
    print(f"      log={log}, taps={tr.left_taps}")
results.append(once)

# left_click_key = none restores an ordinary click.
saved = ns['LEFT_CLICK_CODE']
ns['LEFT_CLICK_CODE'] = None
tr2, log2 = ctrl_setup()
tr2.handle(ev(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
passthrough = ('KEY','LEFT',1) in log2 and tr2.left_taps == 0
print(f"{'PASS' if passthrough else 'FAIL'}  with the key disabled, the click passes through")
if not passthrough:
    print(f"      log={log2}")
results.append(passthrough)
ns['LEFT_CLICK_CODE'] = saved

# --- the stale _right_down hole --------------------------------------------------
# If that flag went stale, ending a pan skipped the button release but still dropped
# Ctrl: button held, no Ctrl, which is a plain right-drag and rotates until something
# else clears it.

tr, log = ctrl_setup()
for e in motion(5):
    tr.handle(e)
tr._right_down = True              # pretend it went stale
log.clear()
tr._end_pan(syn=True)
keys = [x for x in log if x[0] == 'KEY']
released = ('KEY','RIGHT',0) in keys and not tr._right_emitted
print(f"{'PASS' if released else 'FAIL'}  a stale right_down cannot strand the button held")
if not released:
    print(f"      right_emitted={tr._right_emitted}, keys={keys}")
results.append(released)

if ('KEY','CTRL',0) in keys and ('KEY','RIGHT',0) in keys:
    order_ok = keys.index(('KEY','RIGHT',0)) < keys.index(('KEY','CTRL',0))
else:
    order_ok = False
print(f"{'PASS' if order_ok else 'FAIL'}  and the button still goes before Ctrl")
results.append(order_ok)

# Closing the gate must clear the flag even on the idle path, which used to return
# early and leave it set.
tr2, log2 = ctrl_setup()
tr2._right_down = True             # idle: nothing panning, nothing held
tr2.release_all()
cleared = tr2._right_down is False
print(f"{'PASS' if cleared else 'FAIL'}  closing the gate clears right_down even when idle")
results.append(cleared)

# --- the right mouse must cancel a rotate, not just a pan ------------------------
# After the hand-off, _panning is False while the right button stays held. Keying the
# yield off _panning meant a rotate ran on until the physical button came back up.

tr, log = ctrl_setup()
for e in motion(5):
    tr.handle(e)
tr.handle(ev(ecodes.EV_KEY, ecodes.BTN_RIGHT, 1))    # hand off to rotate
handed_off = (not tr._panning) and tr._right_emitted and not tr._ctrl_down
print(f"{'PASS' if handed_off else 'FAIL'}  setup: the hand-off holds the button with _panning False")
results.append(handed_off)

log.clear()
tr.yield_stroke()
cancelled = (not tr._right_emitted
             and ('KEY','RIGHT',0) in [x for x in log if x[0] == 'KEY']
             and tr.yields == 1)
print(f"{'PASS' if cancelled else 'FAIL'}  the right mouse cancels a rotate")
if not cancelled:
    print(f"      right_emitted={tr._right_emitted}, yields={tr.yields}, log={log}")
results.append(cancelled)

# Cancelling a plain pan still works, and still lifts the button before Ctrl.
tr, log = ctrl_setup()
for e in motion(5):
    tr.handle(e)
log.clear()
tr.yield_stroke()
keys = [x for x in log if x[0] == 'KEY']
pan_cancelled = (not tr._panning and not tr._right_emitted and not tr._ctrl_down
                 and ('KEY','RIGHT',0) in keys and ('KEY','CTRL',0) in keys
                 and keys.index(('KEY','RIGHT',0)) < keys.index(('KEY','CTRL',0)))
print(f"{'PASS' if pan_cancelled else 'FAIL'}  the right mouse still cancels a pan, button before Ctrl")
if not pan_cancelled:
    print(f"      keys={keys}")
results.append(pan_cancelled)

# Nothing held: stay silent rather than emitting stray releases.
tr, log = ctrl_setup()
tr.yield_stroke()
quiet = log == [] and tr.yields == 0
print(f"{'PASS' if quiet else 'FAIL'}  yielding with nothing held emits nothing")
if not quiet:
    print(f"      log={log}")
results.append(quiet)

# --- pan dead zone ---------------------------------------------------------------
# A pan should need a deliberate push, not a nudge. Measured as net displacement, so
# jitter that wanders out and back never trips it.

ns['PAN_DEADZONE'] = 10

def dz_setup():
    log = []
    ui, mod = StubUI(log), StubUI(log)
    tr = Translator(ui, mod)
    ns['GATE'] = StubGate(WINDOW, WINDOW)
    ns['POINTER'] = StubPointer(CENTRE)
    return tr, log

def pressed(log):
    return ('KEY','RIGHT',1) in [x for x in log if x[0] == 'KEY']

# Under the threshold: motion flows, but no pan.
tr, log = dz_setup()
for e in motion(4) + motion(4):          # 8px total, still inside
    tr.handle(e)
inside = not tr._panning and not pressed(log) and ('REL','X',4) in log
print(f"{'PASS' if inside else 'FAIL'}  under the dead zone: motion passes, no pan starts")
if not inside:
    print(f"      panning={tr._panning}, log={log}")
results.append(inside)

# Crossing it starts the pan.
for e in motion(4):                      # 12px total
    tr.handle(e)
crossed = tr._panning and pressed(log)
print(f"{'PASS' if crossed else 'FAIL'}  crossing the dead zone starts the pan")
if not crossed:
    print(f"      panning={tr._panning}, log={log}")
results.append(crossed)

# Jitter out and back nets zero, so it must never trip.
tr, log = dz_setup()
for _ in range(6):
    for e in motion(8) + motion(-8):
        tr.handle(e)
jitter = not tr._panning and not pressed(log)
print(f"{'PASS' if jitter else 'FAIL'}  jitter that returns to origin never starts a pan")
if not jitter:
    print(f"      panning={tr._panning}, travel=({tr._travel_x},{tr._travel_y})")
results.append(jitter)

# Diagonal counts as distance, not per-axis.
tr, log = dz_setup()
for e in ([ev(ecodes.EV_REL, ecodes.REL_X, 8), ev(ecodes.EV_REL, ecodes.REL_Y, 8),
           ev(ecodes.EV_SYN, 0, 0)]):
    tr.handle(e)
diagonal = tr._panning and pressed(log)
print(f"{'PASS' if diagonal else 'FAIL'}  a diagonal push crosses on distance, not per axis")
if not diagonal:
    print(f"      panning={tr._panning}, travel=({tr._travel_x},{tr._travel_y})")
results.append(diagonal)

# Each stroke earns its own dead zone.
tr, log = dz_setup()
for e in motion(12):
    tr.handle(e)
tr._end_pan(syn=True)
log.clear()
for e in motion(4):
    tr.handle(e)
re_armed = not tr._panning and not pressed(log)
print(f"{'PASS' if re_armed else 'FAIL'}  the dead zone re-arms after a stroke ends")
if not re_armed:
    print(f"      panning={tr._panning}, log={log}")
results.append(re_armed)

# A stale nudge expires rather than combining with a later one.
tr, log = dz_setup()
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
tr, log = dz_setup()
for e in motion(1):
    tr.handle(e)
disabled = tr._panning and pressed(log)
print(f"{'PASS' if disabled else 'FAIL'}  pan_deadzone_px = 0 starts panning immediately")
results.append(disabled)

# --- other-mouse yield dead zone --------------------------------------------------
# The other mouse stirring should not tear down a pan over a bump or a resting hand
# — only a deliberate push, measured the same way as pan_deadzone_px, or a button /
# wheel signal, which is always immediate.

ns['PAN_YIELD_DEADZONE'] = 10

def yz_setup():
    """A translator already mid-pan, so yield_stroke has something to drop."""
    tr, log = dz_setup()
    for e in motion(4):
        tr.handle(e)
    log.clear()
    return tr, log

def released(log):
    return ('KEY','RIGHT',0) in [x for x in log if x[0] == 'KEY']

# Under the threshold: the pan survives.
tr, log = yz_setup()
tr.yield_stroke(dx=4)
tr.yield_stroke(dx=4)                    # 8px net, still inside
survives = tr._panning and not released(log) and tr.yields == 0
print(f"{'PASS' if survives else 'FAIL'}  under the yield dead zone: the pan survives")
if not survives:
    print(f"      panning={tr._panning}, yields={tr.yields}, log={log}")
results.append(survives)

# Crossing it yields.
tr.yield_stroke(dx=4)                    # 12px net
crosses = not tr._panning and released(log) and tr.yields == 1
print(f"{'PASS' if crosses else 'FAIL'}  crossing the yield dead zone drops the pan")
if not crosses:
    print(f"      panning={tr._panning}, yields={tr.yields}, log={log}")
results.append(crosses)

# Diagonal counts as distance, not per-axis.
tr, log = yz_setup()
tr.yield_stroke(dx=8, dy=8)
diagonal = not tr._panning and tr.yields == 1
print(f"{'PASS' if diagonal else 'FAIL'}  a diagonal nudge crosses on distance, not per axis")
if not diagonal:
    print(f"      panning={tr._panning}, travel=({tr._other_travel_x},{tr._other_travel_y})")
results.append(diagonal)

# A button on the other mouse yields immediately regardless of magnitude.
tr, log = yz_setup()
tr.yield_stroke(dx=1, dy=0, immediate=True)
immediate_ok = not tr._panning and released(log) and tr.yields == 1
print(f"{'PASS' if immediate_ok else 'FAIL'}  a button/wheel signal yields immediately")
if not immediate_ok:
    print(f"      panning={tr._panning}, yields={tr.yields}, log={log}")
results.append(immediate_ok)

# A stale nudge expires rather than combining with a later one.
tr, log = yz_setup()
tr.yield_stroke(dx=8)                    # under 10, not yet yielded
tr._other_travel_at = time.monotonic() - (ns['PAN_IDLE_RELEASE'] + 0.01)
tr.yield_stroke(dx=4)                    # would total 12 if it banked, only 4 fresh
stale_expired = tr._panning and not released(log) and tr.yields == 0
print(f"{'PASS' if stale_expired else 'FAIL'}  a stale nudge on the other mouse expires instead of accumulating")
if not stale_expired:
    print(f"      panning={tr._panning}, yields={tr.yields}, travel=({tr._other_travel_x},{tr._other_travel_y})")
results.append(stale_expired)

# Symmetric with _start_pan: when bare motion drives rotate (the default mapping)
# rather than pan, it gets no dead zone on the way out either — the other mouse's
# very first move ends it, however small, and however recently the gated mouse
# itself was moving. A trackball's ball can keep rolling a little after the hand
# lifts, so nothing here may depend on the gated mouse having gone idle first.
saved_prb = ns['PAN_REQUIRES_RIGHT_BUTTON']
ns['PAN_REQUIRES_RIGHT_BUTTON'] = True
tr, log = yz_setup()
tr.yield_stroke(dx=1)                    # far under 10px
ns['PAN_REQUIRES_RIGHT_BUTTON'] = saved_prb
rotate_has_no_yield_deadzone = not tr._panning and released(log) and tr.yields == 1
print(f"{'PASS' if rotate_has_no_yield_deadzone else 'FAIL'}  bare-motion rotate has no yield dead zone, even fresh")
if not rotate_has_no_yield_deadzone:
    print(f"      panning={tr._panning}, yields={tr.yields}, log={log}")
results.append(rotate_has_no_yield_deadzone)

# But a button-held gesture keeps the dead zone regardless: a pause mid-hold is
# normal, so it still needs protecting from a bump.
tr, log = ctrl_setup()
for e in motion(5):
    tr.handle(e)
tr.handle(ev(ecodes.EV_KEY, ecodes.BTN_RIGHT, 1))    # hand off to the button
log.clear()
tr.yield_stroke(dx=1)                    # far under 10px, button-held gesture stays
handoff_still_protected = tr._right_emitted and tr.yields == 0
print(f"{'PASS' if handoff_still_protected else 'FAIL'}  a button-held gesture keeps its dead zone")
if not handoff_still_protected:
    print(f"      right_emitted={tr._right_emitted}, yields={tr.yields}, log={log}")
results.append(handoff_still_protected)

# Zero disables it entirely: the first movement yields.
ns['PAN_YIELD_DEADZONE'] = 0
tr, log = yz_setup()
tr.yield_stroke(dx=1)
zero_disabled = not tr._panning and released(log) and tr.yields == 1
print(f"{'PASS' if zero_disabled else 'FAIL'}  pan_yield_deadzone_px = 0 yields on the first movement")
results.append(zero_disabled)
ns['PAN_YIELD_DEADZONE'] = 0             # restored for the sections below

# --- nothing under the pointer but canvas ----------------------------------------
# A pan holds the right button down and drags it. Whatever sits under the pointer
# receives that press and that release, so the button may only ever go down inside a
# region the extension has verified is 3D view and nothing else. No region, no pan:
# the alternative is a right-button release on a toolbar, which opens a context menu
# or activates whatever it lands on.

ns['PAN_DEADZONE'] = 0

# No safe region at all: motion still reaches the page, but no button is pressed.
log = []
ui, modifier = StubUI(log), StubUI(log)
tr = Translator(ui, modifier)
ns['GATE'] = StubGate(WINDOW, None)
ns['POINTER'] = StubPointer(CENTRE)
for e in motion(5) + motion(7):
    tr.handle(e)
no_press = not tr._panning and not any(x[0] == 'KEY' for x in log)
print(f"{'PASS' if no_press else 'FAIL'}  no verified view region: motion passes, no button is pressed")
if not no_press:
    print(f"      panning={tr._panning} log={[x for x in log if x[0] == 'KEY']}")
results.append(no_press)

# And it is not a permanent refusal: the region coming back re-enables panning.
ns['GATE'] = StubGate(WINDOW, WINDOW)
for e in motion(5):
    tr.handle(e)
recovers = tr._panning and any(x[0] == 'KEY' and x[1] == 'RIGHT' and x[2] == 1 for x in log)
print(f"{'PASS' if recovers else 'FAIL'}  panning resumes once a view region is reported again")
results.append(recovers)

# The region vanishing mid-stroke — a dialog opening over the view, or the extension
# going quiet — must let go of the button rather than keep dragging it across whatever
# just appeared.
log = []
ui, modifier = StubUI(log), StubUI(log)
tr = Translator(ui, modifier)
ns['GATE'] = StubGate(WINDOW, WINDOW)
pointer = StubPointer(CENTRE)
ns['POINTER'] = pointer
for e in motion(5):
    tr.handle(e)
started = tr._panning
log.clear()
ns['GATE'] = StubGate(WINDOW, None)      # the safe region disappears
tr._last_edge_check = 0.0
tr._recenter_if_near_edge()
let_go = started and not tr._panning and ('KEY', 'RIGHT', 0) in log
print(f"{'PASS' if let_go else 'FAIL'}  the view region vanishing mid-stroke releases the button")
if not let_go:
    print(f"      started={started} panning={tr._panning} log={[x for x in log if x[0] == 'KEY']}")
results.append(let_go)

# --- pan_requires_right_button: swapping which gesture the button performs -------
# Everything above ran with the original mapping (bare motion pans, the button
# rotates). This flips it: the button should now bracket pan, and bare motion should
# rotate — with no Ctrl anywhere in the rotate cases. Idle release, recentring and
# hand-off are exactly what the cases above already covered, so most of these only
# check which gesture gets Ctrl. The dead zone is the one thing that does NOT carry
# over unchanged — it applies only to whichever gesture bare motion drives when that
# is pan, so flipping the mapping flips whether it applies at all, and that gets its
# own dedicated case below.

ns['PAN_REQUIRES_RIGHT_BUTTON'] = True
ns['PAN_DEADZONE'] = 0
ns['GATE'] = StubGate(WINDOW, WINDOW)
ns['POINTER'] = StubPointer(CENTRE)

results.append(run("bare motion rotates instead of panning",
    motion(5) + motion(7),
    [('KEY','RIGHT',1)]))

# Rotate gets no dead zone at all under this mapping — see the PAN_DEADZONE comment.
# A single, tiny sample must press the button immediately, not wait for 10px to
# accumulate; otherwise a small deliberate rotate loses exactly the distance Onshape's
# own click-vs-drag check needs to see it as a drag rather than a click.
ns['PAN_DEADZONE'] = 10
results.append(run("rotate ignores the dead zone entirely",
    motion(1),
    [('KEY','RIGHT',1)]))
ns['PAN_DEADZONE'] = 0

results.append(run("holding the right button pans instead of rotating",
    button(ecodes.BTN_RIGHT, 1) + motion(9),
    [('KEY','CTRL',1), ('KEY','RIGHT',1)]))

results.append(run("releasing the button after a button-held pan drops Ctrl, "
                    "button before Ctrl",
    button(ecodes.BTN_RIGHT, 1) + motion(5) + button(ecodes.BTN_RIGHT, 0),
    [('KEY','CTRL',1), ('KEY','RIGHT',1), ('KEY','RIGHT',0), ('KEY','CTRL',0)]))

# Idle release still ends a bare-motion rotate. No Ctrl was ever raised for the drag
# itself, but the release tags the button-up with one — pan's own release gets this
# for free, and this is how rotate's gets the same signal without manufacturing any
# motion for the page to measure.
results.append(run("idle release ends a bare-motion rotate, tagging the release with Ctrl",
    motion(5),
    [('KEY','RIGHT',1), ('KEY','CTRL',1), ('KEY','RIGHT',0), ('KEY','CTRL',0)],
    post=idle_then_tick))

# The hand-off now runs the other way: a rotate already under way (bare motion, no
# Ctrl) must not be double-pressed when the button comes down — it adds Ctrl instead
# of dropping it, turning the same drag into a pan.
log = []
ui, modifier = StubUI(log), StubUI(log)
tr = Translator(ui, modifier)
for e in motion(5) + button(ecodes.BTN_RIGHT, 1) + motion(9):
    tr.handle(e)
keys = [x for x in log if x[0] == 'KEY']
handed_off_to_pan = (keys == [('KEY','RIGHT',1), ('KEY','CTRL',1)]
                      and not tr._panning and tr._right_emitted and tr._ctrl_down)
print(f"{'PASS' if handed_off_to_pan else 'FAIL'}  the button hands a rotate off to pan without a double press")
if not handed_off_to_pan:
    print(f"      keys={keys}, panning={tr._panning}, "
          f"right_emitted={tr._right_emitted}, ctrl_down={tr._ctrl_down}")
results.append(handed_off_to_pan)

# The button-held gesture (pan, under this mapping) must resume once the physical
# button never let go — a right-mouse yield mid-pan used to require an actual
# release/press to recover from, because nothing re-pressed the synthetic button
# while _right_down stayed true throughout.
log = []
ui, modifier = StubUI(log), StubUI(log)
tr = Translator(ui, modifier)
for e in button(ecodes.BTN_RIGHT, 1) + motion(5):
    tr.handle(e)
tr.yield_stroke()                            # the other mouse stirs mid-pan
tr._yield_until = time.monotonic() - 0.001   # cooldown already elapsed
log.clear()
for e in motion(4):
    tr.handle(e)
keys = [x for x in log if x[0] == 'KEY']
resumed_on_button = (keys == [('KEY','CTRL',1), ('KEY','RIGHT',1)]
                      and tr._right_emitted and tr._ctrl_down)
print(f"{'PASS' if resumed_on_button else 'FAIL'}  "
      "a button-held pan resumes after a yield without a real release/press")
if not resumed_on_button:
    print(f"      keys={keys}, right_emitted={tr._right_emitted}, ctrl_down={tr._ctrl_down}")
results.append(resumed_on_button)

# Straight after the yield, before the cooldown lapses, motion must not resume it —
# the same protection the bare-motion gesture already gets against fighting a
# wheel-zoom burst on the other mouse.
log2 = []
ui2, modifier2 = StubUI(log2), StubUI(log2)
tr2 = Translator(ui2, modifier2)
for e in button(ecodes.BTN_RIGHT, 1) + motion(5):
    tr2.handle(e)
tr2.yield_stroke()
log2.clear()
for e in motion(4):
    tr2.handle(e)
keys2 = [x for x in log2 if x[0] == 'KEY']
held_off_on_button = keys2 == [] and not tr2._right_emitted
print(f"{'PASS' if held_off_on_button else 'FAIL'}  "
      "cooldown also holds a button-held pan off right after a yield")
if not held_off_on_button:
    print(f"      keys={keys2}, right_emitted={tr2._right_emitted}")
results.append(held_off_on_button)

ns['PAN_REQUIRES_RIGHT_BUTTON'] = False    # restore the default this file exercises

# --- rotate scale ------------------------------------------------------------------
# Rotate is plain motion with no scaling of its own, so ROTATE_SCALE is what tones it
# down. Pan must never be touched by it — panning already has its own dedicated feel.

def rs_setup():
    log = []
    ui, mod = StubUI(log), StubUI(log)
    tr = Translator(ui, mod)
    ns['GATE'] = StubGate(WINDOW, WINDOW)
    ns['POINTER'] = StubPointer(CENTRE)
    return tr, log

def rel_x(log):
    return [x for x in log if x[0] == 'REL' and x[1] == 'X']

ns['PAN_REQUIRES_RIGHT_BUTTON'] = True     # bare motion rotates
ns['ROTATE_SCALE'] = 0.5

tr, log = rs_setup()
tr.handle(ev(ecodes.EV_REL, ecodes.REL_X, 10))
halved = rel_x(log) == [('REL','X',5)]
print(f"{'PASS' if halved else 'FAIL'}  rotate motion is scaled down by ROTATE_SCALE")
if not halved:
    print(f"      log={log}")
results.append(halved)

# The fractional remainder carries across samples, so a slow rotate at a low scale
# still moves eventually instead of every sample individually truncating to zero.
ns['ROTATE_SCALE'] = 0.3
tr, log = rs_setup()
for _ in range(10):
    tr.handle(ev(ecodes.EV_REL, ecodes.REL_X, 1))
total_sent = sum(x[2] for x in rel_x(log))
# The running remainder is always under 1px in magnitude, by construction — except
# floating-point slop can push a sample that should just cross a whole pixel to just
# miss it instead, so the tolerance allows exactly that much.
carried = total_sent != 0 and abs(total_sent - 10 * ns['ROTATE_SCALE']) <= 1.0
print(f"{'PASS' if carried else 'FAIL'}  a sub-1px-per-sample rotate still accumulates instead of vanishing")
if not carried:
    print(f"      sent={total_sent}, log={log}")
results.append(carried)

# 1.0 is raw, unscaled motion — how this behaved before the setting existed.
ns['ROTATE_SCALE'] = 1.0
tr, log = rs_setup()
tr.handle(ev(ecodes.EV_REL, ecodes.REL_X, 7))
raw_at_one = rel_x(log) == [('REL','X',7)]
print(f"{'PASS' if raw_at_one else 'FAIL'}  rotate_scale = 1.0 leaves motion untouched")
if not raw_at_one:
    print(f"      log={log}")
results.append(raw_at_one)

# Pan (bare motion, with the original mapping) must be unaffected by ROTATE_SCALE.
ns['PAN_REQUIRES_RIGHT_BUTTON'] = False    # bare motion pans
ns['ROTATE_SCALE'] = 0.2
tr, log = rs_setup()
tr.handle(ev(ecodes.EV_REL, ecodes.REL_X, 10))
pan_unaffected = rel_x(log) == [('REL','X',10)]
print(f"{'PASS' if pan_unaffected else 'FAIL'}  pan motion ignores ROTATE_SCALE")
if not pan_unaffected:
    print(f"      log={log}")
results.append(pan_unaffected)

# The button-held rotate (the original mapping's rotate) is scaled too.
tr, log = rs_setup()
tr.handle(ev(ecodes.EV_KEY, ecodes.BTN_RIGHT, 1))
log.clear()
tr.handle(ev(ecodes.EV_REL, ecodes.REL_X, 10))
button_rotate_scaled = rel_x(log) == [('REL','X',2)]
print(f"{'PASS' if button_rotate_scaled else 'FAIL'}  a button-held rotate is scaled too")
if not button_rotate_scaled:
    print(f"      log={log}")
results.append(button_rotate_scaled)

ns['PAN_REQUIRES_RIGHT_BUTTON'] = False    # restore the default this file exercises
ns['ROTATE_SCALE'] = 1.0                   # neutral, so an unrelated future case isn't surprised

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
