#!/usr/bin/env python3
"""Tier-A contract tests for hot-path-overhead-elimination.

Covers three groups:

  scripts    value_distribution.py and codegen_overhead_probe.py against
             synthetic fixtures: histogram/coverage decisions, numeric bin
             collapsing, AArch64（鲲鹏）反汇编解析，以及 compare 模式
             mechanically checks a codegen_expectation.
  skill      SKILL.md invariants: frontmatter, every recipe documented, no
             library-name routing in the skill body, and every referenced
             script/reference actually exists.
  contract   the output object carries the fields downstream stages read.

Prints "PYTEST_SUMMARY: {json}" on the last line.
"""

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(os.path.dirname(HERE))
SCRIPTS = os.path.join(SKILL_DIR, "scripts")

results = []


def record(group, name, ok, detail=""):
    results.append({"group": group, "name": name, "ok": bool(ok), "detail": detail})
    print(("ok   " if ok else "FAIL ") + f"[{group}] {name}" + (f" :: {detail}" if detail and not ok else ""))


def run_py(script, args, expect_rc=0):
    proc = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, script)] + args,
        capture_output=True, text=True,
    )
    return proc


def test_value_distribution(tmp):
    # Concentrated distribution: top-3 reaches 0.9 but the top-1 does not.
    path = os.path.join(tmp, "lengths.txt")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(["5"] * 60 + ["4"] * 20 + ["6"] * 12 + ["2"] * 8))
    proc = run_py("value_distribution.py", ["--input", path, "--json"])
    data = json.loads(proc.stdout) if proc.stdout.strip() else {}
    record("scripts", "value_distribution concentrated parses",
           proc.returncode == 0 and data.get("ok") is True, proc.stderr[-200:])
    record("scripts", "value_distribution top-1 share is 0.6",
           abs(data.get("top1_share", -1) - 0.6) < 1e-9, str(data.get("top1_share")))
    record("scripts", "value_distribution min_k for 0.9 is 3",
           data.get("min_k_for_threshold") == 3, str(data.get("min_k_for_threshold")))
    record("scripts", "value_distribution recommends value specialisation",
           data.get("recommendation") == "constant_width_copy",
           str(data.get("recommendation")))
    record("scripts", "value_distribution histogram is frequency ordered",
           [row["value"] for row in data.get("histogram", [])][:2] == ["5", "4"],
           str([row["value"] for row in data.get("histogram", [])][:2]))

    # Flat distribution must be rejected instead of specialised.
    flat = os.path.join(tmp, "flat.txt")
    with open(flat, "w", encoding="utf-8") as handle:
        handle.write("\n".join(str(i) for i in range(1, 9)))
    proc = run_py("value_distribution.py", ["--input", flat, "--json", "--max-k", "4"])
    data = json.loads(proc.stdout) if proc.stdout.strip() else {}
    record("scripts", "value_distribution rejects flat distribution",
           data.get("recommendation") == "reject", str(data.get("recommendation")))

    # Single outcome => dominant outcome family.
    dominant = os.path.join(tmp, "outcomes.txt")
    with open(dominant, "w", encoding="utf-8") as handle:
        handle.write("\n".join(["no_callbacks"] * 90 + ["callbacks"] * 10))
    proc = run_py("value_distribution.py", ["--input", dominant, "--json"])
    data = json.loads(proc.stdout) if proc.stdout.strip() else {}
    record("scripts", "value_distribution recommends dominant outcome inline",
           data.get("recommendation") == "dominant_outcome_inline",
           str(data.get("recommendation")))

    # JSON share input and numeric bin collapsing.
    shares = os.path.join(tmp, "shares.json")
    with open(shares, "w", encoding="utf-8") as handle:
        json.dump({"5": 0.8, "4.0": 0.2}, handle)
    proc = run_py("value_distribution.py", ["--input", shares, "--json"])
    data = json.loads(proc.stdout) if proc.stdout.strip() else {}
    record("scripts", "value_distribution treats unit-sum JSON as shares",
           data.get("source") == "json-shares", str(data.get("source")))
    record("scripts", "value_distribution collapses 4.0 and '4' into one bin",
           data.get("distinct") == 2, str(data.get("distinct")))

    # Missing input must fail loudly.
    proc = run_py("value_distribution.py", ["--input", os.path.join(tmp, "nope.txt"), "--json"])
    record("scripts", "value_distribution fails on missing input", proc.returncode != 0)


A64_BASE = """0000000000400a00 <caller>:
  400a00:\tstp\tx29, x30, [sp, #-32]!
  400a04:\tmov\tx29, sp
  400a08:\tbl\t400b40 <callee>
  400a0c:\tldp\tx29, x30, [sp], #32
  400a10:\tret

0000000000400b40 <callee>:
  400b40:\tldr\tx1, [x0, #120]
  400b44:\tcbz\tx1, 400b50 <callee+0x10>
  400b48:\tbl\t400c00 <slow>
  400b50:\tret
"""

A64_CAND = """0000000000400a00 <caller>:
  400a00:\tldr\tx1, [x0, #120]
  400a04:\tcbz\tx1, 400a10 <caller+0x10>
  400a08:\tbl\t400c00 <slow>
  400a10:\tret
"""

A64_INDIRECT = """0000000000400d00 <indirect_caller>:
  400d00:\tblr\tx8
  400d04:\tret
"""


