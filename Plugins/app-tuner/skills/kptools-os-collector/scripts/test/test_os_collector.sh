#!/bin/bash
# =============================================================================
# os-collector 测试套件
# 覆盖语法验证、discover、各采集模式、--all、SKILL.md/evals 格式
# 对应设计文档：os-collector-skill-design.md §7（OS-001 ~ OS-011）
# =============================================================================
set +e

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$TEST_DIR/../.." && pwd)"
SCRIPTS="$SKILL_DIR/scripts"
TEST_RESULTS_DIR="$TEST_DIR/test_results"
TEST_PASSED=0
TEST_FAILED=0
TEST_SKIPPED=0

mkdir -p "$TEST_RESULTS_DIR"
BASH_BIN=$(command -v bash)

red()    { echo -e "\033[31m$*\033[0m"; }
green()  { echo -e "\033[32m$*\033[0m"; }
yellow() { echo -e "\033[33m$*\033[0m"; }

assert_equal() {
    local expected="$1" actual="$2" message="${3:-AssertEqual}"
    if [ "$expected" = "$actual" ]; then
        echo "  [PASS] $message"; TEST_PASSED=$((TEST_PASSED + 1))
    else
        echo "  [FAIL] $message"; echo "         期望: $expected"; echo "         实际: $actual"; TEST_FAILED=$((TEST_FAILED + 1))
    fi
}

assert_success() {
    local exit_code="$1" message="${2:-Command should succeed}"
    if [ "$exit_code" -eq 0 ]; then
        echo "  [PASS] $message"; TEST_PASSED=$((TEST_PASSED + 1))
    else
        echo "  [FAIL] $message (exit code: $exit_code)"; TEST_FAILED=$((TEST_FAILED + 1))
    fi
}

assert_failure() {
    local exit_code="$1" message="${2:-Command should fail}"
    if [ "$exit_code" -ne 0 ]; then
        echo "  [PASS] $message"; TEST_PASSED=$((TEST_PASSED + 1))
    else
        echo "  [FAIL] $message (exit code: $exit_code)"; TEST_FAILED=$((TEST_FAILED + 1))
    fi
}

assert_contains() {
    local haystack="$1" needle="$2" message="${3:-AssertContains}"
    if [[ "$haystack" == *"$needle"* ]]; then
        echo "  [PASS] $message"; TEST_PASSED=$((TEST_PASSED + 1))
    else
        echo "  [FAIL] $message"; echo "         未找到: $needle"; TEST_FAILED=$((TEST_FAILED + 1))
    fi
}

skip() { echo "  [SKIP] $1"; TEST_SKIPPED=$((TEST_SKIPPED + 1)); }

test_group_start() { echo -e "\n\033[34m===== 测试组: $1 =====\033[0m"; }

# =============================================================================
# 1. 语法验证
# =============================================================================
test_group_start "1. 语法验证"

for script in discover.sh collect.sh; do
    [ -f "$SCRIPTS/$script" ]; assert_success $? "$script 存在"
    if [ -f "$SCRIPTS/$script" ]; then
        bash -n "$SCRIPTS/$script" 2>"$TEST_RESULTS_DIR/syntax_$script.err"
        assert_success $? "$script 语法检查"
    fi
done

if [ ! -f "$SCRIPTS/collect.sh" ]; then
    echo "collect.sh 不存在，跳过后续功能测试"
    echo "============================================"
    echo "  测试汇总"
    echo "============================================"
    echo "  PASS: $TEST_PASSED"
    echo "  FAIL: $TEST_FAILED"
    echo "  SKIP: $TEST_SKIPPED"
    echo "  总计: $((TEST_PASSED + TEST_FAILED + TEST_SKIPPED))"
    echo "============================================"
    exit 1
fi

# =============================================================================
# 2. discover / --help（OS-001 / OS-002）
# =============================================================================
test_group_start "2. discover / --help"

echo "  OS-001: discover.sh"
output=$(bash "$SCRIPTS/discover.sh" 2>&1); rc=$?
assert_success $rc "OS-001: discover 退出码 0"
for tool in sysctl systemctl tuned-adm; do
    assert_contains "$output" "$tool" "OS-001: 输出含 $tool 状态"
done

echo "  OS-002: --help"
output=$(bash "$SCRIPTS/collect.sh" --help 2>&1); rc=$?
assert_success $rc "OS-002: --help 退出码 0"
for token in "--kernel" "--sysctl" "--thp" "--hugepages" "--ulimit" "--services" "--iosched" "--numa-balancing" "--virt" "--cgroup" "--perf" "--all"; do
    assert_contains "$output" "$token" "OS-002: 帮助含 $token"
done

# =============================================================================
# 3. 采集模式（OS-003 ~ OS-009）
# =============================================================================
test_group_start "3. 采集模式"

echo "  OS-003: --kernel"
output=$(bash "$SCRIPTS/collect.sh" --kernel 2>/dev/null); rc=$?
assert_success $rc "OS-003: --kernel 退出码 0"
assert_contains "$output" "uname -a" "OS-003: 含 uname 段"
assert_contains "$output" "os-release" "OS-003: 含 os-release 段"
assert_contains "$output" "/proc/cmdline" "OS-003: 含 cmdline 段"

echo "  OS-004: --sysctl"
output=$(bash "$SCRIPTS/collect.sh" --sysctl 2>/dev/null); rc=$?
assert_success $rc "OS-004: --sysctl 退出码 0"
assert_contains "$output" "vm.swappiness" "OS-004: 含白名单参数 vm.swappiness"
assert_contains "$output" "net.ipv4" "OS-004: 含白名单参数 net.ipv4"

