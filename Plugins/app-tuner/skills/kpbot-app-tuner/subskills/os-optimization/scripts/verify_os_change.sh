#!/usr/bin/env bash
set -euo pipefail

# verify_os_change.sh — A/B verification for OS parameter changes
# Runs benchmark before/after parameter change, calculates gain_pct,
# applies threshold-based decision (keep/ask/rollback)

BENCHMARK_CMD=""
CHANGED_BENCHMARK_CMD=""
BENCHMARK_TYPE="sysbench"
METRIC="tps"
METRIC_REGEX=""
METRIC_NAME=""
THRESHOLD="0.3"
OUTPUT_DIR="./os-optimization-output/verification"
ACTION=""
KEY=""
OLD_VALUE=""
NEW_VALUE=""
ROLLBACK_CMD=""
BASELINE_ONLY=false

usage() {
  cat <<'EOF'
Usage:
  verify_os_change.sh --benchmark-cmd <cmd> [options]

Required:
  --benchmark-cmd <cmd>     Benchmark command for baseline (quoted)
  --changed-benchmark-cmd <cmd>  Benchmark command for changed (default: same as --benchmark-cmd)

Options:
  --benchmark-type <type>   sysbench|pgbench|redis-benchmark|custom (default: sysbench)
  --metric <name>           tps|qps|p95|p99 (default: tps)
  --metric-regex <regex>    Custom regex for metric extraction (custom type)
  --metric-name <name>      Custom metric name (custom type)
  --threshold <pct>         Verification threshold in % (default: 0.3)
  --output-dir <dir>        Output directory (default: ./os-optimization-output/verification)
  --action <name>           Action being verified (for logging)
  --key <name>              Parameter name being changed
  --old-value <value>       Original parameter value
  --new-value <value>       New parameter value
  --rollback-cmd <cmd>      Rollback command if gain < 0
  --baseline-only           Only run baseline benchmark (no change applied)
  -h, --help                Show help

Examples:
  # Sysbench TPS verification
  verify_os_change.sh \
    --benchmark-cmd "sysbench oltp_read_only --tables=10 --table-size=1000000 --threads=40 run" \
    --benchmark-type sysbench --metric tps --threshold 0.3 \
    --action sysctl --key vm.dirty_ratio --old-value 10 --new-value 5 \
    --rollback-cmd "sysctl -w vm.dirty_ratio=10"

  # Custom benchmark
  verify_os_change.sh \
    --benchmark-cmd "./my_bench.sh" \
    --benchmark-type custom --metric-regex 'RESULT: tps=(\d+\.?\d*)' --metric-name tps \
    --threshold 0.5
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --benchmark-cmd) BENCHMARK_CMD="$2"; shift 2 ;;
    --changed-benchmark-cmd) CHANGED_BENCHMARK_CMD="$2"; shift 2 ;;
    --benchmark-type) BENCHMARK_TYPE="$2"; shift 2 ;;
    --metric) METRIC="$2"; shift 2 ;;
    --metric-regex) METRIC_REGEX="$2"; shift 2 ;;
    --metric-name) METRIC_NAME="$2"; shift 2 ;;
    --threshold) THRESHOLD="$2"; shift 2 ;;
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    --action) ACTION="$2"; shift 2 ;;
    --key) KEY="$2"; shift 2 ;;
    --old-value) OLD_VALUE="$2"; shift 2 ;;
    --new-value) NEW_VALUE="$2"; shift 2 ;;
    --rollback-cmd) ROLLBACK_CMD="$2"; shift 2 ;;
    --baseline-only) BASELINE_ONLY=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done

[[ -n "${BENCHMARK_CMD}" ]] || { echo "ERROR: --benchmark-cmd is required" >&2; exit 1; }

mkdir -p "${OUTPUT_DIR}"

# ── Metric extraction ────────────────────────────────────────

extract_metric() {
  local output_file="$1"
  local metric_value=""

  case "${BENCHMARK_TYPE}" in
    sysbench)
      case "${METRIC}" in
        tps|qps)
          metric_value=$(grep -oP 'tps: \K[\d.]+' "${output_file}" | head -1 || \
                         grep -oP 'queries: \K[\d.]+' "${output_file}" | head -1 || echo "")
          ;;
        p95)
          metric_value=$(grep -oP 'avg:\s+\K[\d.]+|p95\):\s+\K[\d.]+' "${output_file}" | tail -1 || echo "")
          ;;
        p99)
          metric_value=$(grep -oP 'p99\):\s+\K[\d.]+' "${output_file}" | head -1 || echo "")
          ;;
      esac
      ;;
    pgbench)
      metric_value=$(grep -oP 'tps = \K[\d.]+' "${output_file}" | head -1 || echo "")
      ;;
    redis-benchmark)
      metric_value=$(grep -oP '\K[\d.]+(?= requests per second)' "${output_file}" | head -1 || echo "")
      ;;
    custom)
      local regex="${METRIC_REGEX}"
      local name="${METRIC_NAME:-metric}"
      if [[ -n "${regex}" ]]; then
        metric_value=$(grep -oP "${regex}" "${output_file}" | head -1 || echo "")
        # Extract last capture group if present
        metric_value=$(printf '%s\n' "${metric_value}" | grep -oP '[\d.]+$' || echo "${metric_value}")
      fi
      ;;
  esac

  if [[ -z "${metric_value}" ]]; then
    echo "ERROR" >&2
    return 1
  fi
  echo "${metric_value}"
}

# ── Run benchmark ────────────────────────────────────────────

