#!/usr/bin/env python3
"""Cycle window boundary tests for issue/reserved record filtering."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mca_visualize as mca  # noqa: E402


def main():
    checks = [
        ("cycle 0 in window", mca._cycle_in_window(0, 11) is True),
        ("cycle C-1 in window", mca._cycle_in_window(10, 11) is True),
        ("cycle C rejected", mca._cycle_in_window(11, 11) is False),
        ("negative cycle rejected", mca._cycle_in_window(-1, 11) is False),
        ("clip C-1 duration 2 to 1", mca._clip_cycles(10, 2, 11) == (1, True)),
        ("clip C-2 duration 5 to 2", mca._clip_cycles(9, 5, 11) == (2, True)),
        ("start C clipped away", mca._clip_cycles(11, 2, 11) == (None, False)),
        ("duration 2 fits at C-2", mca._clip_cycles(9, 2, 11) == (2, False)),
        ("fractional duration rounds to 1", mca._clip_cycles(10, 0.4, 11) == (1, False)),
    ]
    failed = 0
    for name, ok in checks:
        print(("PASS " if ok else "FAIL ") + name)
        failed += 0 if ok else 1
    print("%d/%d passed" % (len(checks) - failed, len(checks)))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
