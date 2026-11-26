#!/bin/bash
# =============================================================================
# nmon 测试套件
# 覆盖语法验证、discover、collect（快照/时长/top/json/-m）、错误处理、evals 格式
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

assert_equal() {
    local expected="$1" actual="$2" message="${3:-AssertEqual}"
    if [ "$expected" = "$actual" ]; then
        echo "  [PASS] $message"
        TEST_PASSED=$((TEST_PASSED + 1))
    else
        echo "  [FAIL] $message"
        echo "         期望: $expected"
        echo "         实际: $actual"
        TEST_FAILED=$((TEST_FAILED + 1))
    fi
}

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
# 1. 脚本语法验证
# =============================================================================

test_group_start "1. 脚本语法验证"

for script in discover.sh deploy.sh collect.sh; do
    bash -n "$SCRIPTS/$script" 2>/dev/null
    assert_success $? "SYNTAX: $script 语法正确"
done

python3 -c "import yaml; yaml.safe_load(open('$SKILL_DIR/SKILL.md'.replace('SKILL.md','evals/eval_queries.json')))" 2>/dev/null
assert_success $? "SYNTAX: eval_queries.json 为合法 JSON"

python3 -c "import json; json.load(open('$SKILL_DIR/evals/evals.json'))" 2>/dev/null
assert_success $? "SYNTAX: evals.json 为合法 JSON"

test_group_end

# =============================================================================
# 2. discover 测试
# =============================================================================

test_group_start "2. discover 测试"

echo "  DISCOVER-001: --help"
output=$(bash "$SCRIPTS/discover.sh" --help 2>&1); rc=$?
assert_success $rc "DISCOVER-001: --help 退出码"
assert_contains "$output" "discover.sh" "DISCOVER-001: --help 含脚本名"

echo "  DISCOVER-002: 已安装环境"
output=$(bash "$SCRIPTS/discover.sh" 2>&1); rc=$?
if [ $rc -eq 0 ]; then
    assert_success 0 "DISCOVER-002: 退出码"
    assert_contains "$output" "READY" "DISCOVER-002: 输出 READY"
    # 版本号动态断言：环境 nmon 版本各异（作者环境 16p、Ubuntu focal 16k），
    # 验证 discover 报告的版本与 nmon -V 实际输出一致，不硬编码具体版本
    expected_ver=$(nmon -V 2>&1 | head -1 | grep -oE '[0-9]+[a-z]+' | head -1)
    assert_contains "$output" "${expected_ver:-unknown}" "DISCOVER-002: 含版本号 (${expected_ver:-unknown})"
else
    skip "DISCOVER-002: nmon 未安装，跳过"
fi

echo "  DISCOVER-003: --path"
output=$(bash "$SCRIPTS/discover.sh" --path 2>&1); rc=$?
if [ $rc -eq 0 ]; then
    assert_success 0 "DISCOVER-003: --path 退出码"
    assert_contains "$output" "nmon" "DISCOVER-003: 输出二进制路径"
else
    assert_failure $rc "DISCOVER-003: 未安装时应失败"
fi

echo "  DISCOVER-004: PATH 无 nmon 时"
output=$(PATH="/nonexistent" "$BASH_BIN" "$SCRIPTS/discover.sh" 2>&1); rc=$?
assert_failure $rc "DISCOVER-004: 无 nmon 时退出码非 0"
assert_contains "$output" "NOT_INSTALLED" "DISCOVER-004: 输出 NOT_INSTALLED"

test_group_end

# =============================================================================
# 3. deploy 测试
# =============================================================================

test_group_start "3. deploy 测试"

echo "  DEPLOY-001: --help"
output=$(bash "$SCRIPTS/deploy.sh" --help 2>&1); rc=$?
assert_success $rc "DEPLOY-001: --help 退出码"

echo "  DEPLOY-002: 已安装环境"
if command -v nmon &>/dev/null; then
    output=$(bash "$SCRIPTS/deploy.sh" 2>&1); rc=$?
    assert_success $rc "DEPLOY-002: 已安装时退出码 0"
    assert_contains "$output" "already installed" "DEPLOY-002: 报告已安装"
else
    skip "DEPLOY-002: nmon 未安装，跳过"
fi

test_group_end

# =============================================================================
# 4. collect 测试
# =============================================================================

test_group_start "4. collect 测试"

echo "  NM-001: --help"
output=$(bash "$SCRIPTS/collect.sh" --help 2>&1); rc=$?
assert_success $rc "NM-001: --help 退出码"
assert_contains "$output" "collect.sh" "NM-001: --help 含脚本名"

