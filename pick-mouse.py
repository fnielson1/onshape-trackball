#!/usr/bin/env python3
"""List or physically detect candidate mice for the Onshape trackball gate.

    pick-mouse.py --list             one "path<TAB>name" line per mouse
    pick-mouse.py --detect [SECS]    print the path of the mouse that gets moved

Names come from /proc/bus/input/devices, which is world-readable, so --list works
before the 'input' group is granted. --detect needs read access to the devices.
"""

import argparse
import glob
import os
import select
import sys
import time

BY_ID = "/dev/input/by-id"
MOTION_THRESHOLD = 30  # accumulated |REL| units, enough to ignore sensor jitter


def names_by_node():
    """event node -> human name, straight from procfs (no permissions needed)."""
    names, name = {}, None
    try:
        with open("/proc/bus/input/devices") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("N: Name="):
                    name = line.split("=", 1)[1].strip('"')
                elif line.startswith("H: Handlers="):
                    for handler in line.split("=", 1)[1].split():
                        if handler.startswith("event"):
                            names[handler] = name or handler
                elif not line:
                    name = None
    except OSError:
        pass
    return names


def candidates():
    """Stable by-id paths for every pointing device, with display names."""
    names = names_by_node()
    found = []
    for path in sorted(glob.glob(os.path.join(BY_ID, "*-event-mouse"))):
        node = os.path.basename(os.path.realpath(path))
        found.append((path, names.get(node, "unknown device"), node))
    return found


def detect(timeout):
    import evdev

    opened = []
    for path, name, _node in candidates():
        try:
            opened.append((evdev.InputDevice(path), path, name))
        except PermissionError:
            print(f"cannot read {path} (not in the 'input' group?)", file=sys.stderr)
        except OSError as exc:
            print(f"cannot open {path}: {exc}", file=sys.stderr)

    if not opened:
        return None

    fdmap = {dev.fd: (dev, path, name) for dev, path, name in opened}

    # Drain anything already queued so a stale event cannot decide this for us.
    for dev, _p, _n in opened:
        try:
            while dev.read_one() is not None:
                pass
        except OSError:
            pass

    travelled = {}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select(list(fdmap), [], [], 0.2)
        for fd in ready:
            dev, path, _name = fdmap[fd]
            try:
                for event in dev.read():
                    if event.type == evdev.ecodes.EV_REL and event.code in (
                        evdev.ecodes.REL_X, evdev.ecodes.REL_Y
                    ):
                        travelled[path] = travelled.get(path, 0) + abs(event.value)
                        if travelled[path] >= MOTION_THRESHOLD:
                            return path
            except OSError:
                continue
    return None


def main():
    ap = argparse.ArgumentParser(add_help=True)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true")
    group.add_argument("--detect", nargs="?", const=10.0, type=float, metavar="SECS")
    args = ap.parse_args()

    if args.list:
        for path, name, _node in candidates():
            print(f"{path}\t{name}")
        return 0

    path = detect(args.detect)
    if path is None:
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
