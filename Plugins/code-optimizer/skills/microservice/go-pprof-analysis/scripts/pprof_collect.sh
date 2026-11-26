#!/usr/bin/env bash
# pprof_collect.sh — 从运行中的 Go 服务一键采集 pprof 数据
# 用法: pprof_collect.sh <host:port> <输出目录> [CPU采样秒数]
# 示例: pprof_collect.sh localhost:6060 ./profiles 30
set -euo pipefail

ADDR="${1:?用法: pprof_collect.sh <host:port> <输出目录> [CPU采样秒数]}"
OUT_DIR="${2:?缺少输出目录}"
SECONDS_CPU="${3:-30}"
BASE="http://${ADDR}/debug/pprof"
TS="$(date +%Y%m%d-%H%M%S)"

mkdir -p "$OUT_DIR"

if ! curl -sf -o /dev/null --max-time 5 "${BASE}/"; then
    echo "错误: 无法访问 ${BASE}/ — 目标程序未接入 net/http/pprof 或地址不对" >&2
    exit 1
fi

echo "[1/5] goroutine 文本栈 (debug=1)..."
curl -sf "${BASE}/goroutine?debug=1" -o "${OUT_DIR}/goroutine-${TS}.txt"
grep -m1 'goroutine profile: total' "${OUT_DIR}/goroutine-${TS}.txt" || true

echo "[2/5] heap..."
curl -sf "${BASE}/heap" -o "${OUT_DIR}/heap-${TS}.pprof"

echo "[3/5] allocs..."
curl -sf "${BASE}/allocs" -o "${OUT_DIR}/allocs-${TS}.pprof"

echo "[4/5] CPU profile (${SECONDS_CPU}s，期间请保持负载)..."
curl -sf "${BASE}/profile?seconds=${SECONDS_CPU}" -o "${OUT_DIR}/cpu-${TS}.pprof"

echo "[5/5] trace (5s)..."
curl -sf "${BASE}/trace?seconds=5" -o "${OUT_DIR}/trace-${TS}.out"

cat <<EOF

采集完成，产物在 ${OUT_DIR}/:
  cpu-${TS}.pprof       go tool pprof -top ${OUT_DIR}/cpu-${TS}.pprof
  heap-${TS}.pprof      go tool pprof -sample_index=inuse_space -top ${OUT_DIR}/heap-${TS}.pprof
  allocs-${TS}.pprof    go tool pprof -top ${OUT_DIR}/allocs-${TS}.pprof
  goroutine-${TS}.txt   文本直接阅读，按栈顶分组数数量
  trace-${TS}.out       go tool trace ${OUT_DIR}/trace-${TS}.out

提示: block/mutex 需目标程序设置 runtime.SetBlockProfileRate / SetMutexProfileFraction 后才有数据：
  curl -sf "${BASE}/block" -o block.pprof
  curl -sf "${BASE}/mutex" -o mutex.pprof
EOF
