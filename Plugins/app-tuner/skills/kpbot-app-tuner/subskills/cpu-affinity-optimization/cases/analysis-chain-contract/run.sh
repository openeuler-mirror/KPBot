#!/bin/bash
# test_analysis_chain.sh — cpu-affinity-optimization 分析链 UT（skill-ut-supplement）
# 对象：aggregate_diagnosis.sh + generate_affinity_strategy.sh（R3 schema 消费点）
# fixture：subskill-ab-comparison 真实 A/B 数据（pretty=旧链 heredoc 格式 / compact=json.dump 单行格式）
# 出处：journal skill-testing/collection-replacement/subskill-ab-comparison/cpu-affinity/（2026-11-02）
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../scripts" && pwd)"   # → scripts/
CASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIX="$CASE_DIR/fixtures"
SKILL_MD="$CASE_DIR/../../SKILL.md"                                       # → subskill 根
WORK="$(mktemp -d /tmp/ca-ut.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

TEST_PASSED=0; TEST_FAILED=0; TEST_SKIPPED=0
green()  { echo -e "\033[32m$*\033[0m"; }
red()    { echo -e "\033[31m$*\033[0m"; }
yellow() { echo -e "\033[33m$*\033[0m"; }
assert_success() { if [ "$1" -eq 0 ]; then green "  [PASS] $2"; TEST_PASSED=$((TEST_PASSED+1)); else red "  [FAIL] $2 (rc=$1)"; TEST_FAILED=$((TEST_FAILED+1)); fi; }
assert_contains(){ if echo "$1" | grep -qF -- "$2"; then green "  [PASS] $3"; TEST_PASSED=$((TEST_PASSED+1)); else red "  [FAIL] $3 (未找到: $2)"; TEST_FAILED=$((TEST_FAILED+1)); fi; }
assert_eq() { if [ "$1" = "$2" ]; then green "  [PASS] $3"; TEST_PASSED=$((TEST_PASSED+1)); else red "  [FAIL] $3 (实际: $1)"; TEST_FAILED=$((TEST_FAILED+1)); fi; }
skip() { yellow "  [SKIP] $1"; TEST_SKIPPED=$((TEST_SKIPPED+1)); }

