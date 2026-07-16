#!/bin/bash
# 在线进程 perf 采样脚本
# 输入: PID
# 输出: JSON 格式，包含 perf_available、perf_report 路径
# 依赖: perf (linux-tools-generic)

set -e

PID=$1
if [[ -z "$PID" ]]; then
    echo '{"error": "PID is required"}'
    exit 1
fi

PERF_AVAILABLE=false
PERF_ERROR_MSG=""

# 检查 perf 是否存在
if ! command -v perf &> /dev/null; then
    PERF_ERROR_MSG="perf command not found, please install: apt install linux-tools-generic"
    echo "{\"perf_available\": false, \"perf_error\": \"$PERF_ERROR_MSG\"}"
    exit 0
fi

# 检查 /proc/<PID> 是否可访问
if [[ ! -r "/proc/$PID" ]]; then
    PERF_ERROR_MSG="Cannot access /proc/$PID, permission denied"
    echo "{\"perf_available\": false, \"perf_error\": \"$PERF_ERROR_MSG\"}"
    exit 0
fi

# 尝试 perf 采样
PERF_OUTPUT=$(perf record -p "$PID" -g --call-graph dwarf -o "/tmp/perf_${PID}.data" -- sleep 5 2>&1)
PERF_EXIT_CODE=$?

# 检查是否有权限错误
if echo "$PERF_OUTPUT" | grep -q "Permission denied"; then
    PERF_ERROR_MSG="perf requires root permission to sample process $PID"
    echo "{\"perf_available\": false, \"perf_error\": \"$PERF_ERROR_MSG\", \"needs_authorization\": true, \"auth_command\": \"sudo chmod 755 /proc/$PID\"}"
    exit 0
fi

if [[ $PERF_EXIT_CODE -eq 0 ]] || [[ -f "/tmp/perf_${PID}.data" ]]; then
    # 导出 perf 报告（添加 30s 超时保护，避免大文件导致 perf report 挂起）
    # 使用 --stdio -n --pretty 简化输出，仅取前100行
    timeout 30 perf report --stdio -i "/tmp/perf_${PID}.data" -n 2>/dev/null | head -100 > "/tmp/perf_report_${PID}.txt" || {
        # 超时或失败时尝试更轻量的输出格式
        timeout 20 perf report -i "/tmp/perf_${PID}.data" --stdio --no-child 2>/dev/null | head -100 > "/tmp/perf_report_${PID}.txt" || {
            # 最终降级：直接用 perf report 默认输出截断
            timeout 15 perf report -i "/tmp/perf_${PID}.data" 2>/dev/null | head -100 > "/tmp/perf_report_${PID}.txt" || {
                echo "perf report generation failed or timed out" > "/tmp/perf_report_${PID}.txt"
            }
        }
    }
    PERF_AVAILABLE=true
    echo "{\"perf_available\": true, \"perf_report\": \"/tmp/perf_report_${PID}.txt\"}"
else
    PERF_ERROR_MSG="perf sampling failed: $PERF_OUTPUT"
    echo "{\"perf_available\": false, \"perf_error\": \"$PERF_ERROR_MSG\"}"
fi
