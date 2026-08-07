#!/bin/bash
# 在线进程 perf 采样脚本
# 输入: PID
# 输出: JSON 格式，包含 perf_available、perf_report 路径、maps_libs_path、lsof_libs_path、environ_path
# 依赖: perf (linux-tools-generic)
# 说明: 本脚本自包含采集 perf 热点 + 进程库列表(lsof 降级 /proc/maps) + 环境变量(LD_PRELOAD/MALLOC_CONF)，
#       输出路径供 detect_all_libraries.sh 直接使用，无需 LLM 手动拼接 evidence JSON

# 不使用 set -e：脚本需要捕获 perf record 的非零退出码并降级处理，set -e 会导致错误处理逻辑不可达

PID=$1
if [[ -z "$PID" ]]; then
    echo '{"error": "PID is required"}'
    exit 1
fi

# -------------------------------------------------------------------------
# 0. 采集进程库列表与环境变量（与 perf 采样独立，即使 perf 失败也输出）
# -------------------------------------------------------------------------
LSOF_LIBS_FILE="/tmp/lsof_libs_${PID}.txt"
MAPS_LIBS_FILE="/tmp/maps_libs_${PID}.txt"
ENVIRON_FILE="/tmp/environ_${PID}.txt"

# lsof 采集（容器内可能返回空）
lsof -p "$PID" 2>/dev/null | grep '\.so' | awk '{print $NF}' | sort -u > "$LSOF_LIBS_FILE" 2>/dev/null || true

# /proc/maps 降级采集（lsof 返回空时作为补充，两者输出互补）
cat /proc/"$PID"/maps 2>/dev/null | grep '\.so' | awk '{print $6}' | sort -u > "$MAPS_LIBS_FILE" 2>/dev/null || true

# 环境变量采集（LD_PRELOAD / MALLOC_CONF / TCMALLOC_* 等）
cat /proc/"$PID"/environ 2>/dev/null | tr '\0' '\n' | grep -E '^LD_PRELOAD=|^MALLOC_CONF=|^TCMALLOC|^HEAPPROFILE=' > "$ENVIRON_FILE" 2>/dev/null || true

PERF_AVAILABLE=false
PERF_ERROR_MSG=""

# 检查 perf 是否存在
if ! command -v perf &> /dev/null; then
    PERF_ERROR_MSG="perf command not found, please install: apt install linux-tools-generic"
    echo "{\"perf_available\": false, \"perf_error\": \"$PERF_ERROR_MSG\", \"maps_libs_path\": \"$MAPS_LIBS_FILE\", \"lsof_libs_path\": \"$LSOF_LIBS_FILE\", \"environ_path\": \"$ENVIRON_FILE\"}"
    exit 0
fi

# 检查 /proc/<PID> 是否可访问
if [[ ! -r "/proc/$PID" ]]; then
    PERF_ERROR_MSG="Cannot access /proc/$PID, permission denied"
    echo "{\"perf_available\": false, \"perf_error\": \"$PERF_ERROR_MSG\", \"maps_libs_path\": \"$MAPS_LIBS_FILE\", \"lsof_libs_path\": \"$LSOF_LIBS_FILE\", \"environ_path\": \"$ENVIRON_FILE\"}"
    exit 0
fi

# 尝试 perf 采样
PERF_OUTPUT=$(perf record -p "$PID" -g --call-graph dwarf -o "/tmp/perf_${PID}.data" -- sleep 5 2>&1)
PERF_EXIT_CODE=$?

# 检查是否有权限错误
if echo "$PERF_OUTPUT" | grep -q "Permission denied"; then
    PERF_ERROR_MSG="perf requires permission to sample process $PID"
    echo "{\"perf_available\": false, \"perf_error\": \"$PERF_ERROR_MSG\", \"needs_authorization\": true, \"auth_hints\": [\"sudo sysctl -w kernel.perf_event_paranoid=1\", \"sudo setcap cap_perfmon,cap_sys_ptrace=ep $(command -v perf)\"], \"maps_libs_path\": \"$MAPS_LIBS_FILE\", \"lsof_libs_path\": \"$LSOF_LIBS_FILE\", \"environ_path\": \"$ENVIRON_FILE\"}"
    exit 0