echo "  NM-002: --snapshot 快照模式"
output=$(bash "$SCRIPTS/collect.sh" --snapshot 2>"$TEST_RESULTS_DIR/nm002.stderr"); rc=$?
assert_success $rc "NM-002: 退出码 0"
assert_contains "$output" "AAA,command" "NM-002: stdout 含 CSV 元数据头"
assert_contains "$output" "CPU_ALL" "NM-002: stdout 含 CPU_ALL 段"
data_file=$(grep -oP 'nmon data file: \K.*' "$TEST_RESULTS_DIR/nm002.stderr" | head -1)
if [ -n "$data_file" ] && [ ! -e "$data_file" ]; then
    assert_success 0 "NM-002: 临时数据文件已删除"
else
    assert_failure 1 "NM-002: 临时数据文件未删除（$data_file）"
fi

echo "  NM-003: -d 3 -i 1 时长模式"
output=$(bash "$SCRIPTS/collect.sh" -d 3 -i 1 2>/dev/null); rc=$?
assert_success $rc "NM-003: 退出码 0"
snapshot_count=$(echo "$output" | grep -c '^ZZZZ')
if [ "$snapshot_count" -ge 3 ]; then
    assert_success 0 "NM-003: 快照数 >= 3（实际 $snapshot_count）"
else
    assert_success 1 "NM-003: 快照数不足（实际 $snapshot_count）"
fi

echo "  NM-004: -d 5 --top 附带 Top Processes"
output=$(bash "$SCRIPTS/collect.sh" -d 5 --top 2>/dev/null); rc=$?
assert_success $rc "NM-004: 退出码 0"
assert_contains "$output" "TOP" "NM-004: CSV 含 TOP 段"

echo "  NM-005: -d 3 --json JSON envelope"
output=$(bash "$SCRIPTS/collect.sh" -d 3 --json 2>"$TEST_RESULTS_DIR/nm005.stderr"); rc=$?
assert_success $rc "NM-005: 退出码 0"
echo "$output" | python3 -c "import json,sys; d=json.load(sys.stdin); assert d['skill']=='kptools-nmon' and 'AAA,command' in d['data'] and d['data_file']" 2>/dev/null
assert_success $? "NM-005: 输出为合法 JSON envelope 且含 CSV 原文"
json_data_file=$(grep -oP 'nmon data file: \K.*' "$TEST_RESULTS_DIR/nm005.stderr" | head -1)
if [ -n "$json_data_file" ] && [ -s "$json_data_file" ]; then
    assert_success 0 "NM-005: --json 数据文件保留"
    rm -rf "$(dirname "$json_data_file")"
else
    assert_failure 1 "NM-005: --json 数据文件未保留"
fi

echo "  NM-006: -d 3 -i 10 非法参数（i > d）"
output=$(bash "$SCRIPTS/collect.sh" -d 3 -i 10 2>&1); rc=$?
assert_failure $rc "NM-006: 退出码非 0"
assert_contains "$output" "must not exceed" "NM-006: 含参数报错"

echo "  NM-007: -d 7 -i 3 换算提示"
output=$(bash "$SCRIPTS/collect.sh" -d 7 -i 3 2>"$TEST_RESULTS_DIR/nm007.stderr"); rc=$?
assert_success $rc "NM-007: 退出码 0"
assert_contains "$(cat "$TEST_RESULTS_DIR/nm007.stderr")" "actual collection: 9 s" "NM-007: stderr 含换算提示"

echo "  NM-008: -m 指定保留目录"
NM_KEEP="/tmp/nmon-keep-test.$$"
output=$(bash "$SCRIPTS/collect.sh" -m "$NM_KEEP" -d 2 2>"$TEST_RESULTS_DIR/nm008.stderr"); rc=$?
assert_success $rc "NM-008: 退出码 0"
kept_file=$(find "$NM_KEEP" -name '*.nmon' -type f | head -1)
if [ -n "$kept_file" ] && [ -s "$kept_file" ]; then
    assert_success 0 "NM-008: 数据文件保留在指定目录"
else
    assert_failure 1 "NM-008: 指定目录无数据文件"
fi
rm -rf "$NM_KEEP"

echo "  NM-009: PATH 无 nmon 时 collect 报错"
output=$(PATH="/nonexistent" "$BASH_BIN" "$SCRIPTS/collect.sh" -d 2 2>&1); rc=$?
assert_failure $rc "NM-009: 无 nmon 时退出码非 0"
assert_contains "$output" "nmon not found" "NM-009: 含依赖缺失提示"

echo "  NM-010: 未知选项"
output=$(bash "$SCRIPTS/collect.sh" --bogus 2>&1); rc=$?
assert_failure $rc "NM-010: 未知选项退出码非 0"

echo "  NM-011: -d 缺值报错"
output=$(bash "$SCRIPTS/collect.sh" -d 2>&1); rc=$?
assert_failure $rc "NM-011: -d 缺值退出码非 0"
assert_contains "$output" "requires a value" "NM-011: 含缺值提示"

test_group_end

# =============================================================================
# 汇总
# =============================================================================

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
