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
BY_FUNCTION=false

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    cat <<EOF
spe-compare.sh — ARM SPE 前后对比

用法:
  bash spe-compare.sh [选项] <before.data> <after.data>

选项:
  -m, --metric METRIC  对比指标: l1_miss, llc_miss, branch_mispred, all（默认 all）
  -f, --format FORMAT  输出格式: text, json（默认 text）
  --by-function        函数级对比（Rust mangled 符号按去 hash 规范化对齐）
  -h, --help           帮助

示例:
  bash spe-compare.sh before.data after.data
  bash spe-compare.sh -m l1_miss before.data after.data
  bash spe-compare.sh -f json before.data after.data
  bash spe-compare.sh --by-function before.data after.data
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

# ── 函数级对比（--by-function）────────────────────────────────────────────

# 从 spe-parse --by-function 全量 text 输出中抠函数表段并规范化：
# 剥离 Rust legacy mangled 符号末尾的 17h<hash>E 后缀（非 Rust 符号无副作用）
# 输出每行: FN count l1_pct llc_pct br_pct（空格分隔，FN 无空格——上游已下划线连接）
extract_function_table() {
    local parsed="$1"
    echo "$parsed" | awk '
    /^=== 按函数聚合/ { in_fn=1; next }
    in_fn && /^FUNCTION/ { next }
    in_fn && NF >= 5 {
        fn = $1
        for (i = 2; i <= NF-4; i++) fn = fn "_" $i
        # 剥离 Rust legacy mangled 符号的 17h<hash>E 后缀（hash 为小写 hex）
        sub(/17h[0-9a-f]+E$/, "", fn)
        cnt = $(NF-3) + 0
        l1  = $(NF-2); llc = $(NF-1); br = $NF
        gsub(/%/, "", l1); gsub(/%/, "", llc); gsub(/%/, "", br)
        printf "%s %d %.1f %.1f %.1f\n", fn, cnt, l1+0, llc+0, br+0
    }
    '
}

# 输出交集函数名的规范化列表（每行一个函数名）
function_intersection() {
    local before_tbl="$1" after_tbl="$2"
    comm -12 \
        <(echo "$before_tbl" | awk "{print \$1}" | sort -u) \
        <(echo "$after_tbl" | awk "{print \$1}" | sort -u)
}

# 函数级 text 对比表 + 单侧清单
compute_function_diff() {
    local before_tbl="$1"
    local after_tbl="$2"
    local common_fns="$3"

    echo ""
    echo "=== 函数级对比 ==="
    echo ""
    printf "  %-42s  %7s  %7s  %16s  %16s  %16s  %s\n" \
        "FUNCTION" "CNT_B" "CNT_A" "L1% B→A" "LLC% B→A" "BR% B→A" "判定"
    echo "  ------------------------------------------------------------------------------------------------------------------"

    local fn b_cnt a_cnt b_l1 a_l1 b_llc a_llc b_br a_br l1_diff verdict
    while IFS= read -r fn; do
        [ -z "$fn" ] && continue
        b_cnt=0 b_l1=0.0 b_llc=0.0 b_br=0.0
        a_cnt=0 a_l1=0.0 a_llc=0.0 a_br=0.0
        eval "$(echo "$before_tbl" | awk -v fn="$fn" '$1==fn {printf "b_cnt=%d b_l1=%.1f b_llc=%.1f b_br=%.1f", $2, $3, $4, $5; exit}')"
        eval "$(echo "$after_tbl"  | awk -v fn="$fn" '$1==fn {printf "a_cnt=%d a_l1=%.1f a_llc=%.1f a_br=%.1f", $2, $3, $4, $5; exit}')"

        l1_diff=$(echo "$a_l1 $b_l1" | awk '{printf "%.1f", $1-$2}')
        if [ "$l1_diff" != "0.0" ]; then
            if [ "$(echo "$l1_diff" | awk '{print ($1 < 0) ? 1 : 0}')" = "1" ]; then
                verdict="[IMPROVED]"
            else
                verdict="[REGRESSED]"
            fi
        else
            verdict="[NO CHANGE]"
        fi

        printf "  %-42s  %7s  %7s  %7s→%-7s  %7s→%-7s  %7s→%-7s  %s\n" \
            "$fn" "$b_cnt" "$a_cnt" "$b_l1" "$a_l1" "$b_llc" "$a_llc" "$b_br" "$a_br" "$verdict"
    done <<< "$common_fns"

    local only_before only_after
    only_before=$(comm -23 <(echo "$before_tbl" | awk '{print $1}' | sort -u) <(echo "$after_tbl" | awk '{print $1}' | sort -u))
    only_after=$(comm -13 <(echo "$before_tbl" | awk '{print $1}' | sort -u) <(echo "$after_tbl" | awk '{print $1}' | sort -u))

    if [ -n "$only_before" ]; then
        echo ""
        echo "--- 仅优化前 ---"
        echo "$only_before" | sed 's/^/  /'
    fi
    if [ -n "$only_after" ]; then
        echo ""
        echo "--- 仅优化后 ---"
        echo "$only_after" | sed 's/^/  /'
    fi
}

