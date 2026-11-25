#!/usr/bin/env python3
"""Compile-path test: allowed --cflags must compile; -g must be rejected first."""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mca_visualize as mca  # noqa: E402


def main():
    if not shutil.which("clang"):
        print("SKIP clang not installed")
        return 0

    checks = []
    with tempfile.TemporaryDirectory() as work:
        src = os.path.join(work, "f.c")
        with open(src, "w", encoding="utf-8") as f:
            f.write("int f(int x) { return x * 3 + 1; }\n")

        asm, ok, err = mca.compile_source_to_asm(
            src, "tsv110", ["-O3", "-funroll-loops", "-march=armv8.2-a"])
        checks.append(("allowed flags compile", ok and bool(asm.strip()), err))
        checks.append(("asm is AArch64", ok and ("x0" in asm or "w0" in asm), ""))

        asm2, ok2, err2 = mca.compile_source_to_asm(src, "tsv110", ["-g"])
        checks.append(("-g rejected before clang", (not ok2) and "不支持" in (err2 or ""),
                       err2))
        checks.append(("-g produces no asm", not asm2, ""))

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
