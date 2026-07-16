#!/usr/bin/env bash
set -euo pipefail

SUMMARY_JSON="${1:-./output/cpu-affinity/diagnosis/cpu_affinity_summary.json}"
OUTPUT_DIR="${2:-./output/cpu-affinity/final}"

mkdir -p "${OUTPUT_DIR}"
OUT_MD="${OUTPUT_DIR}/affinity_strategy.md"
OUT_JSON="${OUTPUT_DIR}/affinity_strategy.json"

BALANCE_STATUS="$(awk -F'"' '/"balance_status"/ {print $4; exit}' "${SUMMARY_JSON}" 2>/dev/null || true)"
MIGRATION_LEVEL="$(awk -F'"' '/"migration_level"/ {print $4; exit}' "${SUMMARY_JSON}" 2>/dev/null || true)"

STRATEGY="preserve_current_layout_with_validation"
RATIONALE="No strong affinity issue was detected from available evidence."
NEXT_ROUND="observe_after_validation"

if [[ "${BALANCE_STATUS}" == "skewed" ]]; then
  STRATEGY="rebalance_hot_threads_and_limit_cross_cpu_spread"
  RATIONALE="Thread distribution is skewed and hot threads are concentrated on a subset of CPUs."
  NEXT_ROUND="validate_thread_distribution_after_rebalance"
fi

if [[ "${MIGRATION_LEVEL}" == "high" ]]; then
  STRATEGY="reduce_thread_migration_with_tighter_affinity"
  RATIONALE="High migration frequency suggests scheduler churn or weak CPU affinity."
  NEXT_ROUND="verify_migration_reduction"
fi

cat > "${OUT_MD}" <<EOF
# CPU 亲和性策略建议

- 选中策略: ${STRATEGY}
- 原因: ${RATIONALE}
- 下一轮建议: ${NEXT_ROUND}
- 验证要点:
  - 检查热点 CPU 是否收敛
  - 检查线程分布是否更均衡
  - 检查线程迁移是否下降
  - 检查 IRQ 与业务线程是否仍然冲突
EOF

cat > "${OUT_JSON}" <<EOF
{
  "binding_strategy_candidates": [
    "preserve_current_layout_with_validation",
    "rebalance_hot_threads_and_limit_cross_cpu_spread",
    "reduce_thread_migration_with_tighter_affinity"
  ],
  "selected_binding_strategy": "${STRATEGY}",
  "binding_validation_plan": [
    "check_hot_cpu_list",
    "check_thread_distribution",
    "check_thread_migration",
    "check_irq_cpu_conflict"
  ],
  "next_round_candidate": "${NEXT_ROUND}"
}
EOF

cat "${OUT_JSON}"
