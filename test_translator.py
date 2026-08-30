"""Drive Translator with synthetic evdev events against a stub UInput."""
import os, sys, time, types
from evdev import ecodes, InputEvent

src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gate.py')).read()
src = src.replace('if __name__ == "__main__":', 'if False:')
ns = {}
exec(compile(src, 'gate.py', 'exec'), ns)
Translator = ns['Translator']

NAMES = {ecodes.BTN_LEFT:'LEFT', ecodes.BTN_RIGHT:'RIGHT', ecodes.BTN_MIDDLE:'MIDDLE',
         ecodes.KEY_LEFTCTRL:'CTRL', ecodes.KEY_SPACE:'SPACE'}

# The cases below are about gesture mechanics and use small synthetic motions, so
# the dead zone is off for them. It has its own section at the end.
ns['PAN_DEADZONE'] = 0

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

# 4. Releasing it ends the gesture; the next movement starts a fresh pan.
results.append(run("pan resumes after the right button is released",
    button(ecodes.BTN_RIGHT, 1) + motion(3) + button(ecodes.BTN_RIGHT, 0) + motion(4),
    [('KEY','RIGHT',1), ('KEY','RIGHT',0), ('KEY','CTRL',1), ('KEY','RIGHT',1)]))

# 5. The gate closing mid-stroke must strand neither the button nor the modifier.
results.append(run("gate close releases the button and Ctrl",
    motion(5),
    [('KEY','CTRL',1), ('KEY','RIGHT',1), ('KEY','RIGHT',0), ('KEY','CTRL',0)],
    post=lambda tr: tr.release_all()))

# 6. Including a button the user is physically holding.
results.append(run("gate close releases a real held button",
    button(ecodes.BTN_RIGHT, 1),
    [('KEY','RIGHT',1), ('KEY','RIGHT',0)],
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

class StubPointer:
    def __init__(self, pos): self.ok = True; self._pos = pos; self.warps = []
    def position(self): return self._pos
    def warp(self, x, y): self.warps.append((x, y)); self._pos = (x, y)

class StubGate:
    """geometry() is the Chrome window; view_rect() is what the cursor must stay
    inside — the canvas when one is known. They differ so a test can tell which one
    the code actually consulted."""
    def __init__(self, geom, canvas=None):
        self._geom = geom
        self._canvas = canvas
    def geometry(self): return self._geom
    def view_rect(self): return self._canvas if self._canvas is not None else self._geom

WINDOW = (0, 0, 1920, 1080)          # x, y, w, h
CENTRE = (960, 540)

def run_recentre(name, pointer_at, expect_warps, expect_log):
    ui = StubUI(); tr = Translator(ui)
    ns['GATE'] = StubGate(WINDOW)
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
ns['GATE'] = StubGate(WINDOW)
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
ns['GATE'] = StubGate(WINDOW)
pointer = StubPointer(CENTRE); ns['POINTER'] = pointer
tr._last_edge_check = 0.0
tr._recenter_if_near_edge()
left_alone = pointer.warps == [] and ui.log == []
print(f"{'PASS' if left_alone else 'FAIL'}  no stroke and inside the view: left alone")
results.append(left_alone)

# A recentre stalls the read loop, so it must refresh the idle deadline or the
# timer tears down the stroke it just restored.
ui = StubUI(); tr = Translator(ui)
ns['GATE'] = StubGate(WINDOW)
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
# clean scroll rather than wheel-with-middle-held.
ui = StubUI(); tr = Translator(ui)
ns['GATE'] = StubGate(WINDOW)
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
ns['GATE'] = StubGate(WINDOW)
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
ns['GATE'] = StubGate(WINDOW)
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
ns['GATE'] = StubGate(WINDOW)
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
    ns['GATE'] = StubGate(WINDOW)
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

# Releasing it ends the gesture exactly once.
log.clear()
tr.handle(ev(ecodes.EV_KEY, ecodes.BTN_RIGHT, 0))
released = ([x for x in log if x[0] == 'KEY'] == [('KEY','RIGHT',0)]
            and not tr._right_emitted)
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

# --- every release must be a drag, never a bare click ---------------------------
# Ctrl + right-click with no movement opens Chrome's context menu mid-pan.


# Press then release with the pointer never moving: a nudge must be emitted first.
tr, log = ctrl_setup()
tr._press_pan()
log.clear()
tr._release_right_button()
nudged = ([x[0] for x in log] == ['REL', 'SYN', 'KEY', 'SYN']
          and log[0][1] == 'X' and abs(log[0][2]) == ns['MIN_DRAG_PX']
          and log[2] == ('KEY','RIGHT',0)
          and tr.drag_nudges == 1)
print(f"{'PASS' if nudged else 'FAIL'}  a motionless release is nudged into a drag first")
if not nudged:
    print(f"      log={log}, nudges={tr.drag_nudges}")
results.append(nudged)

# Already dragged far enough: no extra nudge, so no stray pan.
tr, log = ctrl_setup()
tr._press_pan()
ns['POINTER']._pos = (CENTRE[0] + 200, CENTRE[1])
log.clear()
tr._release_right_button()
clean = (log == [('KEY','RIGHT',0), ('SYN',)] and tr.drag_nudges == 0)
print(f"{'PASS' if clean else 'FAIL'}  a real drag is released without an extra nudge")
if not clean:
    print(f"      log={log}, nudges={tr.drag_nudges}")
results.append(clean)

# The nudge steers away from the window edge rather than off it.
tr, log = ctrl_setup()
tr._press_pan()
ns['POINTER']._pos = (WINDOW[0] + WINDOW[2] - 2, CENTRE[1])
tr._last_press_pos = ns['POINTER']._pos
log.clear()
tr._release_right_button()
inward = any(x[0] == 'REL' and x[2] < 0 for x in log)
print(f"{'PASS' if inward else 'FAIL'}  the nudge steers inward at the window edge")
if not inward:
    print(f"      log={log}")
results.append(inward)

# Ending a pan normally goes through the same path.
tr, log = ctrl_setup()
for e in motion(1):
    tr.handle(e)
log.clear()
tr._end_pan(syn=True)
ordered = [x for x in log if x[0] in ('REL','KEY')]
via_nudge = (ordered and ordered[0][0] == 'REL'
             and ('KEY','RIGHT',0) in ordered and ('KEY','CTRL',0) in ordered
             and ordered.index(('KEY','RIGHT',0)) < ordered.index(('KEY','CTRL',0)))
print(f"{'PASS' if via_nudge else 'FAIL'}  ending a pan nudges, releases right, then Ctrl")
if not via_nudge:
    print(f"      log={log}")
results.append(via_nudge)

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
falls_back = gate.view_rect() == WINDOW_RECT
print(f"{'PASS' if falls_back else 'FAIL'}  view_rect uses the window when no canvas is known")
results.append(falls_back)

gate.set_canvas(CANVAS_RECT)
prefers = gate.view_rect() == CANVAS_RECT
print(f"{'PASS' if prefers else 'FAIL'}  view_rect prefers the canvas when it is fresh")
results.append(prefers)

gate._canvas_at -= (ns['CANVAS_STALE_AFTER'] + 1)
stale = gate.view_rect() == WINDOW_RECT
print(f"{'PASS' if stale else 'FAIL'}  view_rect falls back once the canvas goes stale")
results.append(stale)

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
    ns['GATE'] = StubGate(WINDOW)
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

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
