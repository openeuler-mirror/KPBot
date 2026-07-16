#!/bin/bash
# =============================================================================
# spe-compare.sh 测试套件
# 测试参数解析、diff 计算、JSON 输出和错误处理
# =============================================================================

set -e

red()     { echo -e "\033[31m$*\033[0m"; }
green()   { echo -e "\033[32m$*\033[0m"; }
yellow()  { echo -e "\033[33m$*\033[0m"; }
blue()    { echo -e "\033[34m$*\033[0m"; }

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="$(dirname "$TEST_DIR")"
SCRIPT="$SCRIPT_DIR/spe-compare.sh"
TEST_RESULTS_DIR="$TEST_DIR/test_results"
TEST_PASSED=0
TEST_FAILED=0

MOCK_DIR="$TEST_RESULTS_DIR/compare_mock"

# =============================================================================
# 断言函数
# =============================================================================

assert_contains() {
    local haystack="$1"; local needle="$2"; local message="${3:-AssertContains}"
    if [[ "$haystack" == *"$needle"* ]]; then
        echo "  [PASS] $message"; TEST_PASSED=$((TEST_PASSED + 1))
    else
        echo "  [FAIL] $message"; echo "         未找到: $needle"; TEST_FAILED=$((TEST_FAILED + 1))
    fi
}

assert_not_contains() {
    local haystack="$1"; local needle="$2"; local message="${3:-AssertNotContains}"
    if [[ "$haystack" != *"$needle"* ]]; then
        echo "  [PASS] $message"; TEST_PASSED=$((TEST_PASSED + 1))
    else
        echo "  [FAIL] $message"; echo "         不应包含: $needle"; TEST_FAILED=$((TEST_FAILED + 1))
    fi
}

assert_exit_code() {
    local expected="$1"; local actual="$2"; local message="${3:-AssertExitCode}"
    if [ "$actual" -eq "$expected" ]; then
        echo "  [PASS] $message"; TEST_PASSED=$((TEST_PASSED + 1))
    else
        echo "  [FAIL] $message (期望 exit=$expected, 实际 exit=$actual)"; TEST_FAILED=$((TEST_FAILED + 1))
    fi
}

# =============================================================================
# Mock 环境
# =============================================================================

# 创建 mock spe-parse.sh，输出当前格式的 --summary 数据
setup_mock() {
    rm -rf "$MOCK_DIR"
    mkdir -p "$MOCK_DIR"

    cat > "$MOCK_DIR/spe-parse.sh" <<'MOCKEOF'
#!/bin/bash
# Mock spe-parse.sh that outputs current-format summary
# Supports: --summary, otherwise full output
SUMMARY_ONLY=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --summary) SUMMARY_ONLY=true; shift ;;
        *) shift ;;
    esac
done

# Determine which data file to mock (check args for "before" or "after")
# Default: use a moderate set of values
cat <<'EOF'
=== SPE 采样汇总 ===
总记录数:      50000

--- Cache 分布 ---
L1 miss:   5000 (10.0%)
LLC miss:  1000 (2.0%)
Remote:    100 (0.2%)

--- TLB 分布 ---
TLB access: 20000 (40.0%)
TLB miss:   1000 (5.0% of TLB)

--- 分支预测 ---
Branch miss: 800 (100.0% of branches)
EOF
MOCKEOF

    # 创建第二个 mock 用于 "after" 数据（数值更好，表示优化后）
    cat > "$MOCK_DIR/spe-parse-improved.sh" <<'MOCKEOF'
