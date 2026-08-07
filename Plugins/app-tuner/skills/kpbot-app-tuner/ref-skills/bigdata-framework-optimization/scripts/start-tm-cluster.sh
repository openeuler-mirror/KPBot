#!/usr/bin/env bash
#
# start-tm-cluster.sh - 在容器内启动多个 TaskManager 实例
#
# 用法:
#   start-tm-cluster.sh <实例数量>
#
# 示例:
#   start-tm-cluster.sh 4   # 启动 4 个 TaskManager 实例

set -euo pipefail

TM_COUNT=${1:-4}
FLINK_HOME="${FLINK_HOME:-/usr/local/flink}"
FLINK_BIN_DIR="${FLINK_HOME}/bin"

echo "Stopping any existing TaskManager processes..."
for pid in $(pgrep -f "TaskManagerRunner" 2>/dev/null || true); do
    kill -9 "$pid" 2>/dev/null || true
done
sleep 2

echo "Starting ${TM_COUNT} TaskManager instances..."

for i in $(seq 1 "$TM_COUNT"); do
    echo "  Starting TaskManager instance ${i}..."
    "${FLINK_BIN_DIR}/taskmanager.sh" start &
    sleep 1
done

echo "Waiting for TaskManagers to start..."
sleep 5

# 显示运行的 TM 进程
echo ""
echo "Running TaskManager processes:"
ps aux | grep -E "TaskManagerRunner|java.*taskexecutor" | grep -v grep || echo "No TaskManager processes found"

echo ""
echo "Done. ${TM_COUNT} TaskManager instances started."