#!/bin/bash
# =============================================================================
# node-profiler 测试套件
# 覆盖语法验证、discover、--parse/--parse-dir/--cpuprofile/--guide、错误处理
# 对应设计文档：node-profiler-skill-design.md §7（NP-001 ~ NP-008）
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

# 生成真实 V8 isolate 日志（node --prof 跑一个 CPU 忙脚本）
gen_isolate_log() {
    local outdir="$1"
    mkdir -p "$outdir"
    cat > "$outdir/nptest.js" <<'EOF'
function spin(){let x=0;for(let i=0;i<3e7;i++){x+=i}return x}
setTimeout(spin, 10)
setTimeout(()=>process.exit(0), 1500)
EOF
    (cd "$outdir" && node --prof nptest.js && node --prof nptest.js)
    ls "$outdir"/isolate-*.log >/dev/null 2>&1
}

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
# 2. discover / --help（NP-001 / NP-002）
# =============================================================================
test_group_start "2. discover / --help"

echo "  NP-001: discover.sh"
output=$(bash "$SCRIPTS/discover.sh" 2>&1); rc=$?
assert_success $rc "NP-001: discover 退出码 0"
assert_contains "$output" "node" "NP-001: 输出含 node 状态"
assert_contains "$output" "npm" "NP-001: 输出含 npm 状态"
assert_contains "$output" "0x" "NP-001: 输出含 0x 状态"

echo "  NP-002: --help"
output=$(bash "$SCRIPTS/collect.sh" --help 2>&1); rc=$?
assert_success $rc "NP-002: --help 退出码 0"
for token in "--parse" "--parse-dir" "--cpuprofile" "--guide" "-f" "-d" "-p"; do
    assert_contains "$output" "$token" "NP-002: 帮助含 $token"
done

# =============================================================================
# 3. 采集模式（NP-003 ~ NP-006）
# =============================================================================
test_group_start "3. 采集模式"

echo "  NP-003: --parse（真实 isolate 日志）"
LOGDIR="$TEST_RESULTS_DIR/logs"
if command -v node &>/dev/null; then
    gen_isolate_log "$LOGDIR"
    if ls "$LOGDIR"/isolate-*.log >/dev/null 2>&1; then
        LOG1=$(ls "$LOGDIR"/isolate-*.log | head -1)
        output=$(bash "$SCRIPTS/collect.sh" --parse -f "$LOG1" 2>"$TEST_RESULTS_DIR/np003.stderr"); rc=$?
        assert_success $rc "NP-003: --parse 退出码 0"
        assert_contains "$output" "[Summary]" "NP-003: 输出含 [Summary] 段"
        assert_contains "$output" "[JavaScript]" "NP-003: 输出含 [JavaScript] 段"
    else
        skip "NP-003: node --prof 未生成日志"
    fi
else
    skip "NP-003: 本机无 node"
fi

echo "  NP-004: --parse-dir 多日志"
if ls "$LOGDIR"/isolate-*.log >/dev/null 2>&1; then
    N_LOGS=$(ls "$LOGDIR"/isolate-*.log | wc -l)
    output=$(bash "$SCRIPTS/collect.sh" --parse-dir -d "$LOGDIR" 2>"$TEST_RESULTS_DIR/np004.stderr"); rc=$?
    assert_success $rc "NP-004: --parse-dir 退出码 0"
    N_PARSED=$(printf '%s' "$output" | grep -c '^## isolate-')
    assert_equal "$N_LOGS" "$N_PARSED" "NP-004: 每个日志均解析（$N_LOGS 个）"
    assert_contains "$output" "[Summary]" "NP-004: 解析输出含 [Summary]"
else
    skip "NP-004: 无 isolate 日志"
fi

echo "  NP-005: --cpuprofile"
mkdir -p "$TEST_RESULTS_DIR/cpudir"
cat > "$TEST_RESULTS_DIR/cpudir/cpu_mock.cpuprofile" <<'EOF'
{"nodes":[{"id":1,"callFrame":{"functionName":"main","url":"file:///a.js","lineNumber":0},"hitCount":5,"children":[]}],"startTime":1,"endTime":2}
EOF
output=$(bash "$SCRIPTS/collect.sh" --cpuprofile -d "$TEST_RESULTS_DIR/cpudir" 2>/dev/null); rc=$?
assert_success $rc "NP-005: --cpuprofile 退出码 0"
assert_contains "$output" "cpu_mock.cpuprofile" "NP-005: 定位并报告 cpuprofile 文件"
assert_contains "$output" "Chrome" "NP-005: 输出可视化指引"

echo "  NP-006: --guide"
output=$(bash "$SCRIPTS/collect.sh" --guide -p 12345 2>/dev/null); rc=$?
assert_success $rc "NP-006: --guide 退出码 0"
assert_contains "$output" "--prof" "NP-006: 含 node --prof 注入模板"
assert_contains "$output" "12345" "NP-006: 模板含目标 PID"
assert_contains "$output" "0x" "NP-006: 含 0x 可选路径"

# =============================================================================
# 4. 错误处理（NP-007）
# =============================================================================
test_group_start "4. 错误处理"

echo "  NP-007: 文件不存在"
output=$(bash "$SCRIPTS/collect.sh" --parse -f /nonexistent.log 2>&1); rc=$?
assert_failure $rc "NP-007: 日志不存在退出码非 0"
assert_contains "$output" "无法读取" "NP-007: 报错提示"

echo "  未知模式"
output=$(bash "$SCRIPTS/collect.sh" --bogus 2>&1); rc=$?
assert_failure $rc "未知模式退出码非 0"

# =============================================================================
# 5. SKILL.md / evals（NP-008）
# =============================================================================
test_group_start "5. SKILL.md / evals"

assert_contains "$(head -3 "$SKILL_DIR/SKILL.md" 2>/dev/null)" "name: kptools-node-profiler" "NP-008: frontmatter name 正确"
if [ -f "$SKILL_DIR/SKILL.md" ]; then
    assert_success 0 "NP-008: SKILL.md 存在"
else
    assert_failure 1 "NP-008: SKILL.md 存在"
fi
for f in eval_queries.json evals.json; do
    if [ -f "$SKILL_DIR/evals/$f" ]; then
        python3 -c "import json; json.load(open('$SKILL_DIR/evals/$f'))" 2>/dev/null
        assert_success $? "NP-008: evals/$f JSON 合法"
        grep -q '"skill_name": "kptools-node-profiler"' "$SKILL_DIR/evals/$f"
        assert_success $? "NP-008: evals/$f skill_name 正确"
    else
        assert_failure 1 "NP-008: evals/$f 存在"
    fi
done
grep -q 'routes to kptools-py-spy' "$SKILL_DIR/evals/eval_queries.json" 2>/dev/null
assert_success $? "NP-008: 含跨 SKILL 负例路由（py-spy）"

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