echo "  OS-005: --thp / --hugepages / --numa-balancing / --ulimit"
output=$(bash "$SCRIPTS/collect.sh" --thp 2>/dev/null); rc=$?
assert_success $rc "OS-005: --thp 退出码 0"
assert_contains "$output" "enabled" "OS-005: --thp 输出 enabled 状态"
output=$(bash "$SCRIPTS/collect.sh" --hugepages 2>/dev/null); rc=$?
assert_success $rc "OS-005: --hugepages 退出码 0"
assert_contains "$output" "HugePages" "OS-005: --hugepages 输出"
output=$(bash "$SCRIPTS/collect.sh" --numa-balancing 2>/dev/null); rc=$?
assert_success $rc "OS-005: --numa-balancing 退出码 0"
assert_contains "$output" "numa_balancing" "OS-005: --numa-balancing 输出"
output=$(bash "$SCRIPTS/collect.sh" --ulimit 2>/dev/null); rc=$?
assert_success $rc "OS-005: --ulimit 退出码 0"
assert_contains "$output" "open files" "OS-005: --ulimit 输出 nofile"

echo "  OS-006: --services"
output=$(bash "$SCRIPTS/collect.sh" --services 2>/dev/null); rc=$?
assert_success $rc "OS-006: --services 退出码 0"
assert_contains "$output" "tuned" "OS-006: 输出 tuned 状态"
assert_contains "$output" "irqbalance" "OS-006: 输出 irqbalance 状态"

echo "  OS-007: --iosched"
output=$(bash "$SCRIPTS/collect.sh" --iosched 2>/dev/null); rc=$?
assert_success $rc "OS-007: --iosched 退出码 0"
assert_contains "$output" "scheduler=" "OS-007: 输出 scheduler 字段"

echo "  OS-008: --virt"
output=$(bash "$SCRIPTS/collect.sh" --virt 2>/dev/null); rc=$?
assert_success $rc "OS-008: --virt 退出码 0"
assert_contains "$output" "virtualization" "OS-008: 输出 virtualization 结论行"

echo "  OS-009: --cgroup"
output=$(bash "$SCRIPTS/collect.sh" --cgroup 2>/dev/null); rc=$?
assert_success $rc "OS-009: --cgroup 退出码 0"
if grep -qs 'cgroup2' /proc/filesystems; then
    assert_contains "$output" "v2" "OS-009: v2 环境下报告 v2 路径"
fi

echo "  --perf"
output=$(bash "$SCRIPTS/collect.sh" --perf 2>/dev/null); rc=$?
assert_success $rc "--perf 退出码 0"
assert_contains "$output" "perf_event_paranoid" "--perf 输出 perf_event_paranoid"

# =============================================================================
# 4. --all（OS-010）
# =============================================================================
test_group_start "4. --all"

echo "  OS-010: --all"
output=$(bash "$SCRIPTS/collect.sh" --all 2>"$TEST_RESULTS_DIR/os010.stderr"); rc=$?
assert_success $rc "OS-010: --all 总退出码 0"
for sec in "uname -a" "vm.swappiness" "HugePages" "tuned" "scheduler=" "virtualization" "perf_event_paranoid"; do
    assert_contains "$output" "$sec" "OS-010: 输出含 $sec"
done

# =============================================================================
# 5. 错误处理
# =============================================================================
test_group_start "5. 错误处理"

echo "  未知模式"
output=$(bash "$SCRIPTS/collect.sh" --bogus 2>&1); rc=$?
assert_failure $rc "未知模式退出码非 0"

echo "  无模式参数"
output=$(bash "$SCRIPTS/collect.sh" 2>&1); rc=$?
assert_failure $rc "无模式退出码非 0"

# =============================================================================
# 6. SKILL.md / evals（OS-011）
# =============================================================================
test_group_start "6. SKILL.md / evals"

assert_contains "$(head -3 "$SKILL_DIR/SKILL.md" 2>/dev/null)" "name: kptools-os-collector" "OS-011: frontmatter name 正确"
if [ -f "$SKILL_DIR/SKILL.md" ]; then
    assert_success 0 "OS-011: SKILL.md 存在"
else
    assert_failure 1 "OS-011: SKILL.md 存在"
fi
for f in eval_queries.json evals.json; do
    if [ -f "$SKILL_DIR/evals/$f" ]; then
        python3 -c "import json; json.load(open('$SKILL_DIR/evals/$f'))" 2>/dev/null
        assert_success $? "OS-011: evals/$f JSON 合法"
        grep -q '"skill_name": "kptools-os-collector"' "$SKILL_DIR/evals/$f"
        assert_success $? "OS-011: evals/$f skill_name 正确"
    else
        assert_failure 1 "OS-011: evals/$f 存在"
    fi
done
grep -q 'routes to kptools-bios-collector' "$SKILL_DIR/evals/eval_queries.json" 2>/dev/null
assert_success $? "OS-011: 含跨 SKILL 负例路由（bios-collector）"

# =============================================================================
# 汇总
# =============================================================================
echo ""
echo "============================================"
echo "  测试汇总"
echo "============================================"
if [ $TEST_FAILED -eq 0 ]; then
    green "  PASS: $TEST_PASSED"
else
    red "  PASS: $TEST_PASSED"
fi
red "  FAIL: $TEST_FAILED"
yellow "  SKIP: $TEST_SKIPPED"
echo "  总计: $((TEST_PASSED + TEST_FAILED + TEST_SKIPPED))"
echo "============================================"
exit $TEST_FAILED