fi

# 生成 perf 报告的辅助函数
# 参数: <输出文件> <data文件> <超时秒> <perf report 额外参数...>
# 返回: 0=成功且输出非空, 1=失败或输出为空
# 关键: DWARF debug 信息损坏（常见于 CANN/第三方库）会导致 perf report 空输出但 exit 0，
#       且大 DWARF data 文件(>100MB)处理可能超时；因此必须用 [[ -s ]] 检查文件非空，
#       而非依赖退出码触发 || 降级
generate_report() {
    local out_file="$1"
    local data_file="$2"
    local timeout_sec="$3"
    shift 3
    : > "$out_file"
    timeout "$timeout_sec" perf report --stdio -i "$data_file" "$@" 2>/dev/null | head -200 > "$out_file" 2>/dev/null || true
    [[ -s "$out_file" ]]
}

if [[ $PERF_EXIT_CODE -eq 0 ]] || [[ -f "/tmp/perf_${PID}.data" ]]; then
    REPORT_FILE="/tmp/perf_report_${PID}.txt"
    DWARF_DATA="/tmp/perf_${PID}.data"
    IP_DATA="/tmp/perf_${PID}_ip.data"
    USED_DATA_FILE=""

    # 多级降级：每级检查输出非空，空输出（DWARF error 或超时）触发降级
    # Level 1: 标准 -n 模式（带调用次数和完整调用链，依赖 DWARF）
    # DWARF error 时快速返回空输出（非超时），[[ -s ]] 检测到空则降级
    if generate_report "$REPORT_FILE" "$DWARF_DATA" 30 -n; then
        USED_DATA_FILE="$DWARF_DATA"
    else
        # Level 2: DWARF data 可能因大文件(>100MB)处理超时或 DWARF 损坏，
        # 重新用 IP-only 模式采样（无 call-graph，data 仅 ~1MB，report 处理快）
        perf record -p "$PID" -o "$IP_DATA" -- sleep 5 2>/dev/null || true
        if generate_report "$REPORT_FILE" "$IP_DATA" 15 -F overhead,dso,symbol --no-children; then
            USED_DATA_FILE="$IP_DATA"
        elif generate_report "$REPORT_FILE" "$IP_DATA" 10 -F overhead,dso --no-children; then
            USED_DATA_FILE="$IP_DATA"
        else
            # Level 3: IP-only data 仅 DSO 维度（无符号解析，最可靠降级）
            echo "perf report generation failed (DWARF corrupted and IP-only fallback failed)" > "$REPORT_FILE"
        fi
    fi

    # 额外生成 Self% 符号报告（--no-children -F overhead,dso,symbol），供 detect_all_libraries.sh 直接消费
    # 主报告可能为 call-graph 格式（含 Children% 和调用树），detect_all_libraries.sh 的正则期望 Self% 干净三列格式
    SELF_REPORT_FILE="/tmp/perf_self_report_${PID}.txt"
    if [[ -n "$USED_DATA_FILE" ]] && [[ -f "$USED_DATA_FILE" ]]; then
        generate_report "$SELF_REPORT_FILE" "$USED_DATA_FILE" 20 -F overhead,dso,symbol --no-children || true
    fi

    PERF_AVAILABLE=true
    echo "{\"perf_available\": true, \"perf_report\": \"$REPORT_FILE\", \"perf_self_report_path\": \"$SELF_REPORT_FILE\", \"perf_data\": \"$USED_DATA_FILE\", \"maps_libs_path\": \"$MAPS_LIBS_FILE\", \"lsof_libs_path\": \"$LSOF_LIBS_FILE\", \"environ_path\": \"$ENVIRON_FILE\"}"
else
    PERF_ERROR_MSG="perf sampling failed: $PERF_OUTPUT"
    echo "{\"perf_available\": false, \"perf_error\": \"$PERF_ERROR_MSG\", \"maps_libs_path\": \"$MAPS_LIBS_FILE\", \"lsof_libs_path\": \"$LSOF_LIBS_FILE\", \"environ_path\": \"$ENVIRON_FILE\"}"
fi
