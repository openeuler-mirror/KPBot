#!/bin/bash
# =============================================================================
# kptools-proc-collector 测试套件
# 覆盖语法、discover、collect 各模式、check_balance、parse_maps、错误处理
# =============================================================================

set +e

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$TEST_DIR/../.." && pwd)"
SCRIPTS="$SKILL_DIR/scripts"
TEST_RESULTS_DIR="$TEST_DIR/test_results"
TEST_PASSED=0
TEST_FAILED=0
TEST_SKIPPED=0
BASH_BIN=$(command -v bash)

mkdir -p "$TEST_RESULTS_DIR"

red()    { echo -e "\033[31m$*\033[0m"; }
green()  { echo -e "\033[32m$*\033[0m"; }
yellow() { echo -e "\033[33m$*\033[0m"; }

assert_success() {
    local exit_code="$1" message="${2:-Command should succeed}"
    if [ "$exit_code" -eq 0 ]; then
        echo "  [PASS] $message"
        TEST_PASSED=$((TEST_PASSED + 1))
    else
        echo "  [FAIL] $message (exit code: $exit_code)"
        TEST_FAILED=$((TEST_FAILED + 1))
    fi
}

assert_failure() {
    local exit_code="$1" message="${2:-Command should fail}"
    if [ "$exit_code" -ne 0 ]; then
        echo "  [PASS] $message"
        TEST_PASSED=$((TEST_PASSED + 1))
    else
        echo "  [FAIL] $message (exit code: $exit_code)"
        TEST_FAILED=$((TEST_FAILED + 1))
    fi
}

assert_contains() {
    local haystack="$1" needle="$2" message="${3:-AssertContains}"
    if [[ "$haystack" == *"$needle"* ]]; then
        echo "  [PASS] $message"
        TEST_PASSED=$((TEST_PASSED + 1))
    else
        echo "  [FAIL] $message"
        echo "         未找到: $needle"
        TEST_FAILED=$((TEST_FAILED + 1))
    fi
}

skip() {
    echo "  [SKIP] $1"
    TEST_SKIPPED=$((TEST_SKIPPED + 1))
}

test_group_start() { echo -e "\n\033[34m===== 测试组: $1 =====\033[0m"; }
test_group_end() { :; }

# =============================================================================
# 1. 语法验证
# =============================================================================

test_group_start "1. 语法验证"

for script in discover.sh collect.sh check_balance.sh parse_maps.sh; do
    bash -n "$SCRIPTS/$script" 2>/dev/null
    assert_success $? "SYNTAX: $script 语法正确"
done

python3 -c "import json; json.load(open('$SKILL_DIR/evals/eval_queries.json'))" 2>/dev/null
assert_success $? "SYNTAX: eval_queries.json 合法 JSON"
python3 -c "import json; json.load(open('$SKILL_DIR/evals/evals.json'))" 2>/dev/null
assert_success $? "SYNTAX: evals.json 合法 JSON"

test_group_end

# =============================================================================
# 2. discover
# =============================================================================

test_group_start "2. discover 测试"

echo "  PC-D-001: --help"
output=$(bash "$SCRIPTS/discover.sh" --help 2>&1); rc=$?
assert_success $rc "PC-D-001: --help 退出码"

echo "  PC-D-002: 工具清单"
output=$(bash "$SCRIPTS/discover.sh" 2>&1); rc=$?
assert_success $rc "PC-D-002: 退出码 0"
assert_contains "$output" "ps: AVAILABLE" "PC-D-002: ps AVAILABLE"
for tool in pidstat perf taskset numastat readelf ldd; do
    if echo "$output" | grep -qE "^$tool: (AVAILABLE|MISSING)"; then
        assert_success 0 "PC-D-002: $tool 有状态行"
    else
        assert_success 1 "PC-D-002: $tool 缺少状态行"
    fi
done

test_group_end

# =============================================================================
# 3. collect 各模式（用 sleep 后台进程做目标）
# =============================================================================

sleep 30 &
TARGET_PID=$!
trap 'kill $TARGET_PID 2>/dev/null' EXIT

test_group_start "3. collect 模式测试（目标 PID=$TARGET_PID）"

echo "  PC-002: --help"
output=$(bash "$SCRIPTS/collect.sh" --help 2>&1); rc=$?
assert_success $rc "PC-002: --help 退出码"

echo "  PC-003: --threads"
output=$(bash "$SCRIPTS/collect.sh" -p "$TARGET_PID" --threads 2>&1); rc=$?
assert_success $rc "PC-003: 退出码"
assert_contains "$output" "pid,tid,psr,pcpu,stat,comm,wchan" "PC-003: 含表头"
assert_contains "$output" "sleep" "PC-003: 含目标线程"

