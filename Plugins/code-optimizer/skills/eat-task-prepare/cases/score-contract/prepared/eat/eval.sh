#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p .work
run_dir=$(mktemp -d .work/eval-XXXXXXXX)
report="$run_dir/report.json"
printf '逐项结果 %s\n' "$report"
python3 bench.py > "$report"
python3 - "$report" <<'PY'
import json
import math
import sys
from decimal import Decimal

with open(sys.argv[1], encoding="utf-8") as stream:
    report = json.load(stream)
score = report["score"]
if not math.isfinite(score):
    sys.exit("评分器返回了非有限数值")
if not report["correct"]:
    print("至少一个用例答错或读取次数为零")
print(f"SCORE: {Decimal(str(score)):f}")
PY
