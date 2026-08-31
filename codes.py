"""Input event constants, vendored so the core imports on any platform.

`gate.py` used to take these from `evdev.ecodes`, which pinned the whole daemon —
and both test suites — to Linux. The values here are the ones from the kernel's
`input-event-codes.h`; they are ABI-stable, and `backend_linux` asserts at import
that they still match what evdev reports, so a drift is a loud failure rather than
a silent mistranslation.

Only the codes the translator actually touches are defined. Backends speak this
vocabulary at their edges: the Linux one passes evdev's values straight through
(they are identical), the Windows one maps to and from Interception's stroke flags.

Use as a drop-in for the old import:  import codes as ecodes
"""

# --- event types ----------------------------------------------------------------
EV_SYN = 0x00
EV_KEY = 0x01
EV_REL = 0x02
EV_MSC = 0x04

SYN_REPORT = 0
MSC_SCAN = 0x04

# --- relative axes ---------------------------------------------------------------
REL_X = 0x00
REL_Y = 0x01
REL_HWHEEL = 0x06
REL_WHEEL = 0x08
REL_WHEEL_HI_RES = 0x0B
REL_HWHEEL_HI_RES = 0x0C

# --- buttons ----------------------------------------------------------------------
BTN_LEFT = 0x110
BTN_RIGHT = 0x111
BTN_MIDDLE = 0x112
BTN_SIDE = 0x113
BTN_EXTRA = 0x114

# --- keys --------------------------------------------------------------------------
KEY_ESC = 1
KEY_LEFTCTRL = 29
KEY_SPACE = 57
KEY_F12 = 88


def assert_matches_evdev(ecodes):
    """Fail loudly if the vendored values ever drift from the installed evdev.

    Called by backend_linux at import. A mismatch would otherwise show up as the
    translator quietly emitting the wrong button.
    """
    mismatched = []
    for name, mine in sorted(globals().items()):
        if not name.isupper() or not isinstance(mine, int):
            continue
        theirs = getattr(ecodes, name, None)
        if theirs is not None and theirs != mine:
            mismatched.append(f"{name}: vendored {mine}, evdev {theirs}")
    if mismatched:
        raise AssertionError(
            "codes.py disagrees with evdev.ecodes:\n  " + "\n  ".join(mismatched))
