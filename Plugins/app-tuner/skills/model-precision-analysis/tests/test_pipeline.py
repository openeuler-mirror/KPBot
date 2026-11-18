#!/usr/bin/env python3
"""End-to-end checks for analyze_precision.py on the shipped eval fixtures.

Run: python3 tests/test_pipeline.py
Each case asserts the accuracy pair, the regression/improvement case ids, and that
a regression exits 1 both with the legacy --fail-on-regression flag and without it.
The gate checks assert that undecidable comparisons exit 2 and that an ambiguous or
missing dataset is an error instead of a silent fallback to some other json.
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "analyze_precision.py"
FIX = ROOT / "evals" / "fixtures"

CASES = {
    "aime2024 fp16 (unit / truncation / real regression)": (
        "case-a/opendatas/output/aime2024_gen/20261110-baseline_dsv4pro_2026-11-10-05-06-11",
        "case-a/opendatas/output/aime2024_gen/20261112-opt-fp16_dsv4pro_2026-11-12-09-31-40",
        ("90.0%", "80.0%", ["7", "9"], ["4"]),
    ),
    "gpqa bf16 (letter answers)": (
        "case-b/opendatas/output/gpqa_gen_diamond/20261109-baseline_dsv4pro_2026-11-09-22-14-03",
        "case-b/opendatas/output/gpqa_gen_diamond/20261111-opt-bf16_dsv4pro_2026-11-11-23-41-52",
        ("83.33%", "83.33%", ["3"], ["4"]),
    ),
    "aime2025 int8 (mixed answer formats)": (
        "case-c/opendatas/output/aime2025_gen/20261113-baseline_dsv4pro_2026-11-13-07-12-08",
        "case-c/opendatas/output/aime2025_gen/20261115-opt-int8_dsv4pro_2026-11-15-18-02-33",
        ("87.5%", "75.0%", ["4", "5"], ["6"]),
    ),
    "gpqa fp8 (distractor ANSWER / truncation)": (
        "case-d/opendatas/output/gpqa_gen_diamond/20261110-baseline_dsv4pro_2026-11-10-11-20-07",
        "case-d/opendatas/output/gpqa_gen_diamond/20261113-opt-fp8_dsv4pro_2026-11-13-14-05-41",
        ("83.33%", "66.67%", ["3", "5"], ["4"]),
    ),
}


def section(text: str, header: str) -> str:
    if header not in text:
        return ""
    return text.split(header)[1].split("###")[0]


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True)


def check_gates() -> int:
    failures = 0
    a = FIX / "case-a/opendatas/output/aime2024_gen/20261110-baseline_dsv4pro_2026-11-10-05-06-11"
    b = FIX / "case-b/opendatas/output/gpqa_gen_diamond/20261109-baseline_dsv4pro_2026-11-09-22-14-03"

    mixed = run(a, b)
    ok = mixed.returncode == 2 and "不可判定" in mixed.stdout
    failures += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  undecidable runs exit 2{'':<30} exit={mixed.returncode}")

    missing = run(a, a, "--dataset-name", "no_such_dataset")
    ok = missing.returncode != 0 and "--dataset-name" in missing.stderr
    failures += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  missing --dataset-name is an error{'':<21} exit={missing.returncode}")

    with tempfile.TemporaryDirectory() as tmp:
        merge = Path(tmp) / "merge"
        merge.mkdir()
        src = a / "merge" / "aime2024.json"
        shutil.copy(src, merge / "aime2024.json")
        shutil.copy(src, merge / "other.json")
        ambiguous = run(tmp, tmp)
        ok = ambiguous.returncode != 0 and "--dataset-name" in ambiguous.stderr
        failures += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  ambiguous merge dir is an error{'':<20} exit={ambiguous.returncode}")

        picked = run(tmp, tmp, "--dataset-name", "other")
        ok = picked.returncode == 0 and f"json: {merge / 'other.json'}" in picked.stdout
        failures += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  --dataset-name picks the named json{'':<16} exit={picked.returncode}")
    return failures


def main() -> int:
    failures = 0
    for name, (before, after, (acc_b, acc_a, reg, imp)) in CASES.items():
        dirs = [str(FIX / before), str(FIX / after)]
        plain = subprocess.run([sys.executable, str(SCRIPT), *dirs], capture_output=True, text=True)
        out = plain.stdout
        got_acc = re.search(r"\| 准确率 \| ([\d.]+%) \| ([\d.]+%) \|", out).groups()
        got_reg = re.findall(r"^- 题 (\d+): gold=", section(out, "### ⚠️ 回归项"), re.M)
        got_imp = re.findall(r"^- 题 (\d+): gold=", section(out, "### 改善项"), re.M)
        rc_plain = plain.returncode
        rc = subprocess.run(
            [sys.executable, str(SCRIPT), *dirs, "--fail-on-regression"], capture_output=True
        ).returncode
        plain_ok = rc_plain == 1
        failures += 0 if plain_ok else 1
        print(f"{'PASS' if plain_ok else 'FAIL'}  {name:<50} no-flag exit={rc_plain}")
        ok = got_acc == (acc_b, acc_a) and got_reg == reg and got_imp == imp and rc == 1
        failures += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  {name:<50} acc={got_acc} reg={got_reg} imp={got_imp} exit={rc}")
    failures += check_gates()
    total = 2 * len(CASES) + 4
    print(f"\n{total - failures}/{total} end-to-end cases passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
