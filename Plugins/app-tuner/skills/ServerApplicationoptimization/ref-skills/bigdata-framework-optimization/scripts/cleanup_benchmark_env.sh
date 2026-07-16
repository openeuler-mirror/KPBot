#!/usr/bin/env bash
#
# cleanup_benchmark_env.sh - 清理 benchmark 测试环境
#
# 用法:
#   cleanup_benchmark_env.sh [--jm-host <host>] [--ssh-key <key>]
#
# 示例:
#   # 清理本地容器环境
#   ./cleanup_benchmark_env.sh
#
#   # 清理远程环境
#   ./cleanup_benchmark_env.sh --jm-host root@172.17.0.3

set -euo pipefail

JM_HOST="root@172.17.0.3"
SSH_KEY=""
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=30"

# 解析参数
while [[ $# -gt 0 ]]; do
  case $1 in
    --jm-host)
      JM_HOST="$2"
      shift 2
      ;;
    --ssh-key)
      SSH_KEY="-i $2"
      shift 2
      ;;
    -h|--help)
      echo "Usage: $0 [--jm-host <host>] [--ssh-key <key>]"
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      exit 1
      ;;
  esac
done

SSH_CMD="ssh ${SSH_OPTS} ${SSH_KEY} ${JM_HOST}"

echo "=========================================="
echo "Benchmark 环境清理"
echo "=========================================="
echo "目标主机: ${JM_HOST}"
echo ""

# 1. 清理所有残留的 benchmark 进程
echo "[1/5] 清理 benchmark 进程..."
${SSH_CMD} "pkill -9 -f Benchmark 2>/dev/null || true; pkill -9 -f CpuMetricSender 2>/dev/null || true; pkill -9 -f CpuMetricReceiver 2>/dev/null || true; pkill -9 -f zdl 2>/dev/null || true; pkill -9 -f zdl.sh 2>/dev/null || true"
echo "  完成"

# 2. 清理端口占用
echo "[2/5] 清理 9098 端口占用..."
${SSH_CMD} "
  # 查找并清理占用 9098 端口的进程
  for pid in \$(lsof -t -i:9098 2>/dev/null || true); do
    kill -9 \$pid 2>/dev/null || true
  done
"
echo "  完成"

# 3. 停止 Flink 集群
echo "[3/5] 停止 Flink 集群..."
${SSH_CMD} "${FLINK_HOME:-/usr/local/flink}/bin/stop-cluster.sh 2>/dev/null || true"
echo "  完成"

# 4. 清理残留的 TM 进程
echo "[4/5] 清理 TaskManager 进程..."
${SSH_CMD} "
  for pid in \$(ps aux | grep TaskManagerRunner | grep -v grep | awk '{print \$2}'); do
    kill -9 \$pid 2>/dev/null || true
  done
  # 强制杀死可能的残留进程
  pkill -9 -f TaskManagerRunner 2>/dev/null || true
"
echo "  完成"

# 5. 重启 Flink 集群
echo "[5/5] 重启 Flink 集群..."
sleep 3
${SSH_CMD} "${FLINK_HOME:-/usr/local/flink}/bin/start-cluster.sh"
sleep 5
echo "  完成"

# 验证清理结果
echo ""
echo "=========================================="
echo "验证清理结果"
echo "=========================================="

# 检查进程残留
echo "[进程检查]"
PROCESS_COUNT=$(${SSH_CMD} "ps aux | grep -E 'Benchmark|zdl|CpuMetric' | grep -v grep | wc -l" 2>/dev/null || echo "0")
if [[ "$PROCESS_COUNT" -eq 0 ]]; then
  echo "  ✓ 无残留 benchmark 进程"
else
  echo "  ✗ 发现 $PROCESS_COUNT 个残留进程:"
  ${SSH_CMD} "ps aux | grep -E 'Benchmark|zdl|CpuMetric' | grep -v grep" 2>/dev/null || true
fi

# 检查端口占用
echo "[端口检查]"
PORT_CHECK=$(${SSH_CMD} "lsof -i :9098 2>/dev/null | grep -v COMMAND | wc -l" 2>/dev/null || echo "0")
if [[ "$PORT_CHECK" -eq 0 ]]; then
  echo "  ✓ 端口 9098 空闲"
else
  echo "  ✗ 端口 9098 仍被占用"
fi

# 检查 Flink 集群状态
echo "[Flink 集群检查]"
TM_INFO=$(${SSH_CMD} "curl -s http://localhost:8081/taskmanagers 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); tms=d[\"taskmanagers\"]; print(f\"TM: {len(tms)}, 总slots: {sum(t[\"slotsNumber\"] for t in tms)}\")'" 2>/dev/null || echo "无法获取")
echo "  $TM_INFO"

echo ""
echo "=========================================="
echo "环境清理完成"
echo "=========================================="
