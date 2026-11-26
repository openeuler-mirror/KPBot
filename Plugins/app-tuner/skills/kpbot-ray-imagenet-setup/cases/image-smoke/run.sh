#!/usr/bin/env bash
set -u
SKILL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$SKILL_DIR"
mkdir -p results

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
OUT="${KPBOT_SMOKE_OUTPUT:-results/runs/$RUN_ID}"

if [ -e "$OUT" ]; then
  echo "output directory already exists: $OUT; choosing a new run directory is required" >&2
  mkdir -p results/image-smoke
  printf '{"success": false, "error": "output directory already exists: %s"}\n' "$OUT" \
    > results/image-smoke/result.json
  exit 2
fi

if python3 cases/image-smoke/check.py --output-dir "$OUT"; then
  code=0
else
  code=$?
fi

mkdir -p results/image-smoke
if [ -f "$OUT/result.json" ]; then
  cp "$OUT/result.json" results/image-smoke/result.json
else
  printf '{"success": false, "error": "acceptance did not write result.json", "output_dir": "%s"}\n' "$OUT" \
    > results/image-smoke/result.json
fi
exit "$code"
