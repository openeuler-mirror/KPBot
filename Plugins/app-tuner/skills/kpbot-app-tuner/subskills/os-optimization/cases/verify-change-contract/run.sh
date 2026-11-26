#!/bin/bash
# run.sh — tier A：verify_os_change.sh 契约用例（os-optimization）
# 覆盖：4 种基准类型的指标提取、三态阈值决策（verified_effective/below_threshold/harmful）、
#       回滚执行、baseline-only、提取失败路径、verification_result.json 契约
# 运行（技能根目录）: bash cases/verify-change-contract/run.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../scripts" && pwd)"
CASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d /tmp/vo-ut.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

TEST_PASSED=0; TEST_FAILED=0; TEST_SKIPPED=0
green()  { echo -e "\033[32m$*\033[0m"; }
red()    { echo -e "\033[31m$*\033[0m"; }
yellow() { echo -e "\033[33m$*\033[0m"; }
assert_success() { if [ "$1" -eq 0 ]; then green "  [PASS] $2"; TEST_PASSED=$((TEST_PASSED+1)); else red "  [FAIL] $2 (rc=$1)"; TEST_FAILED=$((TEST_FAILED+1)); fi; }
assert_fail()    { if [ "$1" -ne 0 ]; then green "  [PASS] $2"; TEST_PASSED=$((TEST_PASSED+1)); else red "  [FAIL] $2 (期望非零)"; TEST_FAILED=$((TEST_FAILED+1)); fi; }
assert_contains(){ if echo "$1" | grep -qF -- "$2"; then green "  [PASS] $3"; TEST_PASSED=$((TEST_PASSED+1)); else red "  [FAIL] $3 (未找到: $2)"; TEST_FAILED=$((TEST_FAILED+1)); fi; }
assert_eq() { if [ "$1" = "$2" ]; then green "  [PASS] $3"; TEST_PASSED=$((TEST_PASSED+1)); else red "  [FAIL] $3 (实际: $1)"; TEST_FAILED=$((TEST_FAILED+1)); fi; }
skip() { yellow "  [SKIP] $1"; TEST_SKIPPED=$((TEST_SKIPPED+1)); }

VO() { bash "$SCRIPT_DIR/verify_os_change.sh" "$@" 2>/dev/null; }

echo "===== verify_os_change 契约用例 ====="

echo "VO-01: sysbench tps 增益超阈值 → verified_effective / KEEP"
OUT=$(VO --benchmark-cmd "echo 'tps: 100'" --changed-benchmark-cmd "echo 'tps: 110'" \
        --benchmark-type sysbench --metric tps --threshold 0.3 \
        --output-dir "$WORK/vo01" --action sysctl --key vm.dirty_ratio --old-value 10 --new-value 5)
assert_success $? "VO-01: rc=0"
assert_contains "$OUT" "Decision: ✅ VERIFIED EFFECTIVE" "VO-01: 决策=verified_effective"
assert_contains "$OUT" "Action: KEEP" "VO-01: 动作=KEEP"
assert_eq "$(python3 -c "import json;print(json.load(open('$WORK/vo01/verification_result.json'))['gain_pct'])")" "10.0" "VO-01: gain_pct=10.0"
assert_eq "$(python3 -c "import json;print(json.load(open('$WORK/vo01/verification_result.json'))['verified'])")" "True" "VO-01: verified=true"

echo "VO-02: 增益低于阈值 → below_threshold / ASK USER"
OUT=$(VO --benchmark-cmd "echo 'tps: 100'" --changed-benchmark-cmd "echo 'tps: 100.1'" \
        --benchmark-type sysbench --metric tps --threshold 0.3 --output-dir "$WORK/vo02")
assert_contains "$OUT" "Decision: ⚠️  BELOW THRESHOLD" "VO-02: 决策=below_threshold"
assert_contains "$OUT" "Action: ASK USER" "VO-02: 动作=ASK USER（仅提示，不阻塞交互）"

echo "VO-03: 负增益 → harmful / AUTO ROLLBACK 且执行回滚命令"
OUT=$(VO --benchmark-cmd "echo 'tps: 100'" --changed-benchmark-cmd "echo 'tps: 90'" \
        --benchmark-type sysbench --metric tps --threshold 0.3 \
        --output-dir "$WORK/vo03" --rollback-cmd "touch $WORK/vo03/rollback_marker")
assert_contains "$OUT" "Decision: ❌ HARMFUL" "VO-03: 决策=harmful"
assert_contains "$OUT" "Action: AUTO ROLLBACK" "VO-03: 动作=AUTO ROLLBACK"
assert_success $([ -f "$WORK/vo03/rollback_marker" ] && echo 0 || echo 1) "VO-03: 回滚命令被执行（marker 存在）"

echo "VO-04: baseline-only 模式 → 基线后即退出"
OUT=$(VO --benchmark-cmd "echo 'tps: 100'" --baseline-only --output-dir "$WORK/vo04")
assert_success $? "VO-04: rc=0"
assert_contains "$OUT" "=== Baseline only ===" "VO-04: baseline-only 输出"
assert_fail $([ -f "$WORK/vo04/verification_result.json" ] && echo 0 || echo 1) "VO-04: 不产生决策 JSON"

echo "VO-05: 指标提取——pgbench / redis-benchmark / custom 三类"
OUT=$(VO --benchmark-cmd "echo 'tps = 123.45'" --changed-benchmark-cmd "echo 'tps = 123.45'" \
        --benchmark-type pgbench --metric tps --output-dir "$WORK/vo05a")
assert_contains "$OUT" "tps=123.45" "VO-05: pgbench 'tps = ' 提取"
OUT=$(VO --benchmark-cmd "echo '9876.54 requests per second'" --changed-benchmark-cmd "echo '9876.54 requests per second'" \
        --benchmark-type redis-benchmark --metric tps --output-dir "$WORK/vo05b")
assert_contains "$OUT" "tps=9876.54" "VO-05: redis requests-per-second 提取"
OUT=$(VO --benchmark-cmd "echo 'RESULT: tps=55.5'" --changed-benchmark-cmd "echo 'RESULT: tps=55.5'" \
        --benchmark-type custom --metric-regex 'RESULT: tps=(\d+\.?\d*)' --metric-name tps --output-dir "$WORK/vo05c")
assert_contains "$OUT" "tps=55.5" "VO-05: custom 正则提取"

echo "VO-06: 指标提取失败 → 基线阶段失败退出"
VO --benchmark-cmd "echo 'no metric here'" --benchmark-type sysbench --metric tps --output-dir "$WORK/vo06" >/dev/null 2>&1
assert_fail $? "VO-06: 无法提取 tps → exit 1"

echo "VO-07: 缺必填参数"
VO --metric tps >/dev/null 2>&1
assert_fail $? "VO-07: 缺 --benchmark-cmd → exit 1"

echo ""
echo "============================================"
echo "  测试汇总"
echo "============================================"
green "  PASS: $TEST_PASSED"
red   "  FAIL: $TEST_FAILED"
yellow "  SKIP: $TEST_SKIPPED"
echo "  总计: $((TEST_PASSED + TEST_FAILED + TEST_SKIPPED))"
echo "============================================"

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