echo "  PC-004: --cpu / --ctx"
output=$(bash "$SCRIPTS/collect.sh" -p "$TARGET_PID" --cpu -d 2 2>&1); rc=$?
if command -v pidstat &>/dev/null; then
    assert_success $rc "PC-004: --cpu 退出码"
    assert_contains "$output" "Average" "PC-004: 含 Average 段"
else
    assert_failure $rc "PC-004: pidstat 缺失时报错"
fi
if command -v pidstat &>/dev/null; then
    output=$(bash "$SCRIPTS/collect.sh" -p "$TARGET_PID" --ctx -d 2 2>&1)
    assert_contains "$output" "cswch" "PC-004: --ctx 含 cswch"
else
    skip "PC-004: --ctx（pidstat 缺失）"
fi

echo "  PC-005: --migration"
output=$(bash "$SCRIPTS/collect.sh" -p "$TARGET_PID" --migration -d 2 2>&1); rc=$?
if command -v perf &>/dev/null; then
    assert_success $rc "PC-005: 退出码"
    assert_contains "$output" "cpu-migrations" "PC-005: 含 cpu-migrations"
else
    assert_failure $rc "PC-005: perf 缺失时报错"
fi

echo "  PC-006: --affinity"
output=$(bash "$SCRIPTS/collect.sh" -p "$TARGET_PID" --affinity 2>&1); rc=$?
if command -v taskset &>/dev/null; then
    assert_success $rc "PC-006: 退出码"
    assert_contains "$output" "current affinity list" "PC-006: 含亲和性掩码"
else
    assert_failure $rc "PC-006: taskset 缺失时报错"
fi

echo "  PC-007: --maps + parse_maps"
output=$(bash "$SCRIPTS/collect.sh" -p "$TARGET_PID" --maps 2>&1); rc=$?
assert_success $rc "PC-007: --maps 退出码"
assert_contains "$output" "r-xp" "PC-007: 含映射权限段"
output=$(bash "$SCRIPTS/parse_maps.sh" -p "$TARGET_PID" 2>&1); rc=$?
assert_success $rc "PC-007: parse_maps 退出码"
echo "$output" | python3 -c "
import json,sys
d=json.load(sys.stdin)
assert isinstance(d,list) and len(d)>0
types={x['type'] for x in d}
assert 'libc' in types, f'libc not classified: {types}'
assert all('path' in x and 'size_kb' in x for x in d)
" 2>/dev/null
assert_success $? "PC-007: parse_maps 输出合法 JSON 且含 libc 分类"

echo "  PC-008: --numa / --cgroup / --binary / --env"
output=$(bash "$SCRIPTS/collect.sh" -p "$TARGET_PID" --numa 2>&1); rc=$?
assert_success $rc "PC-008: --numa 退出码"
output=$(bash "$SCRIPTS/collect.sh" -p "$TARGET_PID" --cgroup 2>&1); rc=$?
assert_success $rc "PC-008: --cgroup 退出码"
assert_contains "$output" "cgroup version" "PC-008: 报告 cgroup 版本"
output=$(bash "$SCRIPTS/collect.sh" -p "$TARGET_PID" --binary 2>&1); rc=$?
assert_success $rc "PC-008: --binary 退出码"
output=$(bash "$SCRIPTS/collect.sh" -p "$TARGET_PID" --env 2>&1); rc=$?
assert_success $rc "PC-008: --env 退出码"

echo "  PC-009: check_balance 单线程进程"
output=$(bash "$SCRIPTS/check_balance.sh" -p "$TARGET_PID" 2>&1); rc=$?
assert_success $rc "PC-009: 退出码"
assert_contains "$output" "single_cpu_or_single_thread" "PC-009: 单线程判 single_cpu_or_single_thread"

echo "  PC-009b: check_balance -f 已知分布（skewed 构造）"
cat > "$TEST_RESULTS_DIR/ps_skewed.txt" <<'EOF'
    PID TID PSR
   1000 1001   3
   1000 1002   3
   1000 1003   3
   1000 1004   5
EOF
output=$(bash "$SCRIPTS/check_balance.sh" -f "$TEST_RESULTS_DIR/ps_skewed.txt" 2>&1); rc=$?
assert_success $rc "PC-009b: 退出码"
assert_contains "$output" "skewed" "PC-009b: 3:3:3:1 分布判 skewed"
assert_contains "$output" '"3": 3' "PC-009b: thread_distribution 正确"

