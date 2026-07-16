#!/bin/bash
# =============================================================================
# ARM SPE 数据解析脚本
# arm-spe-analysis skill - scripts/spe-parse.sh
# 用途：解析 perf.data 中的 SPE 记录，输出结构化信息
# 用法：
#   bash spe-parse.sh perf.data                       # 默认解析输出
#   bash spe-parse.sh -f json -n 10 perf.data         # JSON 格式 Top 10
#   bash spe-parse.sh --summary perf.data              # 仅汇总统计
# =============================================================================

set -euo pipefail

# ── 全局变量 ──
FORMAT="text"
SORT_FIELD="sample_count"
TOP_N=20
SUMMARY_ONLY=false
DATA_FILE=""

usage() {
    cat <<EOF
spe-parse.sh — ARM SPE 数据解析

用法:
  bash spe-parse.sh [选项] <perf.data>

选项:
  -f, --format FORMAT  输出格式: text, json（默认 text）
  -s, --sort FIELD     排序字段: sample_count, l1_miss, llc_miss, br_miss（默认 sample_count）
  -n, --top N          仅输出 Top N 条记录（默认 20）
  --summary            仅输出汇总统计，不列出每条记录
  -h, --help           帮助

示例:
  bash spe-parse.sh spe-20260425.data
  bash spe-parse.sh -f json -n 10 spe-20260425.data
  bash spe-parse.sh --summary spe-20260425.data
EOF
    exit 0
}

parse_spe_records() {
    local data_file="$1"

    if [ ! -f "$data_file" ]; then
        echo "错误: 文件不存在: $data_file" >&2
        exit 1
    fi

    if ! command -v perf &>/dev/null; then
        echo "错误: perf 命令不可用" >&2
        exit 1
    fi

    # SPE 输出格式: COMM PID [CPU] TIMESTAMP: PERIOD EVENT_TYPE: IP SYMBOL (DSO)
    # 每行是一个独立的 SPE 事件记录
    perf script -i "$data_file" 2>/dev/null
}

extract_fields() {
    awk '
    BEGIN {
        total=0
        l1_miss=0; llc_miss=0; remote_access=0
        tlb_count=0; tlb_miss_count=0
        branch_count=0; branch_miss_count=0
    }
    {
        total++

        # 解析列:
        # $1=COMM, $2=PID, $3=[CPU], $4=TIMESTAMP:, $5=PERIOD, $6=EVENT_TYPE:
        # $7=IP, $8=SYMBOL, $9=(DSO)

        event = $6
        gsub(/:$/, "", event)   # 去掉末尾的冒号
        addr = $7
        sym = $8

        # 清理 symbol: 移除 +offset 和 DSO 引用部分
        gsub(/\+0x[0-9a-f]+$/, "", sym)
        gsub(/\(.*\)$/, "", sym)
        gsub(/^\(/, "", sym)
        gsub(/\)$/, "", sym)

        # 统计各类事件
        if (event == "l1d-miss")       l1_miss++
        else if (event == "llc-miss")  llc_miss++
        else if (event == "remote-access") remote_access++

        if (event == "tlb-access") { tlb_count++ }
        else if (event == "tlb-miss") { tlb_miss_count++; tlb_count++ }

        if (event == "branch-miss") { branch_miss_count++; branch_count++ }

        if (addr ~ /^[0-9a-f]+$/) {
            key = addr
            if (!(key in agg_count)) {
                agg_count[key] = 0
                agg_sym[key] = sym
                agg_l1_miss[key] = 0
                agg_llc_miss[key] = 0
                agg_branch_miss[key] = 0
            }
            agg_count[key]++
            if (event == "l1d-miss")       agg_l1_miss[key]++
            else if (event == "llc-miss")  agg_llc_miss[key]++
            else if (event == "remote-access") agg_llc_miss[key]++
            else if (event == "branch-miss") agg_branch_miss[key]++
        }
    }
    END {
        printf "=== SPE 采样汇总 ===\n"
        printf "总记录数:      %d\n", total
        if (total > 0) {
            printf "\n--- Cache 分布 ---\n"
            printf "L1 miss:   %d (%.1f%%)\n", l1_miss, l1_miss*100/total
            printf "LLC miss:  %d (%.1f%%)\n", llc_miss, llc_miss*100/total
            printf "Remote:    %d (%.1f%%)\n", remote_access, remote_access*100/total
            printf "\n--- TLB 分布 ---\n"
            printf "TLB access: %d (%.1f%%)\n", tlb_count, tlb_count*100/total
            if (tlb_count > 0)
                printf "TLB miss:   %d (%.1f%% of TLB)\n", tlb_miss_count, tlb_miss_count*100/tlb_count
            printf "\n--- 分支预测 ---\n"
            if (branch_count > 0)
                printf "Branch miss: %d (%.1f%% of branches)\n", branch_miss_count, branch_miss_count*100/(branch_count > 0 ? branch_count : 1)
        }

        printf "\n=== 按指令聚合 ===\n"
        printf "%-18s  %-6s  %10s  %10s  %10s  %s\n", "ADDRESS", "COUNT", "L1_MISS%", "LLC_MISS%", "BR_MISS%", "SYMBOL"
        n = asorti(agg_count, sorted_keys, "@val_num_desc")
        for (i = 1; i <= n; i++) {
            key = sorted_keys[i]
            cnt = agg_count[key]
            l1m = (cnt > 0) ? agg_l1_miss[key]*100/cnt : 0
            llcm = (cnt > 0) ? agg_llc_miss[key]*100/cnt : 0
            brm = (cnt > 0) ? agg_branch_miss[key]*100/cnt : 0
            printf "%-18s  %-6d  %9.1f%%  %9.1f%%  %9.1f%%  %s\n", key, cnt, l1m, llcm, brm, agg_sym[key]
        }
    }
    '
}

