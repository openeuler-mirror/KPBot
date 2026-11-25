#!/usr/bin/env python3
"""--cflags allowlist tests (no clang needed)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mca_visualize as mca  # noqa: E402

ALLOWED = [
    "-O3",
    "-march=armv8.2-a+sve",
    "-mtune=tsv110",
    "-funroll-loops",
    "-ffast-math",
    "-fopenmp",
    "-ffp-contract=fast",
    "-mprefer-vector-width=256",
]
REJECTED = [
    "-g",
    "-g3",
    "-DNDEBUG",
    "-I/tmp",
    "-include", "x.h",
    "-std=c11",
    "-Werror",
    "-mcpu=hip12",
    "-flto",
    "-Xclang",
    "-save-temps",
    "-o", "x.s",
    "-MD",
]


def main():
    checks = []
    for flag in ALLOWED:
        err = mca.validate_extra_cflags([flag])
        checks.append(("allow %s" % flag, err == "", err))
    for flag in REJECTED:
        err = mca.validate_extra_cflags([flag])
        checks.append(("reject %s" % flag, err != "", err))

    failed = 0
    for name, ok, detail in checks:
        print(("PASS " if ok else "FAIL ") + name)
        if not ok:
            failed += 1
            print("     detail:", detail)
    print("%d/%d passed" % (len(checks) - failed, len(checks)))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
