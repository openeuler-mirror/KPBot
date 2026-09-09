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
#   ./run_ab_benchmark.sh build/alipay_benchmark/alipay_dense_benchmark \
#       onnxruntime/test/alipay/models/model4.onnx 32 3 /tmp/ab \
#       --env-a 'ORT_KDNN_FUSE_ATTENTION=0' --env-b 'ORT_KDNN_FUSE_ATTENTION=1' \
#       --numa-node 1 --label fusion
set -euo pipefail

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

mkdir -p "$OUTDIR"
# repo root detection: works for <repo>/docs/mha_opt_skill/skill (main repo)
# and <ws>/skill next to <ws>/repo (agent workspace) layouts.
SELF_DIR=$(cd "$(dirname "$0")" && pwd)
REPO=""
for cand in "$SELF_DIR/../../../.." "$SELF_DIR/../../repo" "$PWD"; do
  if [ -d "$cand/kdnn/RelWithDebInfo" ] || [ -d "$cand/onnxruntime/core/kdnn" ]; then
    REPO=$(cd "$cand" && pwd); break
  fi
done
if [ -z "$REPO" ]; then
  REPO=${REPO_ROOT:-$PWD}
  echo "warning: repo root not auto-detected; set REPO_ROOT=<repo> (using $REPO)" >&2
fi
LD="${REPO}/kdnn/RelWithDebInfo:${REPO}/onnxruntime/core/kdnn/out/lib"

run_one() {  # run_one <config> <round>
  local cfg=$1 r=$2 envs=$3
  local log="$OUTDIR/${LABEL}_${cfg}_r${r}.log"
  local numactl=""
  [ -n "$NUMA_NODE" ] && numactl="numactl -N $NUMA_NODE --membind=$NUMA_NODE"
  # shellcheck disable=SC2086
  env LD_LIBRARY_PATH="$LD" $envs $numactl "$BENCH" \
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
  echo "round $r: A then B"
  run_one A "$r" "$ENV_A" >/dev/null
  run_one B "$r" "$ENV_B" >/dev/null
done
# reverse order on even rounds is handled by swapping on odd/even r:
# (keep simple alternation; reverse-order rounds recommended when ROUNDS>=4)
echo "logs in $OUTDIR"
echo "=== aggregate ==="
python3 "$(dirname "$0")/aggregate_results.py" "$OUTDIR" --label "$LABEL"
