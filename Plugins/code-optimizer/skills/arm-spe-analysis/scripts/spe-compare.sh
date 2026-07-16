#!/bin/bash
# =============================================================================
# ARM SPE 前后对比脚本
# arm-spe-analysis skill - scripts/spe-compare.sh
# 用途：对比优化前后的 SPE 采样数据，输出 diff 报告
# 用法：
#   bash spe-compare.sh before.data after.data
#   bash spe-compare.sh -m l1_miss before.data after.data
#   bash spe-compare.sh -f json before.data after.data
# =============================================================================

set -euo pipefail

METRIC="all"
FORMAT="text"
BEFORE_FILE=""
AFTER_FILE=""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    cat <<EOF
spe-compare.sh — ARM SPE 前后对比

用法:
  bash spe-compare.sh [选项] <before.data> <after.data>

选项:
  -m, --metric METRIC  对比指标: l1_miss, llc_miss, branch_mispred, all（默认 all）
  -f, --format FORMAT  输出格式: text, json（默认 text）
  -h, --help           帮助

示例:
  bash spe-compare.sh before.data after.data
  bash spe-compare.sh -m l1_miss before.data after.data
  bash spe-compare.sh -f json before.data after.data
EOF
    exit 0
}

extract_summary() {
    local parsed="$1"
    echo "$parsed" | awk '
    /^总记录数:/ { total=$2 }
    /^L1 miss:/ {
        l1_miss_cnt=$3
        gsub(/[()%]/, "", $4); l1_miss_pct=$4+0
    }
    /^LLC miss:/ {
        llc_miss_cnt=$3
        gsub(/[()%]/, "", $4); llc_miss_pct=$4+0
    }
    /^Remote:/ { remote_cnt=$2; gsub(/[()%]/, "", $3); remote_pct=$3+0 }
    /^TLB access:/ { tlb_access_cnt=$3 }
    /^TLB miss:/ {
        tlb_miss_cnt=$3
        gsub(/[()%]/, "", $4); tlb_miss_pct=$4+0
    }
    /^Branch miss:/ {
        branch_miss_cnt=$3
        gsub(/[()%]/, "", $4); branch_miss_pct=$4+0
    }
    END {
        printf "total=%d l1_miss=%d l1_miss_pct=%.1f llc_miss=%d llc_miss_pct=%.1f remote=%d remote_pct=%.1f tlb_access=%d tlb_miss=%d tlb_miss_pct=%.1f branch_miss=%d branch_miss_pct=%.1f\n",
            total, l1_miss_cnt, l1_miss_pct, llc_miss_cnt, llc_miss_pct, remote_cnt, remote_pct, tlb_access_cnt, tlb_miss_cnt, tlb_miss_pct, branch_miss_cnt, branch_miss_pct
    }
    '
}

compute_diff() {
    local before_summary="$1"
    local after_summary="$2"

    eval "$before_summary"
    local b_total=$total b_l1=$l1_miss b_l1_pct=$l1_miss_pct
    local b_llc=$llc_miss b_llc_pct=$llc_miss_pct b_remote=$remote b_remote_pct=$remote_pct
    local b_tlb_acc=$tlb_access b_tlb=$tlb_miss b_tlb_pct=$tlb_miss_pct
    local b_br=$branch_miss b_br_pct=$branch_miss_pct

    eval "$after_summary"
    local a_total=$total a_l1=$l1_miss a_l1_pct=$l1_miss_pct
    local a_llc=$llc_miss a_llc_pct=$llc_miss_pct a_remote=$remote a_remote_pct=$remote_pct
    local a_tlb_acc=$tlb_access a_tlb=$tlb_miss a_tlb_pct=$tlb_miss_pct
    local a_br=$branch_miss a_br_pct=$branch_miss_pct

    calc_row() {
        local b_val="$1" a_val="$2" label="$3"
        local diff pct marker

        diff=$(echo "$a_val $b_val" | awk '{printf "%.1f", $1 - $2}')
        if [ "$(echo "$b_val" | awk '{printf "%.1f", $1}')" != "0.0" ]; then
            pct=$(echo "$a_val $b_val" | awk '{printf "%+.1f", ($1-$2)/$2*100}')
        else
            pct="N/A"
        fi

        if [ "$(echo "$diff" | awk '{printf "%.1f", $1}')" != "0.0" ]; then
            if [ "$(echo "$diff < 0" | bc -l 2>/dev/null || echo 0)" = "1" ]; then
                marker="[IMPROVED]"
            else
                marker="[REGRESSED]"
            fi
        else
            marker="[NO CHANGE]"
        fi

        printf "  %-22s  %10s  %10s  %10s  %8s  %s\n" "$label" "$b_val" "$a_val" "$diff" "$pct%" "$marker"
    }

    echo "=== 对比报告 ==="
    echo ""
    printf "  %-22s  %10s  %10s  %10s  %8s  %s\n" "指标" "优化前" "优化后" "变化" "变化率" "判定"
    echo "  ----------------------------------------------------------------------------------------"

    if [ "$METRIC" = "all" ] || [ "$METRIC" = "l1_miss" ]; then
        calc_row "$b_l1" "$a_l1" "L1 miss(次)"
        calc_row "$b_l1_pct" "$a_l1_pct" "L1 miss(%)"
        echo "  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -"
    fi
    if [ "$METRIC" = "all" ] || [ "$METRIC" = "llc_miss" ]; then
        calc_row "$b_llc" "$a_llc" "LLC miss(次)"
        calc_row "$b_llc_pct" "$a_llc_pct" "LLC miss(%)"
        echo "  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -  -"
    fi
    if [ "$METRIC" = "all" ] || [ "$METRIC" = "branch_mispred" ]; then
        calc_row "$b_br" "$a_br" "Branch miss(次)"
        calc_row "$b_br_pct" "$a_br_pct" "Branch miss(%)"
    fi

    echo ""
    echo "--- 上下文 ---"
    echo "  采样总数:  优化前=$b_total, 优化后=$a_total"
    echo "  Remote:    优化前=$b_remote ($b_remote_pct%), 优化后=$a_remote ($a_remote_pct%)"
    echo "  TLB access: 优化前=$b_tlb_acc, 优化后=$a_tlb_acc"
    echo "  TLB miss:  优化前=$b_tlb ($b_tlb_pct%), 优化后=$a_tlb ($a_tlb_pct%)"
}

