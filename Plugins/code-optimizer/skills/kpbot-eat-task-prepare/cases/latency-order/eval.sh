#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 - <<'PY'
import json
import math
from decimal import Decimal

from bench import measure

report = measure()
print(json.dumps(report, allow_nan=False))
elapsed_ms = report["elapsed_ms"]
if not math.isfinite(elapsed_ms) or elapsed_ms <= 0:
    raise SystemExit("Expected positive, finite elapsed time")
# 本验收任务约定的固定参照值，两个候选共用。
score = 100.0 / elapsed_ms if report["correct"] else 0.0
print(f"SCORE: {Decimal(str(score)):f}")
PY
