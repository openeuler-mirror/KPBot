#!/bin/bash
# =============================================================================
# ARM SPE 数据采集脚本
# arm-spe-analysis skill - scripts/spe-collect.sh
# 用途：检查 SPE 环境、采集 SPE 采样数据
# 用法：
#   bash spe-collect.sh --check                    # 仅检查环境
#   bash spe-collect.sh -p <PID> -t 30             # 采集指定进程 30 秒
#   bash spe-collect.sh -f load -c 0-3 -t 10       # 仅采集 CPU 0-3 的 load 事件
# =============================================================================

set -euo pipefail

# ── 全局变量 ──
PID=""
CPU="all"
FILTER="all"
INTERVAL="auto"
DURATION=10
OUTPUT=""
CHECK_ONLY=false
CMD=""

# =============================================================================
# 通用基础设施
# =============================================================================

usage() {
    cat <<EOF
spe-collect.sh — ARM SPE 数据采集

用法:
  bash spe-collect.sh [选项]                       # 系统级采集
  bash spe-collect.sh -- [命令...]                   # 采集指定命令的 SPE 数据
  bash spe-collect.sh [选项] -- [命令...]             # 采集命令（可指定 filter/cpu 等）

选项:
  -p, --pid PID        附加到指定进程（与 -- 互斥）
  -c, --cpu CPU        采集指定 CPU（默认 all）
  -f, --filter TYPE    采样过滤: load,store,branch,all（默认 all）
  -i, --interval N     采样间隔（最小间隔值，越小越精确但开销大，默认 auto）
  -t, --time SECS      采集时长（秒，默认 10；使用 -- 时作为 timeout）
  -o, --output FILE    输出文件（默认 spe-<timestamp>.data）
  --check              仅检查 SPE 环境是否可用，不采集
  -h, --help           显示帮助

示例:
  bash spe-collect.sh --check
  bash spe-collect.sh -p 12345 -t 30
  bash spe-collect.sh -f load -c 0-3 -t 10 -o /tmp/spe-test.data
  bash spe-collect.sh -f all -- dd if=/dev/zero of=/dev/null bs=64k count=1000
  bash spe-collect.sh -t 10 -- ./my_benchmark --iterations=100
EOF
    exit 0
}

# =============================================================================
# 环境检查
# =============================================================================

check_spe_support() {
    local rc=0

    echo "=== ARM SPE 环境检查 ==="
    echo ""

    # 1. 架构检查
    local arch
    arch=$(uname -m)
    if [ "$arch" != "aarch64" ]; then
        echo "[FAIL] 当前架构: $arch, SPE 仅支持 aarch64"
        rc=1
    else
        echo "[ OK ] 架构: aarch64"
    fi

    # 2. SPE 设备检查
    local spe_dev
    spe_dev=$(ls -d /sys/bus/event_source/devices/arm_spe* 2>/dev/null || true)
    if [ -z "$spe_dev" ]; then
        echo "[FAIL] 未发现 SPE PMU 设备 (/sys/bus/event_source/devices/arm_spe*)"
        rc=1
    else
        echo "[ OK ] SPE PMU 设备: $spe_dev"
        local spe_type
        spe_type=$(cat /sys/bus/event_source/devices/arm_spe_0/type 2>/dev/null || echo "unknown")
        echo "       事件类型号: $spe_type"
    fi

    # 3. perf 工具检查
    if ! command -v perf &>/dev/null; then
        echo "[FAIL] perf 命令不可用"
        rc=1
    else
        local perf_ver
        perf_ver=$(perf --version 2>/dev/null || echo "unknown")
        echo "[ OK ] perf: $perf_ver"

        if grep -q "arm_spe" <(perf list 2>/dev/null); then
            echo "[ OK ] perf 支持 arm_spe 事件"
        else
            echo "[WARN] perf list 中未找到 arm_spe 事件（可能需要更新 perf 或内核）"
            rc=1
        fi
    fi

    # 4. 内核配置检查
    if [ -f /boot/config-"$(uname -r)" ]; then
        if grep -q "CONFIG_ARM_SPE_PMU=[ym]" /boot/config-"$(uname -r)" 2>/dev/null; then
            local spe_cfg
            spe_cfg=$(grep "CONFIG_ARM_SPE_PMU=" /boot/config-"$(uname -r)" 2>/dev/null)
            echo "[ OK ] 内核配置: $spe_cfg"
        else
            echo "[WARN] 内核配置中未找到 CONFIG_ARM_SPE_PMU=y/m"
            rc=1
        fi
    else
        echo "[WARN] 无法读取内核配置文件 (/boot/config-$(uname -r))"
    fi

    # 5. 权限检查
    if [ -w /proc/sys/kernel/perf_event_paranoid ]; then
        local paranoid
        paranoid=$(cat /proc/sys/kernel/perf_event_paranoid)
        if [ "$paranoid" -gt 1 ]; then
            echo "[WARN] perf_event_paranoid=$paranoid, 可能需要 root 或调低 (>1 限制非 root 采集)"
        else
            echo "[ OK ] perf_event_paranoid=$paranoid"
        fi
    else
        echo "[WARN] 无法读取 perf_event_paranoid（可能需要 root 权限）"
    fi

    echo ""
    if [ $rc -eq 0 ]; then
        echo "=== SPE 环境检查通过，可以开始采集 ==="
    else
        echo "=== SPE 环境检查存在问题，请修复后重试 ==="
    fi

    return $rc
}