echo "  PC-009c: check_balance -f balanced 构造"
cat > "$TEST_RESULTS_DIR/ps_balanced.txt" <<'EOF'
    PID TID PSR
   1000 1001   3
   1000 1002   5
EOF
output=$(bash "$SCRIPTS/check_balance.sh" -f "$TEST_RESULTS_DIR/ps_balanced.txt" 2>&1)
assert_contains "$output" "balanced" "PC-009c: 1:1 分布判 balanced"

echo "  PC-010: 不存在的 PID"
output=$(bash "$SCRIPTS/collect.sh" -p 999999 --threads 2>&1); rc=$?
assert_failure $rc "PC-010: 退出码非 0"
assert_contains "$output" "does not exist" "PC-010: 报进程不存在"

echo "  PC-012: --all"
output=$(bash "$SCRIPTS/collect.sh" -p "$TARGET_PID" --all 2>&1); rc=$?
assert_success $rc "PC-012: --all 退出码（本机工具齐全）"

echo "  PC-013: --all 单模式失败不中断（不存在的 PID）"
# 每个模式都会在 check_pid 处 exit——修复前脚本在首个失败模式处整体终止
output=$(bash "$SCRIPTS/collect.sh" -p 999999999 --all -d 1 2>&1); rc=$?
assert_failure $rc "PC-013: 退出码非 0"
pmiss=""
for m in --threads --cpu --ctx --migration --affinity --maps --numa --cgroup --binary --env; do
    echo "$output" | grep -qF -- "========== $m" || pmiss="$pmiss $m"
done
if [ -z "$pmiss" ]; then
    assert_success 0 "PC-013: 失败后所有模式标记仍出现（错误隔离生效）"
else
    assert_success 1 "PC-013: 缺少模式标记:$pmiss"
fi

echo "  PC-008b: --cgroup 报告 PID 自身 cgroup 路径"
output=$(bash "$SCRIPTS/collect.sh" -p "$TARGET_PID" --cgroup 2>&1); rc=$?
assert_success $rc "PC-008b: 退出码"
if echo "$output" | grep -qE "cgroup path \(v2\)|cgroup version: v1"; then
    assert_success 0 "PC-008b: 含 PID cgroup 路径/版本行"
else
    assert_success 1 "PC-008b: 缺少 cgroup 路径行"
fi

test_group_end

# =============================================================================
# 4. 无模式等错误路径
# =============================================================================

test_group_start "4. 错误路径"

output=$(bash "$SCRIPTS/collect.sh" 2>&1); rc=$?
assert_failure $rc "无模式时报错"
output=$(bash "$SCRIPTS/collect.sh" -p "$TARGET_PID" --bogus 2>&1); rc=$?
assert_failure $rc "未知模式报错"
output=$(bash "$SCRIPTS/collect.sh" --threads 2>&1); rc=$?
assert_failure $rc "缺 -p 时报错"
output=$(bash "$SCRIPTS/collect.sh" -p "$TARGET_PID" --cpu -d abc 2>&1); rc=$?
assert_failure $rc "非数字 -d 报错"
output=$(bash "$SCRIPTS/collect.sh" -p 2>&1); rc=$?
assert_failure $rc "-p 缺值报错"
output=$(bash "$SCRIPTS/check_balance.sh" 2>&1); rc=$?
assert_failure $rc "check_balance 缺参数报错"
output=$(bash "$SCRIPTS/parse_maps.sh" 2>&1); rc=$?
assert_failure $rc "parse_maps 缺参数报错"

echo "  PC-014: check_balance -f 无表头单列存档"
printf '3\n3\n3\n5\n' > "$TEST_RESULTS_DIR/ps_nohdr1.txt"
output=$(bash "$SCRIPTS/check_balance.sh" -f "$TEST_RESULTS_DIR/ps_nohdr1.txt" 2>&1); rc=$?
assert_success $rc "PC-014: 退出码"
assert_contains "$output" '"3": 3' "PC-014: 无表头单列统计正确（首行不丢失）"

test_group_end

# =============================================================================
# 汇总
# =============================================================================

kill $TARGET_PID 2>/dev/null

echo ""
echo "============================================"
echo "  测试汇总"
echo "============================================"
green "  PASS: $TEST_PASSED"
red   "  FAIL: $TEST_FAILED"
yellow "  SKIP: $TEST_SKIPPED"
echo "  总计: $((TEST_PASSED + TEST_FAILED + TEST_SKIPPED))"
echo "============================================"

if [ "$TEST_FAILED" -gt 0 ]; then
    exit 1
fi
exit 0
