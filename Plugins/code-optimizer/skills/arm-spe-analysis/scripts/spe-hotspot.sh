#!/bin/bash
# =============================================================================
# ARM SPE 热点定位脚本
# arm-spe-analysis skill - scripts/spe-hotspot.sh
# 用途：基于 SPE 采样数据识别性能瓶颈（延迟热点、Cache miss、分支预测失败等）
# 用法：
#   bash spe-hotspot.sh spe-20260425.data                  # 默认按延迟定位
#   bash spe-hotspot.sh -m cache_miss -n 5 spe.data        # Cache miss Top 5
#   bash spe-hotspot.sh -m branch_mispred --by-function    # 按函数聚合分支预测
# =============================================================================

set -euo pipefail

METRIC="l1_miss"
THRESHOLD=""
TOP_N=10
BY_FUNCTION=false
DATA_FILE=""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    cat <<EOF
spe-hotspot.sh — ARM SPE 热点定位

用法:
  bash spe-hotspot.sh [选项] <perf.data>

选项:
  -m, --metric METRIC  热点指标: l1_miss, llc_miss, branch_mispred（默认 l1_miss）
  -t, --threshold N    阈值过滤（如 miss rate > N%）
  -n, --top N          Top N 热点（默认 10）
  --by-function        按函数聚合（默认按指令地址）
  -h, --help           帮助

示例:
  bash spe-hotspot.sh spe-20260425.data
  bash spe-hotspot.sh -m llc_miss -n 5 spe-20260425.data
  bash spe-hotspot.sh -m branch_mispred --by-function spe-20260425.data
  bash spe-hotspot.sh -m l1_miss -t 10 spe-20260425.data
EOF
    exit 0
}

classify_bottleneck() {
    local metric="$1"
    local value="$2"

    local pct
    pct=$(echo "$value" | awk '{printf "%.0f", $1}')
    case "$metric" in
        l1_miss)
            if [ "$pct" -gt 30 ]; then echo "SEVERE"
            elif [ "$pct" -gt 10 ]; then echo "MODERATE"
            elif [ "$pct" -gt 5 ]; then echo "MILD"
            else echo "OK"
            fi
            ;;
        llc_miss)
            if [ "$pct" -gt 30 ]; then echo "SEVERE"
            elif [ "$pct" -gt 10 ]; then echo "MODERATE"
            elif [ "$pct" -gt 5 ]; then echo "MILD"
            else echo "OK"
            fi
            ;;
        branch_mispred)
            if [ "$pct" -gt 20 ]; then echo "SEVERE"
            elif [ "$pct" -gt 5 ]; then echo "MODERATE"
            elif [ "$pct" -gt 1 ]; then echo "MILD"
            else echo "OK"
            fi
            ;;
    esac
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -m|--metric)        METRIC="$2"; shift 2 ;;
        -t|--threshold)     THRESHOLD="$2"; shift 2 ;;
        -n|--top)           TOP_N="$2"; shift 2 ;;
        --by-function)      BY_FUNCTION=true; shift ;;
        -h|--help)          usage ;;
        -*)                 echo "未知选项: $1"; usage ;;
        *)                  DATA_FILE="$1"; shift ;;
    esac
done

if [ -z "$DATA_FILE" ]; then
    echo "错误: 请指定 perf.data 文件" >&2
    usage
fi

if [ ! -f "$DATA_FILE" ]; then
    echo "错误: 文件不存在: $DATA_FILE" >&2
    exit 1
fi

PARSE_SCRIPT="$SCRIPT_DIR/spe-parse.sh"
if [ ! -f "$PARSE_SCRIPT" ]; then
    echo "错误: 找不到 spe-parse.sh（预期在 $PARSE_SCRIPT）" >&2
    exit 1
fi

SORT_FIELD="l1_miss"
case "$METRIC" in
    l1_miss)        SORT_FIELD="l1_miss" ;;
    llc_miss)       SORT_FIELD="llc_miss" ;;
    branch_mispred) SORT_FIELD="br_miss" ;;
    *)              echo "错误: 不支持的指标: $METRIC" >&2; exit 1 ;;
esac

echo "=== SPE 热点分析 (指标: $METRIC, Top $TOP_N) ==="
echo ""

PARSED=$(bash "$PARSE_SCRIPT" -s "$SORT_FIELD" -n "$TOP_N" "$DATA_FILE")

echo "$PARSED" | sed '/^=== Top/,$d'
echo ""

if [ "$BY_FUNCTION" = true ]; then
    echo "--- 按函数聚合 ---"
    case "$SORT_FIELD" in
        l1_miss) BY_FUNC_COL="L1_MISS%" ;;
        llc_miss) BY_FUNC_COL="LLC_MISS%" ;;
        br_miss) BY_FUNC_COL="BR_MISS%" ;;
    esac
    printf "%-40s  %8s  %10s\n" "FUNCTION" "SAMPLES" "$BY_FUNC_COL"
    echo "$PARSED" | awk -v metric="$SORT_FIELD" '
    /^=== Top/ { in_top=1; next }
    in_top && /^ADDRESS/ { next }
    in_top && /^[0-9a-f]/ {
        # 格式: ADDRESS COUNT L1_MISS% LLC_MISS% BR_MISS% SYMBOL
        sym = $6
        for (i = 7; i <= NF; i++) sym = sym "_" $i
        cnt = $2+0
        func_count[sym] += cnt
        if (metric == "l1_miss") {
            gsub(/%/, "", $3)
            func_metric[sym] += ($3+0) * cnt
            # removed - header now printed before pipeline
        } else if (metric == "llc_miss") {
            gsub(/%/, "", $4)
            func_metric[sym] += ($4+0) * cnt
            # removed - header now printed before pipeline
        } else if (metric == "br_miss") {
            gsub(/%/, "", $5)
            func_metric[sym] += ($5+0) * cnt
            # removed - header now printed before pipeline
        } else {
            gsub(/%/, "", $3)
            func_metric[sym] += ($3+0) * cnt
            # removed - header now printed before pipeline
        }
    }
    END {
        for (f in func_count) {
            rate = (func_count[f] > 0) ? func_metric[f]/func_count[f] : 0
            printf "%-40s  %8d  %9.1f%%\n", f, func_count[f], rate
        }
    }
    ' | sort -k3 -rn | head -n "$TOP_N" || true
else
    echo "$PARSED" | sed -n '/^=== Top/,$ p' | tail -n +2
fi

echo ""
echo "=== 瓶颈判断 ==="
if [ -n "$THRESHOLD" ]; then
    echo "${METRIC} 阈值: > ${THRESHOLD}%"
    col=3
    case "$METRIC" in
        l1_miss) col=3 ;;
        llc_miss) col=4 ;;
        branch_mispred) col=5 ;;
    esac
    echo "$PARSED" | awk -v col="$col" -v threshold="$THRESHOLD" '
    /^[0-9a-fx]/ {
        gsub(/%/, "", $col)
        if ($col+0 > threshold) {
            printf "  [HOT] %-18s  rate=%s%%  %s\n", $1, $col, $0
        }
    }
    '
else
    echo "（使用 -t N 可设置阈值过滤热点）"
fi