sort_and_limit() {
    local parsed="$1"
    local sort_field="$2"
    local top_n="$3"

    {
    echo "$parsed" | awk -v sort_field="$sort_field" -v top_n="$top_n" '
    /^=== 按指令聚合/ { in_agg=1; next }
    in_agg && /^ADDRESS/ { header=$0; next }
    in_agg && /^[0-9a-fx]/ {
        addr=$1; count=$2; l1_miss=$3; llc_miss=$4; br_miss=$5; sym=$6
        gsub(/%/, "", l1_miss)
        gsub(/%/, "", llc_miss)
        gsub(/%/, "", br_miss)
        if (sort_field == "sample_count") key = count+0
        else if (sort_field == "l1_miss") key = l1_miss+0
        else if (sort_field == "llc_miss") key = llc_miss+0
        else if (sort_field == "br_miss") key = br_miss+0
        else key = count+0

        printf "%15.2f  %s  %s  %s  %s  %s  %s\n", key, addr, count, l1_miss, llc_miss, br_miss, sym
    }
    ' | sort -rn | head -n "$top_n" | awk '
    BEGIN {
        printf "%-18s  %-6s  %10s  %10s  %10s  %s\n", "ADDRESS", "COUNT", "L1_MISS%", "LLC_MISS%", "BR_MISS%", "SYMBOL"
    }
    {
        printf "%-18s  %-6s  %9s%%  %9s%%  %9s%%  %s\n", $2, $3, $4, $5, $6, $7
    }
    '
    } || true
}

output_json() {
    local parsed="$1"
    echo "$parsed" | awk '
    BEGIN { printf "{" }
    /^总记录数:/ { printf "\"total_records\": %s", $2 }
    /^L1 miss:/ { printf ", \"l1_miss\": %s", $3 }
    /^LLC miss:/ { printf ", \"llc_miss\": %s", $3 }
    /^TLB access:/ { printf ", \"tlb_access\": %s", $3 }
    /^Branch miss:/ { printf ", \"branch_miss\": %s", $3 }
    END { printf " }\n" }
    '
}

# 解析参数
while [[ $# -gt 0 ]]; do
    case "$1" in
        -f|--format)    FORMAT="$2"; shift 2 ;;
        -s|--sort)      SORT_FIELD="$2"; shift 2 ;;
        -n|--top)       TOP_N="$2"; shift 2 ;;
        --summary)      SUMMARY_ONLY=true; shift ;;
        -h|--help)      usage ;;
        -*)             echo "未知选项: $1"; usage ;;
        *)              DATA_FILE="$1"; shift ;;
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

RAW=$(parse_spe_records "$DATA_FILE")
PARSED=$(echo "$RAW" | extract_fields)

if [ "$SUMMARY_ONLY" = true ]; then
    echo "$PARSED" | sed '/^=== 按指令聚合/,$d'
else
    if [ "$FORMAT" = "json" ]; then
        output_json "$PARSED"
    else
        echo "$PARSED" | sed '/^=== 按指令聚合/,$d'
        echo ""
        echo "=== Top $TOP_N (按 $SORT_FIELD 排序) ==="
        sort_and_limit "$PARSED" "$SORT_FIELD" "$TOP_N"
    fi
fi
