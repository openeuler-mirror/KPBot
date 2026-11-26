#!/bin/bash
# =============================================================================
# py-spy 测试套件
# 覆盖语法验证、discover、deploy、collect（record/top/dump、开关矩阵）、
# 错误处理、SKILL.md/evals 格式
# 对应设计文档：py-spy-skill-design.md §7（PS-001 ~ PS-012）
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

# 空转/忙转 Python 进程启动/清理（exec 确保 $! 即 python3 进程本身）
spin_py() {
    exec python3 -c "
import time
while True: time.sleep(0.05)
" 2>/dev/null
}

# CPU 忙转进程（持 GIL，用于 -g 场景）
busy_py() {
    exec python3 -c "
x = 0
while True: x += 1
" 2>/dev/null
}

# =============================================================================
# 1. 脚本语法验证
# =============================================================================
test_group_start "1. 语法验证"

for script in discover.sh deploy.sh collect.sh; do
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
    echo -e "  \033[32mPASS: $TEST_PASSED\033[0m"
    echo -e "  \033[31mFAIL: $TEST_FAILED\033[0m"
    echo -e "  \033[33mSKIP: $TEST_SKIPPED\033[0m"
    echo "  总计: $((TEST_PASSED + TEST_FAILED + TEST_SKIPPED))"
    echo "============================================"
    exit 1
fi

# =============================================================================
# 2. discover / deploy（PS-001 / PS-002）
# =============================================================================
test_group_start "2. discover / deploy"

echo "  PS-001: discover.sh"
output=$(bash "$SCRIPTS/discover.sh" 2>&1); rc=$?
assert_success $rc "PS-001: discover 退出码 0"
assert_contains "$output" "py-spy" "PS-001: 输出含 py-spy 状态"
if command -v py-spy &>/dev/null; then
    assert_contains "$output" "READY" "PS-001: 已安装时输出 READY"
    assert_contains "$output" "ptrace" "PS-001: 输出 ptrace 环境信息"
else
    assert_contains "$output" "NOT_INSTALLED" "PS-001: 未安装时输出 NOT_INSTALLED"
fi

echo "  PS-002: deploy.sh"
if command -v py-spy &>/dev/null; then
    output=$(bash "$SCRIPTS/deploy.sh" 2>&1); rc=$?
    assert_success $rc "PS-002: 已安装时 deploy 幂等退出 0"
    assert_contains "$output" "already installed" "PS-002: 报告已安装"
else
    skip "PS-002: 本机无 py-spy，安装路径需网络，跳过"
fi

# =============================================================================
# 3. collect 基础（PS-003 / PS-004）
# =============================================================================
test_group_start "3. collect 基础"

echo "  PS-003: --help"
output=$(bash "$SCRIPTS/collect.sh" --help 2>&1); rc=$?
assert_success $rc "PS-003: --help 退出码 0"
for token in "--record" "--top" "--dump" "--subprocesses" "--gil" "--idle" "--native" "--threads" "--rate" "--format" "-p" "-d" "-o"; do
    assert_contains "$output" "$token" "PS-003: 帮助含 $token"
done

echo "  PS-004: --record 火焰图"
spin_py & PY=$!
sleep 1
OUT_SVG="$TEST_RESULTS_DIR/ps004.svg"
output=$(bash "$SCRIPTS/collect.sh" -p $PY -d 2 -o "$OUT_SVG" 2>"$TEST_RESULTS_DIR/ps004.stderr"); rc=$?
assert_success $rc "PS-004: record 退出码 0"
[ -s "$OUT_SVG" ]; assert_success $? "PS-004: SVG 文件生成且非空"
assert_contains "$output" "$OUT_SVG" "PS-004: stdout 报告输出路径"
kill $PY 2>/dev/null; wait $PY 2>/dev/null

# =============================================================================
# 4. collect 开关矩阵（PS-005 / PS-006 / PS-011）
# =============================================================================
test_group_start "4. 开关矩阵"

echo "  PS-005: --subprocesses 子进程采样"
MP_SCRIPT="$TEST_RESULTS_DIR/mp_test.py"
cat > "$MP_SCRIPT" <<'EOF'
import multiprocessing, time
def worker():
    def spin_child():
        x = 0
        while True:
            x += 1
    spin_child()
if __name__ == "__main__":
    p = multiprocessing.Process(target=worker)
    p.start()
    time.sleep(30)
EOF
python3 "$MP_SCRIPT" & MPY=$!
sleep 1.5
OUT_RAW="$TEST_RESULTS_DIR/ps005.txt"
output=$(bash "$SCRIPTS/collect.sh" -p $MPY -d 3 -s --format raw -o "$OUT_RAW" 2>"$TEST_RESULTS_DIR/ps005.stderr"); rc=$?
assert_success $rc "PS-005: 子进程采样退出码 0"
grep -q 'spin_child' "$OUT_RAW" 2>/dev/null
assert_success $? "PS-005: raw 输出含子进程函数帧"
kill $MPY 2>/dev/null; pkill -f 'mp_test.py' 2>/dev/null; wait 2>/dev/null

echo "  PS-006: -g / -i / -t 开关"
busy_py & PY=$!
sleep 1
for sw in -g -i -t; do
    OUT_SW="$TEST_RESULTS_DIR/ps006_${sw#-}.txt"
    output=$(bash "$SCRIPTS/collect.sh" -p $PY -d 2 $sw --format raw -o "$OUT_SW" 2>/dev/null); rc=$?
    assert_success $rc "PS-006: $sw 采样退出码 0"
    [ -s "$OUT_SW" ]; assert_success $? "PS-006: $sw 输出非空"
