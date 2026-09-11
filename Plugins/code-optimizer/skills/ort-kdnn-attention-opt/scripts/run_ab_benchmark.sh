#!/usr/bin/env bash
# Protocolized A/B benchmark runner (Phase 1/5).
#
# Runs two env configs alternately (A,B,A,B,...) for N rounds each, saving full
# logs. Enforces the repo measurement discipline: fixed pinning, sequential,
# warmup/iter fixed, alternating rounds.
#
# Usage:
#   ./run_ab_benchmark.sh BENCH MODEL BATCH ROUNDS OUTDIR \
#       [--env-a 'K=V K=V ...'] [--env-b 'K=V K=V ...'] [--numa-node N] \
#       [--intra 16] [--warmup 10] [--iter 100] [--label name]
#
# Example:
#   ./run_ab_benchmark.sh "$BENCH" \
#       "$MODEL_PATH" 32 3 "$RESULTS_DIR" \
#       --env-a 'ORT_KDNN_FUSE_ATTENTION=0' --env-b 'ORT_KDNN_FUSE_ATTENTION=1' \
#       --numa-node 1 --label fusion
set -euo pipefail

if [ "$#" -lt 5 ]; then
  echo "usage: $0 BENCH MODEL BATCH ROUNDS OUTDIR [options]" >&2
  exit 2
fi
BENCH=$1; MODEL=$2; BATCH=$3; ROUNDS=$4; OUTDIR=$5; shift 5
ENV_A=""; ENV_B=""; NUMA_NODE=""; INTRA=16; WARMUP=10; ITER=100; LABEL="ab"
while [ $# -gt 0 ]; do
  case $1 in
    --env-a) ENV_A=$2; shift 2;;
    --env-b) ENV_B=$2; shift 2;;
    --numa-node) NUMA_NODE=$2; shift 2;;
    --intra) INTRA=$2; shift 2;;
    --warmup) WARMUP=$2; shift 2;;
    --iter) ITER=$2; shift 2;;
    --label) LABEL=$2; shift 2;;
    *) echo "unknown arg $1" >&2; exit 1;;
  esac
done

[[ "$BATCH" =~ ^[1-9][0-9]*$ && "$ROUNDS" =~ ^[1-9][0-9]*$ ]] || { echo "batch/rounds must be positive integers" >&2; exit 2; }
(( ROUNDS >= 3 )) || { echo "at least 3 paired rounds required" >&2; exit 2; }
[[ "$LABEL" =~ ^[a-zA-Z0-9_-]+$ ]] || { echo "invalid label" >&2; exit 2; }
mkdir -p "$OUTDIR"
# Caller supplies library paths; preserve the existing loader configuration.
# Do not infer an ORT checkout from the installed skill's location.
for existing in "$OUTDIR/${LABEL}_"*_r*.log; do
  [ ! -e "$existing" ] || { echo "logs already exist for label $LABEL; use a fresh label/directory" >&2; exit 2; }
done

run_one() {  # run_one <config> <round>
  local cfg=$1 r=$2 envs=$3
  local log="$OUTDIR/${LABEL}_${cfg}_r${r}.log"
  local -a binding=() assignments=()
  [ -z "$NUMA_NODE" ] || binding=(numactl -N "$NUMA_NODE" "--membind=$NUMA_NODE")
  # Space-separated KEY=VALUE entries; values containing whitespace are unsupported.
  read -r -a assignments <<< "$envs"
  for assignment in "${assignments[@]}"; do
    [[ "$assignment" =~ ^[a-zA-Z_][a-zA-Z0-9_]*= ]] || { echo "invalid env assignment" >&2; exit 2; }
  done
  env "${assignments[@]}" "${binding[@]}" "$BENCH" \
    --model-path "$MODEL" --batch-size "$BATCH" \
    --inter-op-threads 1 --intra-op-threads "$INTRA" \
    --warmup-iter "$WARMUP" --num-iter "$ITER" \
    --execution-mode sequential > "$log" 2>&1
  echo "$log"
}

echo "=== A/B benchmark: label=$LABEL batch=$BATCH rounds=$ROUNDS intra=$INTRA numa=$NUMA_NODE ==="
echo "ENV_A: $ENV_A"
echo "ENV_B: $ENV_B"
# alternate A,B per round
for r in $(seq 1 "$ROUNDS"); do
  if (( r % 2 )); then
    run_one A "$r" "$ENV_A"
    run_one B "$r" "$ENV_B"
  else
    run_one B "$r" "$ENV_B"
    run_one A "$r" "$ENV_A"
  fi
done
echo "logs in $OUTDIR"
echo "=== aggregate ==="
python3 "$(dirname "$0")/aggregate_results.py" "$OUTDIR" --label "$LABEL"
