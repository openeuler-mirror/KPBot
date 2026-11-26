#!/usr/bin/env python3
"""Structured (--json/--out) output contract for analyze_precision.py.

Run: python3 tests/test_json_output.py
Exit code 0 means the machine-readable result matches the documented schema and
that exit codes stay consistent with the markdown path.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "analyze_precision.py"
FIX = ROOT / "evals" / "fixtures"

A0 = FIX / "case-a/opendatas/output/aime2024_gen/20261110-baseline_dsv4pro_2026-11-10-05-06-11"
A1 = FIX / "case-a/opendatas/output/aime2024_gen/20261112-opt-fp16_dsv4pro_2026-11-12-09-31-40"
B0 = FIX / "case-b/opendatas/output/gpqa_gen_diamond/20261109-baseline_dsv4pro_2026-11-09-22-14-03"

REQUIRED = {
    "schema_version", "before", "after", "status_flip", "regressions",
    "improvements", "gold_mismatch", "only_one_side", "comparable",
    "verdict", "exit_code",
}

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, ("  | " + detail) if detail else ""))


def run_json(*args):
    r = subprocess.run([sys.executable, str(SCRIPT), *map(str, args), "--json"],
                       capture_output=True, text=True)
    try:
        return r, json.loads(r.stdout)
    except ValueError:
        return r, {}


def write_case(base, name, payload):
    merge = Path(base) / name / "merge"
    merge.mkdir(parents=True)
    (merge / "data.json").write_text(json.dumps(payload), encoding="utf-8")
    return Path(base) / name


def item(prediction, gold):
    return {"origin_prompt": "p", "prediction": prediction, "gold": gold, "exec_times": 1}


def main():
    r, d = run_json(A0, A1)
    check("schema keys present", REQUIRED.issubset(d), str(sorted(REQUIRED - set(d))))
    check("regression case: verdict=fail / exit=1", d.get("verdict") == "fail" and d.get("exit_code") == 1)
    check("regression case: ids [7, 9]", d.get("status_flip", {}).get("regressions") == ["7", "9"])
    check("regression case: accuracy 90.0 -> 80.0",
          d.get("before", {}).get("accuracy") == 90.0 and d.get("after", {}).get("accuracy") == 80.0)
    check("regression entries carry gold and answers",
          [x["id"] for x in d.get("regressions", [])] == ["7", "9"]
          and d["regressions"][0]["gold"] == "45")
    check("process exit code matches result.exit_code", r.returncode == d.get("exit_code"))

    r, d = run_json(A0, B0)
    check("incomparable: verdict=undecidable / exit=2",
          d.get("verdict") == "undecidable" and d.get("exit_code") == 2
          and d.get("comparable") is False)

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "result.json"
        p = subprocess.run([sys.executable, str(SCRIPT), str(A0), str(A1), "--json", "--out", str(out)],
                           capture_output=True, text=True)
        check("--out writes the same JSON as stdout",
              p.returncode == 1 and out.is_file() and out.read_text() == p.stdout)

    with tempfile.TemporaryDirectory() as td:
        b = write_case(td, "b", {})
        a = write_case(td, "a", {})
        r, d = run_json(b, a)
        check("two empty runs are undecidable / exit=2, not a pass",
              r.returncode == 2 and d.get("verdict") == "undecidable"
              and d.get("comparable") is False)

    with tempfile.TemporaryDirectory() as td:
        b = write_case(td, "b", {"1": item("\\boxed{5}", "5"), "2": item("\\boxed{7}", "7")})
        a = write_case(td, "a", {"1": item("\\boxed{6}", "5")})
        r = subprocess.run([sys.executable, str(SCRIPT), str(b), str(a)],
                           capture_output=True, text=True)
        check("markdown verdict follows the structured undecidable verdict",
              r.returncode == 2 and "不可判定" in r.stdout
              and "未达标（存在回归）" not in r.stdout
              and "回归项 (1)" in r.stdout)

    with tempfile.TemporaryDirectory() as td:
        b = write_case(td, "b", {"1": item("\\boxed{5}", "5")})
        a = write_case(td, "a", {"1": item("\\boxed{5}", "5")})
        src = b / "merge" / "data.json"
        orig = src.read_bytes()
        p = subprocess.run([sys.executable, str(SCRIPT), str(b), str(a), "--json", "--out", str(src)],
                           capture_output=True, text=True)
        check("--out refuses to overwrite an input result file",
              p.returncode == 3 and p.stdout == "" and src.read_bytes() == orig)

        link = Path(td) / "link.json"
        link.symlink_to(src)
        p = subprocess.run([sys.executable, str(SCRIPT), str(b), str(a), "--json", "--out", str(link)],
                           capture_output=True, text=True)
        check("--out refuses a symlink alias to an input file",
              p.returncode == 3 and src.read_bytes() == orig)

        hard = Path(td) / "hard.json"
        os.link(src, hard)
        p = subprocess.run([sys.executable, str(SCRIPT), str(b), str(a), "--json", "--out", str(hard)],
                           capture_output=True, text=True)
        check("--out refuses a hardlink alias to an input file",
              p.returncode == 3 and src.read_bytes() == orig)

        p = subprocess.run(
            [sys.executable, str(SCRIPT), str(b), str(a), "--json",
             "--out", str(Path(td) / "missing" / "out.json")],
            capture_output=True, text=True)
        check("--out write failure emits no report and exits with the output-error code",
              p.returncode == 3 and p.stdout == "")

    failed = [x for x in results if not x[1]]
    print("\n%d/%d json-output checks passed" % (len(results) - len(failed), len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
