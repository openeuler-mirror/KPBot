#!/bin/bash
# =============================================================================
# kptools-dplb-report 测试套件
# 覆盖：generate_report.py 语法检查 + 报告端回归（评级分级/小样本护栏/短窗口
#       说明/硬失败/主机解析/--print-schema/新指标）。
# 用法: bash scripts/test/test_dplb_report.sh
# =============================================================================
set +e

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$TEST_DIR/../.." && pwd)"
SCRIPTS="$SKILL_DIR/scripts"
TEST_PASSED=0
TEST_FAILED=0
export PYTHONDONTWRITEBYTECODE=1

# 解析可用的 Python：逐个验证能否真正执行（规避 Windows 商店 python 占位 stub）
find_py() {
    local c
    for c in python3 python py; do
        if command -v "$c" >/dev/null 2>&1 && "$c" -c "import sys" >/dev/null 2>&1; then
            command -v "$c"
            return 0
        fi
    done
    return 1
}
PY_BIN="$(find_py)"
if [ -z "$PY_BIN" ]; then
    echo "[FATAL] 未找到可用的 python3/python/py，无法运行测试套件" >&2
    exit 2
fi

pass() { echo "  [PASS] $1"; TEST_PASSED=$((TEST_PASSED + 1)); }
fail() { echo "  [FAIL] $1"; TEST_FAILED=$((TEST_FAILED + 1)); }

run_py_suite() {
    # 运行 Python 子套件一次：透传输出并按末行 "N/M" 计入汇总
    local name="$1" file="$2" out rc frac
    out=$("$PY_BIN" "$file" 2>&1)
    rc=$?
    echo "$out"
    frac=$(echo "$out" | grep -oE '[0-9]+/[0-9]+' | tail -1)
    [ -n "$frac" ] || frac="rc=$rc"
    if [ "$rc" -eq 0 ]; then
        pass "$name ($frac)"
    else
        fail "$name ($frac)"
    fi
}

echo "== 0. 语法检查 =="
if "$PY_BIN" -c "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())" "$SCRIPTS/generate_report.py" 2>/dev/null; then
    pass "generate_report.py syntax"
else
    fail "generate_report.py syntax"
fi

echo "== 1. 报告端回归（generate_report.py，合成 CSV） =="
run_py_suite "报告回归套件" "$TEST_DIR/test_generate_report.py"

echo ""
echo "== dplb-report 套件结果: PASS=$TEST_PASSED FAIL=$TEST_FAILED =="
[ "$TEST_FAILED" -eq 0 ]
