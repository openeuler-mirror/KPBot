#!/usr/bin/env python3
"""Guard: uarch tables must stay identical to llvm-mca-analysis (single source).

The sibling skill is mandatory: a missing sibling is a FAIL (never a silent
SKIP), and a self-check proves that a one-sided change is detected.
"""
import argparse
import ast
import copy
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OWN = os.path.join(HERE, "..", "..", "scripts", "mca_visualize.py")
SIBLING = os.path.join(HERE, "..", "..", "..", "llvm-mca-analysis", "scripts", "mca_analyze.py")
NAMES = ("UARCH_MAP", "MCPU_TO_UARCH", "ALL_UARCHES", "MTRIPLE")


def load_tables(path):
    tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
    tables = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in NAMES:
                    tables[t.id] = ast.literal_eval(node.value)
    return tables


def compare_tables(own, sib):
    failures = []
    for name in NAMES:
        if name not in own:
            failures.append((name, "missing in own", ""))
        elif name not in sib:
            failures.append((name, "missing in sibling", ""))
        elif own[name] != sib[name]:
            failures.append((name, own[name], sib[name]))
    return failures


def drift_self_check(own):
    """One-sided hip09 change must be detected by compare_tables."""
    sib = copy.deepcopy(own)
    if not isinstance(sib.get("UARCH_MAP"), dict):
        return False
    sib["UARCH_MAP"]["hip09"] = "hip09-drift"
    return any(name == "UARCH_MAP" for name, _a, _b in compare_tables(own, sib))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sibling", default=SIBLING, help="path to mca_analyze.py")
    args = ap.parse_args()

    if not os.path.isfile(args.sibling):
        print("FAIL sibling skill not found:", args.sibling)
        print("     uarch table sync cannot be verified (mandatory guard)")
        return 1
    if not os.path.isfile(OWN):
        print("FAIL own script not found:", OWN)
        return 1

    own = load_tables(OWN)
    sib = load_tables(args.sibling)
    failures = compare_tables(own, sib)
    failed_names = {name for name, _a, _b in failures}

    checks = 0
    for name in NAMES:
        ok = name not in failed_names
        print(("PASS " if ok else "FAIL ") + "table %s matches sibling" % name)
        checks += 0 if ok else 1
    for name, a, b in failures:
        print("  %s own=%r sibling=%r" % (name, a, b))

    self_ok = drift_self_check(own)
    print(("PASS " if self_ok else "FAIL ") + "drift self-check (one-sided hip09)")
    checks += 0 if self_ok else 1

    total = len(NAMES) + 1
    print("%d/%d passed" % (total - checks, total))
    return 0 if checks == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
