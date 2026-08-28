"""Drive Translator with synthetic evdev events against a stub UInput."""
import os, sys, time, types
from evdev import ecodes, InputEvent

src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gate.py')).read()
src = src.replace('if __name__ == "__main__":', 'if False:')
ns = {}
exec(compile(src, 'gate.py', 'exec'), ns)
Translator = ns['Translator']

NAMES = {ecodes.BTN_LEFT:'LEFT', ecodes.BTN_RIGHT:'RIGHT', ecodes.BTN_MIDDLE:'MIDDLE'}

class StubUI:
    def __init__(self): self.log = []
    def write(self, t, c, v):
        self.log.append(('KEY', NAMES.get(c, c), v) if t == ecodes.EV_KEY else (t, c, v))
    def write_event(self, e):
        if e.type == ecodes.EV_KEY: self.log.append(('KEY', NAMES.get(e.code, e.code), e.value))
        elif e.type == ecodes.EV_REL: self.log.append(('REL', 'X' if e.code==ecodes.REL_X else ('Y' if e.code==ecodes.REL_Y else 'WHEEL'), e.value))
        elif e.type == ecodes.EV_SYN: self.log.append(('SYN',))
    def syn(self): self.log.append(('SYN',))

def ev(t, c, v): return InputEvent(0, 0, t, c, v)
def motion(dx): return [ev(ecodes.EV_REL, ecodes.REL_X, dx), ev(ecodes.EV_SYN, 0, 0)]
def button(code, val): return [ev(ecodes.EV_KEY, code, val), ev(ecodes.EV_SYN, 0, 0)]

def run(name, events, expect, post=None):
    ui = StubUI(); tr = Translator(ui)
    for e in events: tr.handle(e)
    if post: post(tr)
    got = ui.log
    ok = got == expect
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"      expected: {expect}")
        print(f"      got     : {got}")
    return ok

results = []

# 1. Bare motion synthesises a held middle button, then passes motion through.
results.append(run("motion alone starts a pan drag",
    motion(5) + motion(7),
    [('KEY','MIDDLE',1), ('REL','X',5), ('SYN',), ('REL','X',7), ('SYN',)]))

# 2. Going idle past PAN_IDLE_RELEASE ends the stroke.
def idle_then_tick(tr):
    tr._last_motion -= 1.0
    tr.tick()
results.append(run("idle releases the pan button",
    motion(5),
    [('KEY','MIDDLE',1), ('REL','X',5), ('SYN',), ('KEY','MIDDLE',0), ('SYN',)],
    post=idle_then_tick))

# 3. Right button interrupts the pan and hands off to native rotate.
results.append(run("right button ends pan, then rotates",
    motion(5) + button(ecodes.BTN_RIGHT, 1) + motion(9),
    [('KEY','MIDDLE',1), ('REL','X',5), ('SYN',),
     ('KEY','MIDDLE',0), ('KEY','RIGHT',1), ('SYN',),
     ('REL','X',9), ('SYN',)]))

# 4. Releasing the right button returns to panning.
results.append(run("pan resumes after right button release",
    button(ecodes.BTN_RIGHT, 1) + motion(3) + button(ecodes.BTN_RIGHT, 0) + motion(4),
    [('KEY','RIGHT',1), ('SYN',), ('REL','X',3), ('SYN',),
     ('KEY','RIGHT',0), ('SYN',),
     ('KEY','MIDDLE',1), ('REL','X',4), ('SYN',)]))

# 5. Gate closing mid-stroke must not strand the synthetic button down.
results.append(run("gate close releases synthetic button",
    motion(5),
    [('KEY','MIDDLE',1), ('REL','X',5), ('SYN',), ('KEY','MIDDLE',0), ('SYN',)],
    post=lambda tr: tr.release_all()))

# 6. Gate close mid real click releases that too.
results.append(run("gate close releases a real held button",
    button(ecodes.BTN_RIGHT, 1),
    [('KEY','RIGHT',1), ('SYN',), ('KEY','RIGHT',0), ('SYN',)],
    post=lambda tr: tr.release_all()))

# 7. Wheel passes through untouched (zoom), without starting a pan.
results.append(run("wheel passes through without panning",
    [ev(ecodes.EV_REL, ecodes.REL_WHEEL, 1), ev(ecodes.EV_SYN, 0, 0)],
    [('REL','WHEEL',1), ('SYN',)]))

# 8. Left click ends the pan stroke first so the click isn't a middle-drag.
results.append(run("left click ends pan stroke first",
    motion(5) + button(ecodes.BTN_LEFT, 1),
    [('KEY','MIDDLE',1), ('REL','X',5), ('SYN',),
     ('KEY','MIDDLE',0), ('KEY','LEFT',1), ('SYN',)]))

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