# 跑完整链：$1=diagnosis 源目录（含 4 个 JSON），$2=输出根；stdout=selected 策略
run_chain() {
    local src="$1" out="$2"
    mkdir -p "$out/diagnosis"
    cp "$src"/*.json "$out/diagnosis/" 2>/dev/null || true
    bash "$SCRIPT_DIR/aggregate_diagnosis.sh" "$out" >/dev/null 2>&1
    bash "$SCRIPT_DIR/generate_affinity_strategy.sh" "$out/diagnosis/cpu_affinity_summary.json" "$out/final" >/dev/null 2>&1
    python3 -c "import json;print(json.load(open('$out/final/affinity_strategy.json'))['selected_binding_strategy'])" 2>/dev/null
}

echo "===== cpu-affinity 分析链 UT ====="

echo "CA-01: pretty 格式（旧链 heredoc 真实数据，skewed/low）→ rebalance 策略"
S=$(run_chain "$FIX/pretty" "$WORK/ca01"); RC=$?
assert_success $RC "CA-01: 链执行 rc=0"
assert_eq "$S" "rebalance_hot_threads_and_limit_cross_cpu_spread" "CA-01: selected=rebalance（skewed 驱动）"

echo "CA-02: compact 格式（SKILL 路径 json.dump 单行真实数据）→ 同 golden（fcd0782 格式无关提取回归）"
if grep -q "extract_json_string_field" "$SCRIPT_DIR/generate_affinity_strategy.sh" 2>/dev/null; then
    S=$(run_chain "$FIX/compact" "$WORK/ca02")
    assert_eq "$S" "rebalance_hot_threads_and_limit_cross_cpu_spread" "CA-02: compact 与 pretty 同判 rebalance"
else
    skip "CA-02: generate 为行式提取（compact JSON 会静默误判——fcd0782 合入后激活本用例）"
fi

echo "CA-03: migration_level 三区间（balance=balanced 隔离迁移驱动）"
for lvl in low medium high; do
    mkdir -p "$WORK/ca03-$lvl/src"
    cp "$FIX/pretty"/*.json "$WORK/ca03-$lvl/src/"
    python3 - "$WORK/ca03-$lvl/src" "$lvl" <<'PYEOF'
import json, sys
d, lvl = sys.argv[1], sys.argv[2]
p = f"{d}/thread_migration.json"
j = json.load(open(p)); j["migration_level"] = lvl
json.dump(j, open(p, "w"), indent=2)
p2 = f"{d}/thread_distribution.json"
j2 = json.load(open(p2)); j2["balance_status"] = "balanced"
json.dump(j2, open(p2, "w"), indent=2)
PYEOF
    S=$(run_chain "$WORK/ca03-$lvl/src" "$WORK/ca03-$lvl/out")
    case "$lvl" in
        high)
            assert_eq "$S" "reduce_thread_migration_with_tighter_affinity" "CA-03: high → reduce_thread_migration"
            NR=$(python3 -c "import json;print(json.load(open('$WORK/ca03-$lvl/out/final/affinity_strategy.json'))['next_round_candidate'])")
            assert_eq "$NR" "verify_migration_reduction" "CA-03: reduce 的 next_round=verify_migration_reduction"
            ;;
        medium) assert_eq "$S" "preserve_current_layout_with_validation" "CA-03: medium → preserve（generate 仅认 high）" ;;
        low)    assert_eq "$S" "preserve_current_layout_with_validation" "CA-03: low → preserve" ;;
    esac
done

echo "CA-08: 优先级——skewed + high 并存 → high 覆盖（reduce 优先于 rebalance）"
mkdir -p "$WORK/ca08/src"
cp "$FIX/pretty"/*.json "$WORK/ca08/src/"
python3 - "$WORK/ca08/src" <<'PYEOF'
import json, sys
p = f"{sys.argv[1]}/thread_migration.json"
j = json.load(open(p)); j["migration_level"] = "high"
json.dump(j, open(p, "w"), indent=2)
# distribution 保持 skewed（fixture 原值）
PYEOF
S=$(run_chain "$WORK/ca08/src" "$WORK/ca08/out")
assert_eq "$S" "reduce_thread_migration_with_tighter_affinity" "CA-08: skewed+high → reduce（后判断覆盖）"

echo "CA-04: 缺 thread_migration.json → 默认值注入 → preserve（balance 置 balanced 隔离）"
mkdir -p "$WORK/ca04/src"
cp "$FIX/pretty"/thread_affinity.json "$FIX/pretty"/thread_distribution.json "$FIX/pretty"/irq_affinity.json "$WORK/ca04/src/"
python3 - "$WORK/ca04/src" <<'PYEOF'
import json, sys
p = f"{sys.argv[1]}/thread_distribution.json"
j = json.load(open(p)); j["balance_status"] = "balanced"
json.dump(j, open(p, "w"), indent=2)
PYEOF
S=$(run_chain "$WORK/ca04/src" "$WORK/ca04/out")
assert_eq "$S" "preserve_current_layout_with_validation" "CA-04: 缺文件降级 → preserve"

echo "CA-05: diagnosis 全缺 → 全默认注入 → preserve + 链路不崩"
mkdir -p "$WORK/ca05/src"
S=$(run_chain "$WORK/ca05/src" "$WORK/ca05/out"); RC=$?
assert_success $RC "CA-05: 链执行 rc=0"
assert_eq "$S" "preserve_current_layout_with_validation" "CA-05: 全默认 → preserve"

echo "CA-06: 指引契约锁定——SKILL.md 落盘口径含三区间阈值"
if grep -q ">100 → high" "$SKILL_MD" 2>/dev/null; then
    G=$(grep -o ">100 → high，>10 → medium，≤10 → low" "$SKILL_MD" | head -1)
    assert_contains "$G" ">100 → high" "CA-06: migration_level 阈值契约文本在 SKILL.md"
    assert_contains "$G" "≤10 → low" "CA-06: 阈值含 low 边界"
else
    skip "CA-06: SKILL.md 无落盘口径阈值契约（指引补全合入后激活）"
fi

echo "CA-07: summary 文件不存在 → 优雅降级 preserve"
bash "$SCRIPT_DIR/generate_affinity_strategy.sh" "$WORK/not_exist.json" "$WORK/ca07-out" >/dev/null 2>&1
S=$(python3 -c "import json;print(json.load(open('$WORK/ca07-out/affinity_strategy.json'))['selected_binding_strategy'])" 2>/dev/null)
assert_eq "$S" "preserve_current_layout_with_validation" "CA-07: 缺 summary → preserve"

echo ""
echo "============================================"
echo "  测试汇总"
echo "============================================"
green "  PASS: $TEST_PASSED"
red   "  FAIL: $TEST_FAILED"
yellow "  SKIP: $TEST_SKIPPED"
echo "  总计: $((TEST_PASSED + TEST_FAILED + TEST_SKIPPED))"
echo "============================================"

# case 契约：case.yaml 的 json_field 断言消费 results/result.json
mkdir -p "$CASE_DIR/results"
python3 - "$CASE_DIR/results/result.json" "$TEST_PASSED" "$TEST_FAILED" "$TEST_SKIPPED" <<'RESULTPY'
import json, sys
p, passed, failed, skipped = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
json.dump({"success": failed == 0, "passed": passed, "checks_ran": passed + failed,
           "checks_failures": failed, "skipped": skipped},
          open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
sys.exit(0 if failed == 0 else 1)
RESULTPY
exit $?