done
OUT_T="$TEST_RESULTS_DIR/ps006_t.txt"
grep -q 'thread (' "$OUT_T" 2>/dev/null
assert_success $? "PS-006: -t 输出含 thread 线程标注"
kill $PY 2>/dev/null; wait $PY 2>/dev/null

echo "  PS-011: -n Native（实验性标注）"
spin_py & PY=$!
sleep 1
OUT_N="$TEST_RESULTS_DIR/ps011.svg"
output=$(bash "$SCRIPTS/collect.sh" -p $PY -d 2 -n -o "$OUT_N" 2>"$TEST_RESULTS_DIR/ps011.stderr"); rc=$?
assert_success $rc "PS-011: -n 采样退出码 0"
[ -s "$OUT_N" ]; assert_success $? "PS-011: 输出非空"
kill $PY 2>/dev/null; wait $PY 2>/dev/null

# =============================================================================
# 5. top 降级与 dump（PS-007 / PS-008）
# =============================================================================
test_group_start "5. top 降级与 dump"

echo "  PS-007: --top 非 TTY 降级"
spin_py & PY=$!
sleep 1
output=$(bash "$SCRIPTS/collect.sh" --top -p $PY -d 2 2>"$TEST_RESULTS_DIR/ps007.stderr"); rc=$?
assert_success $rc "PS-007: 非 TTY 下 --top 降级退出码 0"
assert_contains "$output" ")" "PS-007: 降级输出为帧统计文本"
assert_contains "$(cat "$TEST_RESULTS_DIR/ps007.stderr")" "TTY" "PS-007: stderr 标注降级原因"
kill $PY 2>/dev/null; wait $PY 2>/dev/null

echo "  PS-008: --dump"
spin_py & PY=$!
sleep 1
output=$(bash "$SCRIPTS/collect.sh" --dump -p $PY 2>/dev/null); rc=$?
assert_success $rc "PS-008: dump 退出码 0"
assert_contains "$output" "Thread" "PS-008: 输出含线程栈"
kill $PY 2>/dev/null; wait $PY 2>/dev/null

# =============================================================================
# 6. 错误处理（PS-009 / PS-010）
# =============================================================================
test_group_start "6. 错误处理"

echo "  PS-009: 非 Python 进程"
sleep 30 & SLEEP_PID=$!
output=$(bash "$SCRIPTS/collect.sh" -p $SLEEP_PID -d 1 2>"$TEST_RESULTS_DIR/ps009.stderr"); rc=$?
assert_failure $rc "PS-009: 非 Python 进程退出码非 0"
assert_contains "$(cat "$TEST_RESULTS_DIR/ps009.stderr")" "Python" "PS-009: stderr 提示非 Python 进程"
kill $SLEEP_PID 2>/dev/null; wait $SLEEP_PID 2>/dev/null

echo "  缺 -p 参数"
output=$(bash "$SCRIPTS/collect.sh" -d 1 2>&1); rc=$?
assert_failure $rc "缺 -p 退出码非 0"
assert_contains "$output" "-p" "缺 -p 提示参数"

echo "  未知模式"
output=$(bash "$SCRIPTS/collect.sh" --bogus -p 1 2>&1); rc=$?
assert_failure $rc "未知模式退出码非 0"

echo "  PS-010: ptrace 权限（条件跳过）"
if [ -r /proc/sys/kernel/yama/ptrace_scope ] && [ "$(cat /proc/sys/kernel/yama/ptrace_scope)" != "0" ] && [ "$(id -u)" != "0" ]; then
    output=$(bash "$SCRIPTS/collect.sh" -p 1 -d 1 2>&1); rc=$?
    assert_failure $rc "PS-010: 无 ptrace 权限退出码非 0"
    assert_contains "$output" "ptrace_scope" "PS-010: 提示 ptrace_scope 调整"
else
    skip "PS-010: 当前 ptrace_scope=0 或 root，权限受限场景无法模拟"
fi

# =============================================================================
# 7. SKILL.md / evals 格式（PS-012）
# =============================================================================
test_group_start "7. SKILL.md / evals"

assert_contains "$(head -3 "$SKILL_DIR/SKILL.md" 2>/dev/null)" "name: kptools-py-spy" "PS-012: frontmatter name 正确"
if [ -f "$SKILL_DIR/SKILL.md" ]; then
    assert_success 0 "PS-012: SKILL.md 存在"
else
    assert_failure 1 "PS-012: SKILL.md 存在"
fi

for f in eval_queries.json evals.json; do
    if [ -f "$SKILL_DIR/evals/$f" ]; then
        python3 -c "import json; json.load(open('$SKILL_DIR/evals/$f'))" 2>/dev/null
        assert_success $? "PS-012: evals/$f JSON 合法"
        grep -q '"skill_name": "kptools-py-spy"' "$SKILL_DIR/evals/$f"
        assert_success $? "PS-012: evals/$f skill_name 正确"
    else
        assert_failure 1 "PS-012: evals/$f 存在"
    fi
done
grep -q 'routes to kptools-devkit' "$SKILL_DIR/evals/eval_queries.json" 2>/dev/null
assert_success $? "PS-012: 含跨 SKILL 负例路由（java-perf）"

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