# 函数级 JSON 段（追加为 "functions"/"only_in_before"/"only_in_after" 键）
# 输入的 diff_json 为现有全局 JSON（以 "}" 结尾），本函数将其改写为带函数段的 JSON
append_function_json() {
    local before_tbl="$1"
    local after_tbl="$2"
    local common_fns="$3"
    local diff_json="$4"

    local fn_entries="" fn_entry
    local fn b_cnt a_cnt b_l1 a_l1 b_llc a_llc b_br a_br l1_diff verdict first=1
    while IFS= read -r fn; do
        [ -z "$fn" ] && continue
        b_cnt=0 b_l1=0.0 b_llc=0.0 b_br=0.0
        a_cnt=0 a_l1=0.0 a_llc=0.0 a_br=0.0
        eval "$(echo "$before_tbl" | awk -v fn="$fn" '$1==fn {printf "b_cnt=%d b_l1=%.1f b_llc=%.1f b_br=%.1f", $2, $3, $4, $5; exit}')"
        eval "$(echo "$after_tbl"  | awk -v fn="$fn" '$1==fn {printf "a_cnt=%d a_l1=%.1f a_llc=%.1f a_br=%.1f", $2, $3, $4, $5; exit}')"

        l1_diff=$(echo "$a_l1 $b_l1" | awk '{printf "%.1f", $1-$2}')
        if [ "$l1_diff" != "0.0" ]; then
            if [ "$(echo "$l1_diff" | awk '{print ($1 < 0) ? 1 : 0}')" = "1" ]; then
                verdict="improved"
            else
                verdict="regressed"
            fi
        else
            verdict="no_change"
        fi

        if [ "$first" = 1 ]; then first=0; else fn_entries="$fn_entries,"; fi
        fn_entries="$fn_entries
    {
      \"name\": \"$fn\",
      \"count_before\": $b_cnt, \"count_after\": $a_cnt,
      \"l1_miss_pct_before\": $b_l1, \"l1_miss_pct_after\": $a_l1,
      \"llc_miss_pct_before\": $b_llc, \"llc_miss_pct_after\": $a_llc,
      \"br_miss_pct_before\": $b_br, \"br_miss_pct_after\": $a_br,
      \"verdict\": \"$verdict\"
    }"
    done <<< "$common_fns"

    local only_b_json="" only_a_json="" item first_b=1 first_a=1
    while IFS= read -r item; do
        [ -z "$item" ] && continue
        if [ "$first_b" = 1 ]; then first_b=0; else only_b_json="$only_b_json,"; fi
        only_b_json="$only_b_json\"$item\""
    done <<< "$(comm -23 <(echo "$before_tbl" | awk '{print $1}' | sort -u) <(echo "$after_tbl" | awk '{print $1}' | sort -u))"
    while IFS= read -r item; do
        [ -z "$item" ] && continue
        if [ "$first_a" = 1 ]; then first_a=0; else only_a_json="$only_a_json,"; fi
        only_a_json="$only_a_json\"$item\""
    done <<< "$(comm -13 <(echo "$before_tbl" | awk '{print $1}' | sort -u) <(echo "$after_tbl" | awk '{print $1}' | sort -u))"

    # 去掉全局 JSON 末尾的 "}"，追加函数段后再闭合
    echo "$diff_json" | sed '$s/}$//' | sed '${/^$/d}'
    cat <<FJSONEOF
  ,
  "functions": [$fn_entries],
  "only_in_before": [$only_b_json],
  "only_in_after": [$only_a_json]
}
FJSONEOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -m|--metric)    METRIC="$2"; shift 2 ;;
        -f|--format)    FORMAT="$2"; shift 2 ;;
        --by-function)  BY_FUNCTION=true; shift ;;
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

FN_FALLBACK=false
if [ "$BY_FUNCTION" = true ]; then
    # 函数级对比：全量解析（不 --summary，函数表只在全量输出），提取+规范化+求交集
    BEFORE_FULL=$(bash "$PARSE_SCRIPT" --by-function "$BEFORE_FILE")
    AFTER_FULL=$(bash "$PARSE_SCRIPT" --by-function "$AFTER_FILE")
    BEFORE_FN_TBL=$(extract_function_table "$BEFORE_FULL")
    AFTER_FN_TBL=$(extract_function_table "$AFTER_FULL")
    COMMON_FNS=$(function_intersection "$BEFORE_FN_TBL" "$AFTER_FN_TBL")

    if [ -z "$COMMON_FNS" ]; then
        echo "未找到可对齐的函数级数据，回退全局汇总对比" >&2
        FN_FALLBACK=true
    fi
fi

BEFORE_PARSED=$(bash "$PARSE_SCRIPT" --summary "$BEFORE_FILE")
AFTER_PARSED=$(bash "$PARSE_SCRIPT" --summary "$AFTER_FILE")

BEFORE_SUMMARY=$(extract_summary "$BEFORE_PARSED")
AFTER_SUMMARY=$(extract_summary "$AFTER_PARSED")

if [ "$FORMAT" = "json" ]; then
    if [ "$BY_FUNCTION" = true ]; then
        append_function_json "$BEFORE_FN_TBL" "$AFTER_FN_TBL" "$COMMON_FNS" \
            "$(output_json "$BEFORE_SUMMARY" "$AFTER_SUMMARY")"
    else
        output_json "$BEFORE_SUMMARY" "$AFTER_SUMMARY"
    fi
else
    compute_diff "$BEFORE_SUMMARY" "$AFTER_SUMMARY"
    if [ "$BY_FUNCTION" = true ] && [ "$FN_FALLBACK" = false ]; then
        compute_function_diff "$BEFORE_FN_TBL" "$AFTER_FN_TBL" "$COMMON_FNS"
    fi
fi
