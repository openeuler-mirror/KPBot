#!/usr/bin/env bash
#
# start-multiple-tm.sh - 在容器内启动多个 TaskManager 实例
#
# 用法:
#   start-multiple-tm.sh <实例数量>
#
# 示例:
#   start-multiple-tm.sh 4   # 启动 4 个 TaskManager 实例

set -euo pipefail

TM_COUNT=${1:-4}
FLINK_HOME="${FLINK_HOME:-/usr/local/flink-1.19.2}"
FLINK_BIN_DIR="${FLINK_HOME}/bin"
FLINK_CONF_DIR="${FLINK_HOME}/conf"
FLINK_LOG_DIR="${FLINK_HOME}/log"

echo "Stopping any existing TaskManager processes..."
pkill -9 -f TaskManagerRunner 2>/dev/null || true
sleep 2

echo "Starting ${TM_COUNT} TaskManager instances..."

for i in $(seq 0 $((TM_COUNT - 1))); do
    echo "  Starting TaskManager instance ${i}..."

    # 使用 flink-daemon.sh 启动，每个实例有不同的 HOSTNAME 后缀
    HOSTNAME="tm-${i}" \
    FLINK_CONF_DIR="${FLINK_CONF_DIR}" \
    FLINK_LOG_DIR="${FLINK_LOG_DIR}" \
    "${FLINK_BIN_DIR}/flink-daemon.sh" start taskexecutor &

    sleep 1
done

echo "Waiting for TaskManagers to start..."
sleep 8

# 显示运行的 TM 进程
echo ""
echo "Running TaskManager processes:"
ps aux | grep TaskManagerRunner | grep -v grep || echo "No TaskManager processes found"

# 显示注册的 TM 数量
echo ""
echo "Total TaskManager processes:"
ps aux | grep TaskManagerRunner | grep -v grep | wc -l