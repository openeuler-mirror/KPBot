#!/usr/bin/env bash
set -euo pipefail

case_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
skill_dir="$(cd -- "$case_dir/../.." && pwd)"
python="${TTFT_PYTHON:-python3}"

mkdir -p "$case_dir/results"
if PYTHONUTF8=1 "$python" -B "$skill_dir/scripts/smoke_skill.py" 2>&1 \
    | tee "$case_dir/results/smoke.log"; then
    printf '{"success":true}\n' > "$case_dir/results/result.json"
else
    printf '{"success":false}\n' > "$case_dir/results/result.json"
    exit 1
fi