#!/bin/bash
while [[ $# -gt 0 ]]; do shift; done
cat <<'EOF'
=== SPE 采样汇总 ===
总记录数:      48000

--- Cache 分布 ---
L1 miss:   2400 (5.0%)
LLC miss:  480 (1.0%)
Remote:    48 (0.1%)

--- TLB 分布 ---
TLB access: 16000 (33.3%)
TLB miss:   480 (3.0% of TLB)

--- 分支预测 ---
Branch miss: 400 (100.0% of branches)
EOF
MOCKEOF

    chmod +x "$MOCK_DIR/spe-parse.sh" "$MOCK_DIR/spe-parse-improved.sh"
    ln -sf "$SCRIPT" "$MOCK_DIR/spe-compare.sh"
    touch "$MOCK_DIR/before.data" "$MOCK_DIR/after.data"
}

# 创建 mock 其中 after 数据更差（回归）
setup_mock_regression() {
    rm -rf "$MOCK_DIR"
    mkdir -p "$MOCK_DIR"

    cat > "$MOCK_DIR/spe-parse.sh" <<'MOCKEOF'
#!/bin/bash
while [[ $# -gt 0 ]]; do shift; done
cat <<'EOF'
=== SPE 采样汇总 ===
总记录数:      50000
L1 miss:   2000 (4.0%)
LLC miss:  500 (1.0%)
Remote:    25 (0.05%)
TLB access: 20000 (40.0%)
TLB miss:   400 (2.0% of TLB)
Branch miss: 300 (100.0% of branches)
EOF
MOCKEOF

    cat > "$MOCK_DIR/spe-parse-improved.sh" <<'MOCKEOF'
#!/bin/bash
while [[ $# -gt 0 ]]; do shift; done
cat <<'EOF'
=== SPE 采样汇总 ===
总记录数:      52000
L1 miss:   3120 (6.0%)
LLC miss:  780 (1.5%)
Remote:    52 (0.1%)
TLB access: 21000 (40.4%)
TLB miss:   630 (3.0% of TLB)
Branch miss: 520 (100.0% of branches)
EOF
MOCKEOF

    chmod +x "$MOCK_DIR/spe-parse.sh" "$MOCK_DIR/spe-parse-improved.sh"
    ln -sf "$SCRIPT" "$MOCK_DIR/spe-compare.sh"
    touch "$MOCK_DIR/before.data" "$MOCK_DIR/after.data"
}

# =============================================================================
# 测试组: 参数解析
# =============================================================================

test_help_short() {
    blue "测试: -h 显示帮助"
    local output
    output=$(bash "$SCRIPT" -h 2>&1) || true
    assert_contains "$output" "用法:" "-h 应显示用法"
    assert_contains "$output" "spe-compare.sh" "-h 应显示脚本名"
}

test_help_long() {
    blue "测试: --help 显示帮助"
    local output
    output=$(bash "$SCRIPT" --help 2>&1) || true
    assert_contains "$output" "--metric" "--help 应显示 --metric 选项"
    assert_contains "$output" "--format" "--help 应显示 --format 选项"
}

test_missing_files() {
    blue "测试: 缺少文件参数"
    local output
    output=$(bash "$SCRIPT" 2>&1) || true
    assert_contains "$output" "错误" "缺少文件应报错"
    assert_contains "$output" "before" "应提示需要 before/after 文件"
}

test_missing_second_file() {
    blue "测试: 仅指定一个文件"
    local output
    output=$(bash "$SCRIPT" /tmp/dummy.data 2>&1) || true
    assert_contains "$output" "错误" "仅一个文件应报错"
}

test_nonexistent_file() {
    blue "测试: 文件不存在"
    local exit_code=0
    local output
    output=$(bash "$SCRIPT" /tmp/before_nonexist_$$.data /tmp/after_nonexist_$$.data 2>&1) || exit_code=$?
    assert_contains "$output" "文件不存在" "不存在的文件应报错"
    assert_exit_code 1 "$exit_code" "应返回退出码 1"
}

test_extra_args() {
    blue "测试: 多余参数"
    setup_mock
    local output
    output=$(bash "$MOCK_DIR/spe-compare.sh" "$MOCK_DIR/before.data" "$MOCK_DIR/after.data" extra 2>&1) || true
    assert_contains "$output" "多余参数" "应提示多余参数"
}

test_unknown_option() {
    blue "测试: 未知选项"
    local output
    output=$(bash "$SCRIPT" --bad-flag 2>&1) || true
    assert_contains "$output" "未知选项" "未知选项应提示"
}

# =============================================================================
# 测试组: extract_summary 函数
# =============================================================================

test_extract_summary_current_format() {
    blue "测试: extract_summary 解析当前 spe-parse.sh 格式"
    # 直接测试函数逻辑
    local result
    result=$(bash -c '
        extract_summary() {
            echo "$1" | awk '\''
            /^总记录数:/ { total=$2 }
            /^L1 miss:/ { l1_miss=$3 }
            /^LLC miss:/ { llc_miss=$3 }
            /^Remote:/ { remote=$3 }
            /^TLB access:/ { tlb_access=$3 }
            /^TLB miss:/ { tlb_miss=$3 }
            /^Branch miss:/ { branch_miss=$3 }
            END {
                printf "total=%d l1_miss=%d llc_miss=%d remote=%d tlb_access=%d tlb_miss=%d branch_miss=%d\n",
                    total, l1_miss, llc_miss, remote, tlb_access, tlb_miss, branch_miss
            }
            '\''
        }
        DATA="总记录数:      50000
L1 miss:   5000 (10.0%)
LLC miss:  1000 (2.0%)
Remote:    100 (0.2%)
TLB access: 20000 (40.0%)
TLB miss:   1000 (5.0% of TLB)
Branch miss: 800 (100.0% of branches)"
        extract_summary "$DATA"
    ') || true
    assert_contains "$result" "total=50000" "total 应为 50000"
    assert_contains "$result" "l1_miss=5000" "l1_miss 应为 5000"
    assert_contains "$result" "llc_miss=1000" "llc_miss 应为 1000"
}

# =============================================================================
# 测试组: compute_diff - 改进场景
# =============================================================================

test_diff_improved_l1_miss() {
    blue "测试: diff 计算 - L1 miss 改进"
    local result
    result=$(bash -c '
        calc_change() {
            local before_val="$1" after_val="$2" label="$3"
            local diff
            diff=$(echo "$after_val $before_val" | awk "{printf \"%.1f\", \$1 - \$2}")
            local pct
            if [ "$(echo "$before_val" | awk "{printf \"%.1f\", \$1}")" != "0.0" ]; then
                pct=$(echo "$after_val $before_val" | awk "{printf \"%+.1f\", (\$1-\$2)/\$2*100}")
            else
                pct="N/A"
            fi
            local marker
            if [ "$(echo "$diff" | awk "{printf \"%.1f\", \$1}")" != "0.0" ]; then
                if [ "$(echo "$diff" | awk "{print (\$1 < 0) ? 1 : 0}")" = "1" ]; then
                    marker="[IMPROVED]"
                else
                    marker="[REGRESSED]"
                fi
            else
                marker="[NO CHANGE]"
            fi
            printf "%s %s %s %s %s %s\n" "$label" "$before_val" "$after_val" "$diff" "$pct" "$marker"
        }
        calc_change "5000" "2500" "L1 miss"
    ') || true
    assert_contains "$result" "2500" "after 值应为 2500"
    assert_contains "$result" "IMPROVED" "减少 50% 应标记为 IMPROVED"
}

test_diff_regression_llc_miss() {
    blue "测试: diff 计算 - LLC miss 回归"
    local result
    result=$(bash -c '
        calc_change() {
            local before_val="$1" after_val="$2" label="$3"
            local diff
            diff=$(echo "$after_val $before_val" | awk "{printf \"%.1f\", \$1 - \$2}")
            local pct
            if [ "$(echo "$before_val" | awk "{printf \"%.1f\", \$1}")" != "0.0" ]; then
                pct=$(echo "$after_val $before_val" | awk "{printf \"%+.1f\", (\$1-\$2)/\$2*100}")
            else
                pct="N/A"
            fi
            local marker
            if [ "$(echo "$diff" | awk "{printf \"%.1f\", \$1}")" != "0.0" ]; then
                if [ "$(echo "$diff" | awk "{print (\$1 < 0) ? 1 : 0}")" = "1" ]; then
                    marker="[IMPROVED]"
                else
                    marker="[REGRESSED]"
                fi
            else
                marker="[NO CHANGE]"
            fi
            printf "%s %s %s %s %s %s\n" "$label" "$before_val" "$after_val" "$diff" "$pct" "$marker"
        }
        calc_change "500" "750" "LLC miss"
    ') || true
    assert_contains "$result" "REGRESSED" "增加应标记为 REGRESSED"
}

test_diff_no_change() {
    blue "测试: diff 计算 - 无变化"
    local result
    result=$(bash -c '
        calc_change() {
            local before_val="$1" after_val="$2" label="$3"
            local diff
            diff=$(echo "$after_val $before_val" | awk "{printf \"%.1f\", \$1 - \$2}")
            local pct
            if [ "$(echo "$before_val" | awk "{printf \"%.1f\", \$1}")" != "0.0" ]; then
                pct=$(echo "$after_val $before_val" | awk "{printf \"%+.1f\", (\$1-\$2)/\$2*100}")
            else
                pct="N/A"
            fi
            local marker
            if [ "$(echo "$diff" | awk "{printf \"%.1f\", \$1}")" != "0.0" ]; then
                if [ "$(echo "$diff" | awk "{print (\$1 < 0) ? 1 : 0}")" = "1" ]; then
                    marker="[IMPROVED]"
                else
                    marker="[REGRESSED]"
                fi
            else
                marker="[NO CHANGE]"
            fi
            printf "%s %s %s %s %s %s\n" "$label" "$before_val" "$after_val" "$diff" "$pct" "$marker"
        }
        calc_change "1000" "1000" "Branch miss"
    ') || true
    assert_contains "$result" "NO CHANGE" "值不变应标记为 NO CHANGE"
}

# =============================================================================
# 测试组: 错误处理
# =============================================================================

test_missing_parse_script() {
    blue "测试: spe-parse.sh 不存在"
    local tmpdir="$TEST_RESULTS_DIR/no_parse_compare"
    rm -rf "$tmpdir"
    mkdir -p "$tmpdir"
    cp "$SCRIPT" "$tmpdir/spe-compare.sh"
    chmod +x "$tmpdir/spe-compare.sh"
    touch "$tmpdir/before.data" "$tmpdir/after.data"

    local exit_code=0
    local output
    output=$(bash "$tmpdir/spe-compare.sh" "$tmpdir/before.data" "$tmpdir/after.data" 2>&1) || exit_code=$?
    assert_contains "$output" "找不到 spe-parse.sh" "应报告缺少 spe-parse.sh"
    assert_exit_code 1 "$exit_code" "应返回退出码 1"
    rm -rf "$tmpdir"
}

# =============================================================================
# 测试组: 真实 SPE 环境
# =============================================================================

test_real_comparison() {
    blue "测试: 真实 SPE 数据对比"

    if ! command -v perf &>/dev/null; then
        yellow "  [SKIP] perf 不可用"; return
    fi
    if [ ! -d /sys/bus/event_source/devices/arm_spe_0 ]; then
        yellow "  [SKIP] 非 ARM SPE 环境"; return
    fi

    local before_data="/tmp/spe-test-compare-before.data"
    local after_data="/tmp/spe-test-compare-after.data"

    if ! perf record -e arm_spe_0// -o "$before_data" -a -- sleep 1 >/dev/null 2>&1; then
        yellow "  [SKIP] SPE 采集失败"; return
    fi
    sleep 1
    if ! perf record -e arm_spe_0// -o "$after_data" -a -- sleep 1 >/dev/null 2>&1; then
        yellow "  [SKIP] 第二次采集失败"; rm -f "$before_data"; return
    fi

    local output
    output=$(bash "$SCRIPT" "$before_data" "$after_data" 2>&1) || true
    assert_contains "$output" "对比报告" "应显示对比报告"
    assert_contains "$output" "[IMPROVED]" "应包含判定标记"

    rm -f "$before_data" "$after_data"
}

# =============================================================================
# 主测试运行器
# =============================================================================

run_all_tests() {
    echo "========================================================================="
    echo "spe-compare.sh 测试套件"
    echo "========================================================================="
    echo ""

    blue "===== 测试组: 参数解析 ====="
    echo ""
    test_help_short
    test_help_long
    test_missing_files
    test_missing_second_file
    test_nonexistent_file
    test_extra_args
    test_unknown_option
    echo ""

    blue "===== 测试组: extract_summary ====="
    echo ""
    test_extract_summary_current_format
    echo ""

    blue "===== 测试组: diff 计算 ====="
    echo ""
    test_diff_improved_l1_miss
    test_diff_regression_llc_miss
    test_diff_no_change
    echo ""

    blue "===== 测试组: 错误处理 ====="
    echo ""
    test_missing_parse_script
    echo ""

    blue "===== 测试组: 真实 SPE 环境 ====="
    echo ""
    test_real_comparison
    echo ""

    echo "========================================================================="
    echo "测试摘要"
    echo "========================================================================="
    echo "  通过: $TEST_PASSED"
    echo "  失败: $TEST_FAILED"
    echo "  总计: $((TEST_PASSED + TEST_FAILED))"
    echo ""

    if [ "$TEST_FAILED" -eq 0 ]; then
        green "所有测试通过!"
        return 0
    else
        red "部分测试失败。"
        return 1
    fi
}

cleanup() {
    rm -rf "$MOCK_DIR" 2>/dev/null || true
    rm -rf "$TEST_RESULTS_DIR/no_parse_compare" 2>/dev/null || true
    rm -f /tmp/spe-test-compare-before.data /tmp/spe-test-compare-after.data 2>/dev/null || true
}

SKIP_CLEANUP=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-cleanup) SKIP_CLEANUP=true; shift ;;
        *) echo "未知选项: $1"; exit 1 ;;
    esac
done

trap cleanup EXIT INT TERM

if run_all_tests; then
    if [ "$SKIP_CLEANUP" = false ]; then cleanup; fi
    exit 0
else
    if [ "$SKIP_CLEANUP" = false ]; then cleanup; fi
    exit 1
fi