# =============================================================================
# 构建 perf 事件字符串
# =============================================================================

build_spe_event() {
    local filter="$1"
    local interval="$2"
    local event="arm_spe_0"

    case "$filter" in
        load)   event="$event/load_filter=1/" ;;
        store)  event="$event/store_filter=1/" ;;
        branch) event="$event/branch_filter=1/" ;;
        all)    event="$event//" ;;
        *)      echo "错误: 不支持的 filter 类型: $filter (可选: load,store,branch,all)" >&2; exit 1 ;;
    esac

    # 只有当 interval 不为 auto 或 skip 时才添加 min_interval 参数
    if [ "$interval" != "auto" ] && [ "$interval" != "skip" ] && [ -n "$interval" ]; then
        # 先移除末尾的 /，然后添加 interval 参数
        event="${event%/}/min_interval=$interval/"
    fi

    echo "$event"
}

# =============================================================================
# 自动选择采样间隔
# =============================================================================

auto_interval() {
    local min_interval_file="/sys/bus/event_source/devices/arm_spe_0/min_interval"
    if [ -f "$min_interval_file" ]; then
        local min_val
        min_val=$(cat "$min_interval_file")
        if [ "$min_val" -eq 0 ]; then
            echo "skip"
        else
            echo $((min_val * 10))
        fi
    else
        echo "skip"
    fi
}

# =============================================================================
# 主流程
# =============================================================================

while [[ $# -gt 0 ]]; do
    case "$1" in
        -p|--pid)       PID="$2"; shift 2 ;;
        -c|--cpu)       CPU="$2"; shift 2 ;;
        -f|--filter)    FILTER="$2"; shift 2 ;;
        -i|--interval)  INTERVAL="$2"; shift 2 ;;
        -t|--time)      DURATION="$2"; shift 2 ;;
        -o|--output)    OUTPUT="$2"; shift 2 ;;
        --check)        CHECK_ONLY=true; shift ;;
        -h|--help)      usage ;;
        --)             shift; CMD="$*"; break ;;
        *)              echo "未知参数: $1"; usage ;;
    esac
done

if [ "$CHECK_ONLY" = true ]; then
    check_spe_support
    exit $?
fi

if ! check_spe_support; then
    echo "错误: SPE 环境不可用，无法采集" >&2
    exit 1
fi

if [ "$INTERVAL" = "auto" ]; then
    INTERVAL=$(auto_interval)
    echo "自动选择采样间隔: $INTERVAL"
fi

if [ -z "$OUTPUT" ]; then
    OUTPUT="spe-$(date +%Y%m%d_%H%M%S).data"
fi

SPE_EVENT=$(build_spe_event "$FILTER" "$INTERVAL")

PERF_CMD="perf record -e $SPE_EVENT"

if [ "$CPU" != "all" ]; then
    PERF_CMD="$PERF_CMD -C $CPU"
fi

if [ -n "$CMD" ]; then
    # 采集指定命令：perf record ... -- timeout <duration> <command>
    PERF_CMD="$PERF_CMD -o $OUTPUT -- timeout $DURATION $CMD"
elif [ -n "$PID" ]; then
    PERF_CMD="$PERF_CMD -p $PID -o $OUTPUT sleep $DURATION"
else
    PERF_CMD="$PERF_CMD -a -o $OUTPUT sleep $DURATION"
fi

echo ""
echo "=== 开始 SPE 采集 ==="
echo "事件:     $SPE_EVENT"
echo "CPU:      $CPU"
echo "输出:     $OUTPUT"
if [ -n "$CMD" ]; then
    echo "命令:     $CMD"
    echo "超时:     ${DURATION}s"
elif [ -n "$PID" ]; then
    echo "PID:      $PID"
    echo "时长:     ${DURATION}s"
else
    echo "时长:     ${DURATION}s (系统级)"
fi
echo ""

START_TIME=$(date +%s)
if eval "$PERF_CMD"; then
    END_TIME=$(date +%s)
    ELAPSED=$((END_TIME - START_TIME))
    echo ""
    echo "=== 采集完成 ==="
    echo "输出文件: $OUTPUT"
    echo "采集时长: ${ELAPSED}s"

    if command -v perf &>/dev/null; then
        SAMPLE_COUNT=$(perf script -i "$OUTPUT" 2>/dev/null | wc -l || echo "0")
        FILE_SIZE=$(ls -lh "$OUTPUT" | awk '{print $5}')
        echo "采样记录: ${SAMPLE_COUNT} 行"
        echo "文件大小: $FILE_SIZE"
    fi
else
    echo "错误: SPE 采集失败" >&2
    exit 1
fi
