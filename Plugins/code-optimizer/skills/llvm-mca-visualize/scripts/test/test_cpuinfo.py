#!/usr/bin/env python3
"""CPU identity detection tests (implementer+part, no third-party deps)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mca_visualize as mca  # noqa: E402


def info(impl, part, proc=0):
    return ("processor\t: %d\n"
            "CPU implementer\t: %s\n"
            "CPU part\t: %s\n" % (proc, impl, part))


def main():
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    u, ident, err = mca.detect_uarch_from_cpuinfo_text(info("0x48", "0xd06"))
    check("HiSilicon 0x48/0xd06 -> hip12", u == "hip12" and not err,
          (u, ident, err))
    check("HiSilicon ident has implementer", "implementer=0x48" in (ident or ""),
          ident)

    u, ident, err = mca.detect_uarch_from_cpuinfo_text(info("0x41", "0xd03"))
    check("Cortex-A53 0x41/0xd03 rejected", u is None and bool(err), (u, err))
    check("Cortex-A53 error mentions implementer", "0x41" in (err or ""), err)
    check("Cortex-A53 error asks --uarch", "--uarch" in (err or ""), err)

    mixed = info("0x48", "0xd06", 0) + info("0x41", "0xd03", 1)
    u, ident, err = mca.detect_uarch_from_cpuinfo_text(mixed)
    check("mixed identities rejected", u is None and bool(err), (u, err))
    check("mixed error mentions ambiguity", "多个" in (err or "") or "multiple" in (err or "").lower(),
          err)

    missing_impl = "processor\t: 0\nCPU part\t: 0xd06\n"
    u, ident, err = mca.detect_uarch_from_cpuinfo_text(missing_impl)
    check("missing implementer rejected", u is None and bool(err), (u, err))

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
