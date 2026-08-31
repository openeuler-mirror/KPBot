#!/bin/bash
# =============================================================================
# 目标平台 ISA 能力矩阵测试套件
# 验证 detect_isa_features.sh --target 与 query_uarch_b.py 修复
# =============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DETECT="$SCRIPT_DIR/../../apply-vectorization/scripts/detect_isa_features.sh"
QUERY="$SCRIPT_DIR/query_uarch_b.py"

TEST_PASSED=0
TEST_FAILED=0

pass() { echo "  [PASS] $1"; TEST_PASSED=$((TEST_PASSED + 1)); }
fail() { echo "  [FAIL] $1"; TEST_FAILED=$((TEST_FAILED + 1)); }

assert_contains() {
    local haystack="$1" needle="$2" message="$3"
    if [[ "$haystack" == *"$needle"* ]]; then
        pass "$message"
    else
        fail "$message (缺: $needle)"
    fi
}

assert_not_contains() {
    local haystack="$1" needle="$2" message="$3"
    if [[ "$haystack" != *"$needle"* ]]; then
        pass "$message"
    else
        fail "$message (不应有: $needle)"
    fi
}

assert_exit_code() {
    local expected="$1" actual="$2" message="$3"
    if [ "$actual" -eq "$expected" ]; then
        pass "$message"
    else
        fail "$message (期望 $expected 实际 $actual)"
    fi
}

echo "== detect_isa_features.sh --target =="
OUT_D01="$(bash "$DETECT" --target 0xd01 --json)"
assert_contains "$OUT_D01" '"neon":true' "0xd01 支持 NEON"
assert_contains "$OUT_D01" '"sve":false' "0xd01 不支持 SVE"
assert_contains "$OUT_D01" '"sme":false' "0xd01 不支持 SME"
assert_contains "$OUT_D01" '"dotprod":false' "0xd01 不支持 DotProd"

OUT_D03="$(bash "$DETECT" --target 0xd03 --json)"
assert_contains "$OUT_D03" '"sve":true' "0xd03 支持 SVE"
assert_contains "$OUT_D03" '"dotprod":true' "0xd03 支持 DotProd"
assert_contains "$OUT_D03" '"sme":false' "0xd03 不支持 SME"

OUT_D06="$(bash "$DETECT" --target 0xd06 --json)"
assert_contains "$OUT_D06" '"sve":true' "0xd06 支持 SVE"
assert_contains "$OUT_D06" '"bf16":true' "0xd06 支持 BF16"
assert_contains "$OUT_D06" '"i8mm":true' "0xd06 支持 I8MM"

set +e
bash "$DETECT" --target 0xd01 --require sve >/dev/null 2>&1
rc=$?
set -e
assert_exit_code 3 "$rc" "0xd01 --require sve 应拒绝 (exit 3)"

set +e
bash "$DETECT" --target 0xd06 --require sve >/dev/null 2>&1
rc=$?
set -e
assert_exit_code 0 "$rc" "0xd06 --require sve 应通过 (exit 0)"

set +e
bash "$DETECT" --target 0xd99 >/dev/null 2>&1
rc=$?
set -e
assert_exit_code 1 "$rc" "未知平台 0xd99 应报错 (exit 1)"

echo "== query_uarch_b.py (0xd03) =="
OUT_QUERY="$(python3 "$QUERY" 'FADD')"
assert_contains "$OUT_QUERY" '指令: FADD' "0xd03 FADD 查询可用"
assert_contains "$OUT_QUERY" 'SVE128' "0xd03 FADD 含 SVE128 吞吐"

echo
echo "结果: $TEST_PASSED 通过, $TEST_FAILED 失败"
[ "$TEST_FAILED" -eq 0 ]