def write(tmp, name, text):
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def test_codegen_probe(tmp):
    base = write(tmp, "base.dump", A64_BASE)
    cand = write(tmp, "cand.dump", A64_CAND)
    indirect = write(tmp, "indirect.dump", A64_INDIRECT)

    proc = run_py("codegen_overhead_probe.py", ["--dump", base, "--symbol", "caller", "--json"])
    data = json.loads(proc.stdout) if proc.stdout.strip() else {}
    symbol = data.get("symbol", {})
    record("scripts", "codegen probe counts direct calls",
           symbol.get("direct_calls") == 1, str(symbol.get("direct_calls")))
    record("scripts", "codegen probe resolves callee symbol name",
           "callee" in symbol.get("call_targets", []), str(symbol.get("call_targets")))
    record("scripts", "codegen probe measures stack frame",
           symbol.get("stack_bytes") == 32, str(symbol.get("stack_bytes")))
    record("scripts", "codegen probe marks non-leaf function",
           symbol.get("leaf") is False, str(symbol.get("leaf")))

    proc = run_py("codegen_overhead_probe.py", ["--dump", base, "--compare", cand,
                                                "--callee", "callee", "--symbol", "caller",
                                                "--offset", "0x78", "--json"])
    data = json.loads(proc.stdout) if proc.stdout.strip() else {}
    record("scripts", "compare passes when callee call sites disappear",
           data.get("verdict") == "pass", str(data.get("verdict")))
    offsets = data.get("symbol", {}).get("offset_operand_count", {})
    record("scripts", "compare reports predicate moved into caller",
           offsets.get("before") == 0 and offsets.get("after") == 1, str(offsets))

    # A candidate that did not change the call must fail the mechanical check.
    proc = run_py("codegen_overhead_probe.py", ["--dump", base, "--compare", base,
                                                "--callee", "callee", "--json"])
    data = json.loads(proc.stdout) if proc.stdout.strip() else {}
    record("scripts", "compare fails when nothing was removed",
           data.get("verdict") == "fail" and proc.returncode == 1,
           f"verdict={data.get('verdict')} rc={proc.returncode}")

    # Cheap conditional early exit: the callee tests a field and returns.
    proc = run_py("codegen_overhead_probe.py", ["--dump", base, "--symbol", "callee", "--json"])
    data = json.loads(proc.stdout) if proc.stdout.strip() else {}
    early = data.get("symbol", {}).get("early_exit", {})
    record("scripts", "codegen probe detects a conditional early exit",
           early.get("present") is True, json.dumps(early))

    # Indirect calls through a register must not be counted as direct ones.
    proc = run_py("codegen_overhead_probe.py", ["--dump", indirect, "--symbol",
                                                "indirect_caller", "--json"])
    data = json.loads(proc.stdout) if proc.stdout.strip() else {}
    symbol = data.get("symbol", {})
    record("scripts", "codegen probe separates indirect calls",
           symbol.get("indirect_calls") == 1 and symbol.get("direct_calls") == 0,
           json.dumps({k: symbol.get(k) for k in ("direct_calls", "indirect_calls")}))


def test_skill_invariants():
    path = os.path.join(SKILL_DIR, "SKILL.md")
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()

    record("skill", "frontmatter has name",
           text.startswith("---\n") and "name: hot-path-overhead-elimination" in text.split("---")[1])
    record("skill", "frontmatter has description",
           "description:" in text.split("---")[1])
    record("skill", "frontmatter name matches directory",
           os.path.basename(SKILL_DIR) == "hot-path-overhead-elimination")

    recipes = [
        "dominant_outcome_inline", "leaf_entry_split", "constant_width_copy",
        "hot_value_first_dispatch", "absent_consumer_elision", "snapshot_before_copy",
    ]
    missing = [r for r in recipes if r not in text]
    record("skill", "all six recipes documented", not missing, str(missing))

    # Routing must not depend on a specific library or function name.
    body = text.split("## Compatibility")[0]
    record("skill", "routing body free of library names",
           "libevent" not in body.lower() and "kvrocks" not in body.lower(),
           "library name leaked into routing body")

    for asset in ("scripts/value_distribution.py", "scripts/codegen_overhead_probe.py",
                  "references/methodology.md",
                  "references/case-study-buffer-append-notify.md"):
        record("skill", f"referenced asset exists: {asset}",
               os.path.exists(os.path.join(SKILL_DIR, asset)),
               os.path.join(SKILL_DIR, asset))
        record("skill", f"asset referenced from SKILL.md: {asset}",
               os.path.basename(asset) in text or asset in text)

    for field in ("hot_path_result", "guard_completeness", "codegen_expectation",
                  "fallback_preserved", "error_message"):
        record("contract", f"output contract documents {field}", field in text)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        test_value_distribution(tmp)
        test_codegen_probe(tmp)
        test_skill_invariants()

    failures = [r for r in results if not r["ok"]]
    summary = {
        "ran": len(results),
        "failures": len(failures),
        "groups": {
            group: {
                "ran": sum(1 for r in results if r["group"] == group),
                "failures": sum(1 for r in results if r["group"] == group and not r["ok"]),
            }
            for group in sorted({r["group"] for r in results})
        },
        "failed_names": [r["name"] for r in failures],
    }
    print("PYTEST_SUMMARY: " + json.dumps(summary, ensure_ascii=False))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