output_json() {
    local before_summary="$1"
    local after_summary="$2"

    eval "$before_summary"
    local b_total=$total b_l1=$l1_miss b_l1_pct=$l1_miss_pct
    local b_llc=$llc_miss b_llc_pct=$llc_miss_pct b_remote=$remote b_remote_pct=$remote_pct
    local b_tlb=$tlb_miss b_tlb_pct=$tlb_miss_pct
    local b_br=$branch_miss b_br_pct=$branch_miss_pct

    eval "$after_summary"
    local a_total=$total a_l1=$l1_miss a_l1_pct=$l1_miss_pct
    local a_llc=$llc_miss a_llc_pct=$llc_miss_pct a_remote=$remote a_remote_pct=$remote_pct
    local a_tlb=$tlb_miss a_tlb_pct=$tlb_miss_pct
    local a_br=$branch_miss a_br_pct=$branch_miss_pct

    cat <<JSONEOF
{
  "before": {
    "total": $b_total,
    "l1_miss": $b_l1, "l1_miss_pct": $b_l1_pct,
    "llc_miss": $b_llc, "llc_miss_pct": $b_llc_pct,
    "remote": $b_remote, "remote_pct": $b_remote_pct,
    "tlb_miss": $b_tlb, "tlb_miss_pct": $b_tlb_pct,
    "branch_miss": $b_br, "branch_miss_pct": $b_br_pct
  },
  "after": {
    "total": $a_total,
    "l1_miss": $a_l1, "l1_miss_pct": $a_l1_pct,
    "llc_miss": $a_llc, "llc_miss_pct": $a_llc_pct,
    "remote": $a_remote, "remote_pct": $a_remote_pct,
    "tlb_miss": $a_tlb, "tlb_miss_pct": $a_tlb_pct,
    "branch_miss": $a_br, "branch_miss_pct": $a_br_pct
  },
  "diff": {
    "l1_miss": $(echo "$a_l1 $b_l1" | awk '{printf "%d", $1-$2}'),
    "l1_miss_pct": $(echo "$a_l1_pct $b_l1_pct" | awk '{printf "%.1f", $1-$2}'),
    "llc_miss": $(echo "$a_llc $b_llc" | awk '{printf "%d", $1-$2}'),
    "llc_miss_pct": $(echo "$a_llc_pct $b_llc_pct" | awk '{printf "%.1f", $1-$2}'),
    "branch_miss": $(echo "$a_br $b_br" | awk '{printf "%d", $1-$2}'),
    "branch_miss_pct": $(echo "$a_br_pct $b_br_pct" | awk '{printf "%.1f", $1-$2}')
  }
}
JSONEOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -m|--metric)    METRIC="$2"; shift 2 ;;
        -f|--format)    FORMAT="$2"; shift 2 ;;
        -h|--help)      usage ;;
        -*)             echo "未知选项: $1"; usage ;;
        *)
            if [ -z "$BEFORE_FILE" ]; then
                BEFORE_FILE="$1"
            elif [ -z "$AFTER_FILE" ]; then
                AFTER_FILE="$1"
            else
                echo "错误: 多余参数: $1" >&2; usage
            fi
            shift
            ;;
    esac
done

if [ -z "$BEFORE_FILE" ] || [ -z "$AFTER_FILE" ]; then
    echo "错误: 需要指定 before.data 和 after.data 两个文件" >&2
    usage
fi

for f in "$BEFORE_FILE" "$AFTER_FILE"; do
    if [ ! -f "$f" ]; then
        echo "错误: 文件不存在: $f" >&2
        exit 1
    fi
done

PARSE_SCRIPT="$SCRIPT_DIR/spe-parse.sh"
if [ ! -f "$PARSE_SCRIPT" ]; then
    echo "错误: 找不到 spe-parse.sh（预期在 $PARSE_SCRIPT）" >&2
    exit 1
fi

BEFORE_PARSED=$(bash "$PARSE_SCRIPT" --summary "$BEFORE_FILE")
AFTER_PARSED=$(bash "$PARSE_SCRIPT" --summary "$AFTER_FILE")

BEFORE_SUMMARY=$(extract_summary "$BEFORE_PARSED")
AFTER_SUMMARY=$(extract_summary "$AFTER_PARSED")

if [ "$FORMAT" = "json" ]; then
    output_json "$BEFORE_SUMMARY" "$AFTER_SUMMARY"
else
    compute_diff "$BEFORE_SUMMARY" "$AFTER_SUMMARY"
fi