run_benchmark() {
  local label="$1"
  local cmd="${2:-${BENCHMARK_CMD}}"
  local output_file="${OUTPUT_DIR}/${label}_benchmark.txt"

  echo "  Running benchmark: ${label}..."
  echo "  Command: ${cmd}"

  if bash -c "${cmd}" > "${output_file}" 2>&1; then
    local metric_value
    metric_value=$(extract_metric "${output_file}") || {
      echo "  ⚠️  Failed to extract metric '${METRIC}' from ${BENCHMARK_TYPE} output"
      echo "  Output saved to: ${output_file}"
      return 1
    }
    echo "  ✅ ${label}: ${METRIC}=${metric_value}"
    echo "${metric_value}" > "${OUTPUT_DIR}/${label}_${METRIC}.txt"
  else
    echo "  ❌ Benchmark failed (exit $?)"
    echo "  Output saved to: ${output_file}"
    return 1
  fi
}

# ── Threshold decision ───────────────────────────────────────

threshold_decision() {
  local gain_pct="$1"
  local threshold="${THRESHOLD}"

  echo ""
  echo "  ── Verification Result ──"
  echo "  Action: ${ACTION:-unknown}"
  echo "  Parameter: ${KEY:-unknown} (${OLD_VALUE:-?} → ${NEW_VALUE:-?})"
  echo "  Gain: ${gain_pct}%"
  echo "  Threshold: ${threshold}%"

  # Use python for float comparison
  local decision
  decision=$(python3 -c "
gain = float('${gain_pct}')
threshold = float('${threshold}')
if gain >= threshold:
    print('verified_effective')
elif gain >= 0:
    print('below_threshold')
else:
    print('harmful')
")

  case "${decision}" in
    verified_effective)
      echo "  Decision: ✅ VERIFIED EFFECTIVE (gain >= threshold)"
      echo "  Action: KEEP"
      ;;
    below_threshold)
      echo "  Decision: ⚠️  BELOW THRESHOLD (0 <= gain < threshold)"
      echo "  Action: ASK USER"
      echo ""
      echo "  Parameter ${KEY:-} changed (${OLD_VALUE:-}→${NEW_VALUE:-}), gain ${gain_pct}% < threshold ${threshold}%."
      echo "  This gain is small but may be meaningful in cumulative optimization."
      echo "  Keep this change? [Y]es / [N]o (rollback) / [A]djust threshold"
      ;;
    harmful)
      echo "  Decision: ❌ HARMFUL (gain < 0)"
      echo "  Action: AUTO ROLLBACK"
      if [[ -n "${ROLLBACK_CMD}" ]]; then
        echo "  Executing rollback: ${ROLLBACK_CMD}"
        bash -c "${ROLLBACK_CMD}" 2>&1 || echo "  ⚠️  Rollback command failed"
      fi
      ;;
  esac

  # Write verification result JSON
  python3 - "${OUTPUT_DIR}/verification_result.json" "${ACTION}" "${KEY}" \
    "${OLD_VALUE}" "${NEW_VALUE}" "${gain_pct}" "${threshold}" "${decision}" <<'PYEOF'
import json, os, sys
from datetime import datetime, timezone

out_path, action, key, old_val, new_val, gain, threshold, decision = sys.argv[1:9]

result = {
    "action": action or "unknown",
    "param": key or "unknown",
    "old_value": old_val,
    "new_value": new_val,
    "gain_pct": float(gain),
    "threshold_pct": float(threshold),
    "decision": decision,
    "verified": decision == "verified_effective",
    "timestamp": datetime.now(timezone.utc).isoformat(),
}
os.makedirs(os.path.dirname(out_path), exist_ok=True)
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
    f.write("\n")
print(f"  Result written to: {out_path}")
PYEOF
}

# ── Main ─────────────────────────────────────────────────────

echo "=== OS Change A/B Verification ==="
echo "  Benchmark: ${BENCHMARK_TYPE} / metric: ${METRIC}"
echo "  Threshold: ${THRESHOLD}%"
echo ""

# Step 1: Baseline benchmark
echo "--- Step 1: Baseline ---"
run_benchmark "baseline" || { echo "ERROR: Baseline benchmark failed" >&2; exit 1; }

BASELINE_METRIC=$(cat "${OUTPUT_DIR}/baseline_${METRIC}.txt")

if [[ "${BASELINE_ONLY}" == true ]]; then
  echo ""
  echo "=== Baseline only ==="
  echo "  ${METRIC}=${BASELINE_METRIC}"
  exit 0
fi

# Step 2: Apply change (caller's responsibility)
# The caller applies the change between baseline and changed benchmark
echo ""
echo "--- Step 2: Apply change ---"
echo "  Action: ${ACTION:-unknown}"
echo "  Parameter: ${KEY:-unknown} (${OLD_VALUE:-?} → ${NEW_VALUE:-?})"
echo "  (Change should be applied by caller before running changed benchmark)"
echo ""

# Step 3: Changed benchmark
echo "--- Step 3: Changed benchmark ---"
run_benchmark "changed" "${CHANGED_BENCHMARK_CMD}" || { echo "ERROR: Changed benchmark failed" >&2; exit 1; }

CHANGED_METRIC=$(cat "${OUTPUT_DIR}/changed_${METRIC}.txt")

# Step 4: Calculate gain
echo ""
echo "--- Step 4: Calculate gain ---"
GAIN_PCT=$(python3 -c "
baseline = float('${BASELINE_METRIC}')
changed = float('${CHANGED_METRIC}')
if baseline == 0:
    print('0.0')
else:
    gain = (changed - baseline) / baseline * 100
    print(f'{gain:.4f}')
")

echo "  Baseline: ${METRIC}=${BASELINE_METRIC}"
echo "  Changed:  ${METRIC}=${CHANGED_METRIC}"
echo "  Gain: ${GAIN_PCT}%"

# Step 5: Threshold decision
threshold_decision "${GAIN_PCT}"

echo ""
echo "=== Verification complete ==="
