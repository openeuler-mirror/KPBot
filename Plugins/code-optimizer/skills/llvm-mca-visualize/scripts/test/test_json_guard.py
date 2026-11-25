#!/usr/bin/env python3
"""JSON guard tests: invalid MCA --json output must fail cleanly, not traceback."""
import contextlib
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mca_visualize as mca  # noqa: E402


def expect_die(fn, *args):
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            fn(*args)
    except SystemExit as e:
        return e.code, out.getvalue()
    return None, out.getvalue()


def main():
    checks = []

    code, out = expect_die(mca.parse_json_stats, "not-json")
    checks.append(("invalid JSON exits 1", code == 1, code))
    checks.append(("invalid JSON reports parse failure", "解析失败" in out, out[:120]))

    code, out = expect_die(mca.parse_json_stats, '{"CodeRegions": []}')
    checks.append(("empty CodeRegions exits 1", code == 1, code))
    checks.append(("empty CodeRegions reports region error", "CodeRegion" in out, out[:120]))

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
