#!/usr/bin/env bash
# spe_aggr.sh — ARM SPE 采样事件聚合（arm_spe perf script 输出）
#
# 用法: bash scripts/spe_aggr.sh <perf-script 输出文件> [进程名|topN] [topN]
#   采集: perf record -e arm_spe/period=65536/ -- <bench> && perf script > spe.txt
#         （默认间隔 20s 产 14GB 数据 —— 必须设 period）
#   第二参数为纯数字时视为 topN（如 "spe_aggr.sh spe.txt 15"），
#   否则视为进程名（如 "spe_aggr.sh spe.txt mybench 20"）；
#   进程名缺省时自动探测（取样本最多的 comm）
#
# 输出:
#   1. 事件类型全集统计（tlb-access/memory/l1d-access/l1d-miss/llc-access/
#      llc-miss/branch-miss/tlb-miss，perf 6.6 事件名，与 arm-spe-analysis
#      的 canonical token 一致）
#   2. l1d-miss / llc-miss / tlb-miss / branch-miss 按函数聚合 TopN（用户态，
#      过滤 [kernel.kallsyms]）
#   3. 全事件类型下样本最多的用户态函数 TopN
#
# 判读纪律（与 SKILL.md 步骤 6 / arm-spe-analysis 的分级体系一致）:
#   - 阈值分级用 arm-spe-analysis 的 canonical：l1_miss >30%/10%/5% =>
#     SEVERE/MODERATE/MILD；branch_mispred >20%/5%/1%；tlb_miss 与 llc_miss
#     合并分类 —— 本脚本只做聚合，分级判定交给 spe-hotspot.sh / 人工按该表
#   - SPE 样本按操作量加权 != 时间占比（逐字节循环样本虚高数十倍），
#     时间分布必须用 cycles 采样交叉验证
#
# 退出码: 0

set -u
SF="${1:-}"; PROC=""; TOPN="${3:-15}"
# 参数智能解析：$2 为纯数字时视为 topN（"spe_aggr.sh spe.txt 15" 合法），
# 否则视为进程名；$3 始终是 topN。进程名缺省时自动探测（样本最多的 comm）。
if [ -n "${2:-}" ]; then
  case "$2" in
    ''|*[!0-9]*) PROC="$2" ;;
    *) TOPN="$2" ;;
  esac
fi
[ -z "$SF" ] || [ ! -f "$SF" ] && { echo "用法: spe_aggr.sh <perf-script 输出> [进程名|topN] [topN]" >&2; exit 2; }
[ -z "$PROC" ] && PROC=$(awk '{print $1}' "$SF" | sort | uniq -c | sort -rn | head -1 | awk '{print $2}')
echo "进程: $PROC  输入: $SF  topN: $TOPN"
echo ""
echo "=== 事件类型全集 ==="
grep -E "^\s*${PROC}" "$SF" | awk '{for(i=1;i<=NF;i++) if($i ~ /-/) {print $i; break}}' | tr -d ':' | sort | uniq -c | sort -rn
for ev in l1d-miss llc-miss tlb-miss branch-miss; do
  echo ""
  echo "=== ${ev} Top${TOPN}（函数聚合，用户态）==="
  grep -E "^\s*${PROC}" "$SF" | grep -v kernel.kallsyms | grep -F "$ev" | \
    awk -v e="$ev:" '{for(i=1;i<=NF;i++) if($i==e) {fn=$(i+2); print fn; break}}' | \
    sort | uniq -c | sort -rn | head -"$TOPN"
done
echo ""
echo "=== 全事件类型下样本最多的用户态函数 Top${TOPN} ==="
grep -E "^\s*${PROC}" "$SF" | grep -v kernel.kallsyms | \
  awk '{for(i=1;i<=NF;i++) if($i ~ /-/) {fn=$(i+2); print fn; break}}' | \
  sort | uniq -c | sort -rn | head -"$TOPN"
exit 0
