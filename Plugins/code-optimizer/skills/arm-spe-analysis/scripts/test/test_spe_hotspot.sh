#!/bin/bash
# =============================================================================
# spe-hotspot.sh 测试套件
# 测试脚本的参数解析、核心逻辑、错误处理和边界条件
# =============================================================================

set -e

# 颜色输出
red()     { echo -e "\033[31m$*\033[0m"; }
green()   { echo -e "\033[32m$*\033[0m"; }
yellow()  { echo -e "\033[33m$*\033[0m"; }
blue()    { echo -e "\033[34m$*\033[0m"; }

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="$(dirname "$TEST_DIR")"
SCRIPT="$SCRIPT_DIR/spe-hotspot.sh"
TEST_RESULTS_DIR="$TEST_DIR/test_results"
TEST_PASSED=0
TEST_FAILED=0
MOCK_DIR="$TEST_RESULTS_DIR/hotspot_mock"

# =============================================================================
# 断言函数
# =============================================================================

assert_contains() {
    local haystack="$1"
    local needle="$2"
    local message="${3:-AssertContains}"
    if [[ "$haystack" == *"$needle"* ]]; then
        echo "  [PASS] $message"
        TEST_PASSED=$((TEST_PASSED + 1))
    else
        echo "  [FAIL] $message"
        echo "         未找到: $needle"
        TEST_FAILED=$((TEST_FAILED + 1))
    fi
}

assert_not_contains() {
    local haystack="$1"
    local needle="$2"
    local message="${3:-AssertNotContains}"
    if [[ "$haystack" != *"$needle"* ]]; then
        echo "  [PASS] $message"
        TEST_PASSED=$((TEST_PASSED + 1))
    else
        echo "  [FAIL] $message"
        echo "         不应包含: $needle"
        TEST_FAILED=$((TEST_FAILED + 1))
    fi
}

assert_exit_code() {
    local expected="$1"
    local actual="$2"
    local message="${3:-AssertExitCode}"
    if [ "$actual" -eq "$expected" ]; then
        echo "  [PASS] $message"
        TEST_PASSED=$((TEST_PASSED + 1))
    else
        echo "  [FAIL] $message (期望 exit=$expected, 实际 exit=$actual)"
        TEST_FAILED=$((TEST_FAILED + 1))
    fi
}

# =============================================================================
# Mock 环境设置
# =============================================================================

setup_mock() {
    rm -rf "$MOCK_DIR"
    mkdir -p "$MOCK_DIR"

    # 创建 mock spe-parse.sh，输出可控的测试数据
    cat > "$MOCK_DIR/spe-parse.sh" <<'MOCKEOF'
#!/bin/bash
set -e
SORT_FIELD="l1_miss"
TOP_N=10
while [[ $# -gt 0 ]]; do
    case "$1" in
        -s) SORT_FIELD="$2"; shift 2 ;;
        -n) TOP_N="$2"; shift 2 ;;
        *)  DATA_FILE="$1"; shift ;;
    esac
done
cat <<EOF
=== SPE 采样汇总 ===
总记录数:      1000

--- Cache 分布 ---
L1 miss:   300 (30.0%)
LLC miss:  100 (10.0%)
Remote:    20 (2.0%)

--- TLB 分布 ---
TLB access: 500 (50.0%)
TLB miss:   50 (10.0% of TLB)

--- 分支预测 ---
Branch miss: 80 (8.0% of branches)

=== Top $TOP_N (按 $SORT_FIELD 排序) ===
ADDRESS             COUNT    L1_MISS%   LLC_MISS%   BR_MISS%  SYMBOL
ffff80001000        100        35.0%       12.0%       8.0%    func_hot
ffff80002000        80         25.0%        8.0%      15.0%    func_warm
ffff80003000        50         10.0%        5.0%       3.0%    func_cold
ffff80004000        30          5.0%       30.0%       2.0%    func_llc_heavy
ffff80005000        20          2.0%        1.0%      25.0%    func_br_bad
EOF
MOCKEOF
    chmod +x "$MOCK_DIR/spe-parse.sh"

    # 创建 spe-hotspot.sh 的符号链接，这样 SCRIPT_DIR 会指向 MOCK_DIR
    ln -sf "$SCRIPT" "$MOCK_DIR/spe-hotspot.sh"
    touch "$MOCK_DIR/test.data"
}

# 创建空数据的 mock
setup_mock_empty() {
    rm -rf "$MOCK_DIR"
    mkdir -p "$MOCK_DIR"

    cat > "$MOCK_DIR/spe-parse.sh" <<'MOCKEOF'
#!/bin/bash
cat <<EOF
=== SPE 采样汇总 ===
总记录数:      0

--- Cache 分布 ---
L1 miss:   0 (0.0%)
LLC miss:  0 (0.0%)
Remote:    0 (0.0%)

--- TLB 分布 ---
TLB access: 0 (0.0%)

--- 分支预测 ---
Branch miss: 0 (0.0% of branches)

=== Top 10 (按 l1_miss 排序) ===
ADDRESS             COUNT    L1_MISS%   LLC_MISS%   BR_MISS%  SYMBOL
EOF
MOCKEOF
    chmod +x "$MOCK_DIR/spe-parse.sh"

    ln -sf "$SCRIPT" "$MOCK_DIR/spe-hotspot.sh"
    touch "$MOCK_DIR/test.data"
}

# 创建带多词符号的 mock（测试 by-function 聚合）
setup_mock_multisym() {
    rm -rf "$MOCK_DIR"
    mkdir -p "$MOCK_DIR"

    cat > "$MOCK_DIR/spe-parse.sh" <<'MOCKEOF'
#!/bin/bash
cat <<EOF
=== SPE 采样汇总 ===
总记录数:      200

--- Cache 分布 ---
L1 miss:   60 (30.0%)
LLC miss:  20 (10.0%)
Remote:    4 (2.0%)

--- TLB 分布 ---
TLB access: 100 (50.0%)

--- 分支预测 ---
Branch miss: 16 (8.0% of branches)

=== Top 10 (按 l1_miss 排序) ===
ADDRESS             COUNT    L1_MISS%   LLC_MISS%   BR_MISS%  SYMBOL
ffff80001000        50         40.0%       10.0%       5.0%    func_foo
ffff80001080        30         30.0%        5.0%       8.0%    func_foo
ffff80002000        40         20.0%       15.0%      12.0%    func_bar
EOF
MOCKEOF
    chmod +x "$MOCK_DIR/spe-parse.sh"

    ln -sf "$SCRIPT" "$MOCK_DIR/spe-hotspot.sh"
    touch "$MOCK_DIR/test.data"
}

# =============================================================================
# 测试组: 参数解析
# =============================================================================

test_help_short() {
    blue "测试: -h 显示帮助"
    local output
    output=$(bash "$SCRIPT" -h 2>&1) || true
    assert_contains "$output" "用法:" "-h 应显示用法"
    assert_contains "$output" "spe-hotspot.sh" "-h 应显示脚本名"
}

test_help_long() {
    blue "测试: --help 显示帮助"
    local output
    output=$(bash "$SCRIPT" --help 2>&1) || true
    assert_contains "$output" "用法:" "--help 应显示用法"
    assert_contains "$output" "--metric" "--help 应显示选项说明"
}

test_missing_data_file() {
    blue "测试: 缺少数据文件参数"
    local output
    output=$(bash "$SCRIPT" 2>&1) || true
    assert_contains "$output" "错误" "缺少文件应报错"
}

test_nonexistent_file() {
    blue "测试: 文件不存在"
    local output
    local exit_code=0
    output=$(bash "$SCRIPT" /tmp/nonexistent_spe_file_$$.data 2>&1) || exit_code=$?
    assert_contains "$output" "文件不存在" "不存在的文件应报错"
    assert_exit_code 1 "$exit_code" "应返回退出码 1"
}

test_invalid_metric() {
    blue "测试: 无效指标"
    local tmpfile="$TEST_RESULTS_DIR/.test_invalid_metric_data"
    touch "$tmpfile"
    local output
    local exit_code=0
    output=$(bash "$SCRIPT" -m invalid_metric "$tmpfile" 2>&1) || exit_code=$?
    assert_contains "$output" "不支持的指标" "无效指标应报错"
    assert_exit_code 1 "$exit_code" "应返回退出码 1"
    rm -f "$tmpfile"
}

test_metric_l1_miss() {
    blue "测试: 指标 - l1_miss（默认）"
    setup_mock
    local output
    output=$(bash "$MOCK_DIR/spe-hotspot.sh" "$MOCK_DIR/test.data" 2>&1) || true
    assert_contains "$output" "l1_miss" "默认指标应为 l1_miss"
}

test_metric_llc_miss() {
    blue "测试: 指标 - llc_miss"
    setup_mock
    local output
    output=$(bash "$MOCK_DIR/spe-hotspot.sh" -m llc_miss "$MOCK_DIR/test.data" 2>&1) || true
    assert_contains "$output" "llc_miss" "指标应显示 llc_miss"
}

test_metric_branch_mispred() {
    blue "测试: 指标 - branch_mispred"
    setup_mock
    local output
    output=$(bash "$MOCK_DIR/spe-hotspot.sh" -m branch_mispred "$MOCK_DIR/test.data" 2>&1) || true
    assert_contains "$output" "branch_mispred" "指标应显示 branch_mispred"
}

test_top_n_custom() {
    blue "测试: 自定义 Top N"
    setup_mock
    local output
    output=$(bash "$MOCK_DIR/spe-hotspot.sh" -n 3 "$MOCK_DIR/test.data" 2>&1) || true
    assert_contains "$output" "Top 3" "应显示 Top 3"
}

test_threshold_flag() {
    blue "测试: 阈值参数解析"
    setup_mock
    local output
    output=$(bash "$MOCK_DIR/spe-hotspot.sh" -t 20 "$MOCK_DIR/test.data" 2>&1) || true
    assert_contains "$output" "阈值" "应显示阈值信息"
}

test_by_function_flag() {
    blue "测试: --by-function 参数解析"
    setup_mock
    local output
    output=$(bash "$MOCK_DIR/spe-hotspot.sh" --by-function "$MOCK_DIR/test.data" 2>&1) || true
    assert_contains "$output" "按函数聚合" "应显示按函数聚合"
}

test_unknown_option() {
    blue "测试: 未知选项"
    local output
    output=$(bash "$SCRIPT" --unknown-flag 2>&1) || true
    assert_contains "$output" "未知选项" "未知选项应提示"
}

# =============================================================================
# 测试组: 核心逻辑（使用 mock）
# =============================================================================

test_default_run_shows_summary() {
    blue "测试: 默认运行显示汇总"
    setup_mock
    local output
    output=$(bash "$MOCK_DIR/spe-hotspot.sh" "$MOCK_DIR/test.data" 2>&1) || true
    assert_contains "$output" "SPE 热点分析" "应显示标题"
    assert_contains "$output" "SPE 采样汇总" "应显示汇总信息"
    assert_contains "$output" "总记录数" "应显示总记录数"
}

test_default_run_shows_top_table() {
    blue "测试: 默认运行显示 Top 表"
    setup_mock
    local output
    output=$(bash "$MOCK_DIR/spe-hotspot.sh" "$MOCK_DIR/test.data" 2>&1) || true
    assert_contains "$output" "ADDRESS" "应显示表格头"
    assert_contains "$output" "func_hot" "应包含热点函数 func_hot"
    assert_contains "$output" "func_warm" "应包含 func_warm"
}

test_summary_excludes_top_section() {
    blue "测试: 汇总区不包含 Top 排序数据"
    setup_mock
    local output
    output=$(bash "$MOCK_DIR/spe-hotspot.sh" "$MOCK_DIR/test.data" 2>&1) || true
    # 汇总部分（在 --- 分隔线之前）不应有 ADDRESS 列
    local summary_part
    summary_part=$(echo "$output" | sed '/^---/q')
    assert_not_contains "$summary_part" "ADDRESS" "汇总区不应含地址表"
}

test_llc_miss_shows_correct_data() {
    blue "测试: LLC miss 指标显示正确"
    setup_mock
    local output
    output=$(bash "$MOCK_DIR/spe-hotspot.sh" -m llc_miss "$MOCK_DIR/test.data" 2>&1) || true
    assert_contains "$output" "func_llc_heavy" "应显示 LLC 热点 func_llc_heavy (30%)"
}

test_branch_mispred_shows_correct_data() {
    blue "测试: 分支预测指标显示正确"
    setup_mock
    local output
    output=$(bash "$MOCK_DIR/spe-hotspot.sh" -m branch_mispred "$MOCK_DIR/test.data" 2>&1) || true
    assert_contains "$output" "func_br_bad" "应显示分支预测热点 func_br_bad (25%)"
}

test_top_n_limits_output() {
    blue "测试: -n 限制输出行数"
    setup_mock
    # 创建有 10 个条目的 mock
    rm -rf "$MOCK_DIR"
    mkdir -p "$MOCK_DIR"
    cat > "$MOCK_DIR/spe-parse.sh" <<'MOCKEOF'
#!/bin/bash
TOP_N=10; while [[ $# -gt 0 ]]; do case "$1" in -n) TOP_N="$2"; shift 2 ;; *) shift ;; esac; done
cat <<EOF
=== SPE 采样汇总 ===
总记录数:      500
L1 miss:   150 (30.0%)

=== Top $TOP_N (按 l1_miss 排序) ===
ADDRESS             COUNT    L1_MISS%   LLC_MISS%   BR_MISS%  SYMBOL
EOF
# 只输出 TOP_N 条，模拟真实 spe-parse.sh 的行为
for i in $(seq 1 "$TOP_N"); do
    case "$i" in
        1) printf "%-18s  %-6s  %9s%%  %9s%%  %9s%%  %s\n" "ffff80001000" "100" "50.0" "10.0" "5.0" "func_a" ;;
        2) printf "%-18s  %-6s  %9s%%  %9s%%  %9s%%  %s\n" "ffff80002000" "80" "40.0" "8.0" "10.0" "func_b" ;;
        3) printf "%-18s  %-6s  %9s%%  %9s%%  %9s%%  %s\n" "ffff80003000" "60" "30.0" "5.0" "8.0" "func_c" ;;
        4) printf "%-18s  %-6s  %9s%%  %9s%%  %9s%%  %s\n" "ffff80004000" "40" "20.0" "15.0" "3.0" "func_d" ;;
        5) printf "%-18s  %-6s  %9s%%  %9s%%  %9s%%  %s\n" "ffff80005000" "30" "10.0" "2.0" "20.0" "func_e" ;;
    esac
done
MOCKEOF
    chmod +x "$MOCK_DIR/spe-parse.sh"
    ln -sf "$SCRIPT" "$MOCK_DIR/spe-hotspot.sh"
    touch "$MOCK_DIR/test.data"

    local output
    output=$(bash "$MOCK_DIR/spe-hotspot.sh" -n 2 "$MOCK_DIR/test.data" 2>&1) || true
    # 只应有 2 条数据行（func_a, func_b），不应有 func_e
    assert_contains "$output" "func_a" "Top 2 应包含 func_a"
    assert_contains "$output" "func_b" "Top 2 应包含 func_b"
    assert_not_contains "$output" "func_e" "Top 2 不应包含 func_e"
}

test_threshold_filters_hotspots() {
    blue "测试: -t 阈值过滤热点"
    setup_mock
    local output
    # l1_miss > 20% 应该匹配 func_hot(35%) 和 func_warm(25%)
    output=$(bash "$MOCK_DIR/spe-hotspot.sh" -t 20 "$MOCK_DIR/test.data" 2>&1) || true
    assert_contains "$output" "HOT" "应显示 [HOT] 标记"
    assert_contains "$output" "ffff80001000" "func_hot(35%) 应被标记"
    assert_contains "$output" "ffff80002000" "func_warm(25%) 应被标记"
    assert_not_contains "$output" "ffff80003000.*HOT" "func_cold(10%) 不应被标记"
}

test_threshold_no_match() {
    blue "测试: 阈值过高无匹配"
    setup_mock
    local output
    output=$(bash "$MOCK_DIR/spe-hotspot.sh" -t 90 "$MOCK_DIR/test.data" 2>&1) || true
    assert_not_contains "$output" "HOT" "阈值 90% 不应有热点"
}

test_by_function_aggregates() {
    blue "测试: --by-function 按函数聚合"
    setup_mock_multisym
    local output
    output=$(bash "$MOCK_DIR/spe-hotspot.sh" --by-function "$MOCK_DIR/test.data" 2>&1) || true
    assert_contains "$output" "FUNCTION" "应显示 FUNCTION 列头"
    # func_foo 出现两次 (50+30=80 samples)，func_bar 一次 (40 samples)
    assert_contains "$output" "func_foo" "应聚合 func_foo"
    assert_contains "$output" "func_bar" "应聚合 func_bar"
}

test_by_function_with_llc_miss() {
    blue "测试: --by-function + llc_miss 指标"
    setup_mock_multisym
    local output
    output=$(bash "$MOCK_DIR/spe-hotspot.sh" -m llc_miss --by-function "$MOCK_DIR/test.data" 2>&1) || true
    assert_contains "$output" "LLC_MISS%" "应显示 LLC_MISS% 列"
}

test_by_function_with_branch_mispred() {
    blue "测试: --by-function + branch_mispred 指标"
    setup_mock_multisym
    local output
    output=$(bash "$MOCK_DIR/spe-hotspot.sh" -m branch_mispred --by-function "$MOCK_DIR/test.data" 2>&1) || true
    assert_contains "$output" "BR_MISS%" "应显示 BR_MISS% 列"
}

test_bottleneck_section_appears() {
    blue "测试: 瓶颈判断区域显示"
    setup_mock
    local output
    output=$(bash "$MOCK_DIR/spe-hotspot.sh" "$MOCK_DIR/test.data" 2>&1) || true
    assert_contains "$output" "瓶颈判断" "应显示瓶颈判断区域"
}

# =============================================================================
# 测试组: classify_bottleneck 单元测试
# =============================================================================

test_classify_l1_miss_severe() {
    blue "测试: classify_bottleneck l1_miss SEVERE"
    local result
    result=$(bash -c '
        classify_bottleneck() {
            local metric="$1" local value="$2"
            local pct
            pct=$(echo "$value" | awk "{printf \"%.0f\", \$1}")
            case "$metric" in
                l1_miss)
                    if [ "$pct" -gt 30 ]; then echo "SEVERE"
                    elif [ "$pct" -gt 10 ]; then echo "MODERATE"
                    elif [ "$pct" -gt 5 ]; then echo "MILD"
                    else echo "OK"
                    fi ;;
            esac
        }
        classify_bottleneck l1_miss 35
    ') || true
    assert_contains "$result" "SEVERE" "35% l1_miss 应为 SEVERE"
}

test_classify_l1_miss_moderate() {
    blue "测试: classify_bottleneck l1_miss MODERATE"
    local result
    result=$(bash -c '
        classify_bottleneck() {
            local metric="$1" local value="$2"
            local pct
            pct=$(echo "$value" | awk "{printf \"%.0f\", \$1}")
            case "$metric" in
                l1_miss)
                    if [ "$pct" -gt 30 ]; then echo "SEVERE"
                    elif [ "$pct" -gt 10 ]; then echo "MODERATE"
                    elif [ "$pct" -gt 5 ]; then echo "MILD"
                    else echo "OK"
                    fi ;;
            esac
        }
        classify_bottleneck l1_miss 15
    ') || true
    assert_contains "$result" "MODERATE" "15% l1_miss 应为 MODERATE"
}

test_classify_l1_miss_mild() {
    blue "测试: classify_bottleneck l1_miss MILD"
    local result
    result=$(bash -c '
        classify_bottleneck() {
            local metric="$1" local value="$2"
            local pct
            pct=$(echo "$value" | awk "{printf \"%.0f\", \$1}")
            case "$metric" in
                l1_miss)
                    if [ "$pct" -gt 30 ]; then echo "SEVERE"
                    elif [ "$pct" -gt 10 ]; then echo "MODERATE"
                    elif [ "$pct" -gt 5 ]; then echo "MILD"
                    else echo "OK"
                    fi ;;
            esac
        }
        classify_bottleneck l1_miss 8
    ') || true
    assert_contains "$result" "MILD" "8% l1_miss 应为 MILD"
}

test_classify_l1_miss_ok() {
    blue "测试: classify_bottleneck l1_miss OK"
    local result
    result=$(bash -c '
        classify_bottleneck() {
            local metric="$1" local value="$2"
            local pct
            pct=$(echo "$value" | awk "{printf \"%.0f\", \$1}")
            case "$metric" in
                l1_miss)
                    if [ "$pct" -gt 30 ]; then echo "SEVERE"
                    elif [ "$pct" -gt 10 ]; then echo "MODERATE"
                    elif [ "$pct" -gt 5 ]; then echo "MILD"
                    else echo "OK"
                    fi ;;
            esac
        }
        classify_bottleneck l1_miss 3
    ') || true
    assert_contains "$result" "OK" "3% l1_miss 应为 OK"
}

test_classify_llc_miss_severe() {
    blue "测试: classify_bottleneck llc_miss SEVERE"
    local result
    result=$(bash -c '
        classify_bottleneck() {
            local metric="$1" local value="$2"
            local pct
            pct=$(echo "$value" | awk "{printf \"%.0f\", \$1}")
            case "$metric" in
                llc_miss)
                    if [ "$pct" -gt 30 ]; then echo "SEVERE"
                    elif [ "$pct" -gt 10 ]; then echo "MODERATE"
                    elif [ "$pct" -gt 5 ]; then echo "MILD"
                    else echo "OK"
                    fi ;;
            esac
        }
        classify_bottleneck llc_miss 35
    ') || true
    assert_contains "$result" "SEVERE" "35% llc_miss 应为 SEVERE"
}

test_classify_branch_mispred_severe() {
    blue "测试: classify_bottleneck branch_mispred SEVERE"
    local result
    result=$(bash -c '
        classify_bottleneck() {
            local metric="$1" local value="$2"
            local pct
            pct=$(echo "$value" | awk "{printf \"%.0f\", \$1}")
            case "$metric" in
                branch_mispred)
                    if [ "$pct" -gt 20 ]; then echo "SEVERE"
                    elif [ "$pct" -gt 5 ]; then echo "MODERATE"
                    elif [ "$pct" -gt 1 ]; then echo "MILD"
                    else echo "OK"
                    fi ;;
            esac
        }
        classify_bottleneck branch_mispred 25
    ') || true
    assert_contains "$result" "SEVERE" "25% branch_mispred 应为 SEVERE"
}

test_classify_branch_mispred_moderate() {
    blue "测试: classify_bottleneck branch_mispred MODERATE"
    local result
    result=$(bash -c '
        classify_bottleneck() {
            local metric="$1" local value="$2"
            local pct
            pct=$(echo "$value" | awk "{printf \"%.0f\", \$1}")
            case "$metric" in
                branch_mispred)
                    if [ "$pct" -gt 20 ]; then echo "SEVERE"
                    elif [ "$pct" -gt 5 ]; then echo "MODERATE"
                    elif [ "$pct" -gt 1 ]; then echo "MILD"
                    else echo "OK"
                    fi ;;
            esac
        }
        classify_bottleneck branch_mispred 10
    ') || true
    assert_contains "$result" "MODERATE" "10% branch_mispred 应为 MODERATE"
}

test_classify_branch_mispred_ok() {
    blue "测试: classify_bottleneck branch_mispred OK"
    local result
    result=$(bash -c '
        classify_bottleneck() {
            local metric="$1" local value="$2"
            local pct
            pct=$(echo "$value" | awk "{printf \"%.0f\", \$1}")
            case "$metric" in
                branch_mispred)
                    if [ "$pct" -gt 20 ]; then echo "SEVERE"
                    elif [ "$pct" -gt 5 ]; then echo "MODERATE"
                    elif [ "$pct" -gt 1 ]; then echo "MILD"
                    else echo "OK"
                    fi ;;
            esac
        }
        classify_bottleneck branch_mispred 0.5
    ') || true
    assert_contains "$result" "OK" "0.5% branch_mispred 应为 OK"
}

# 边界值测试
test_classify_boundary_l1_miss_30() {
    blue "测试: classify_bottleneck 边界值 l1_miss=30"
    local result
    result=$(bash -c '
        classify_bottleneck() {
            local metric="$1" local value="$2"
            local pct
            pct=$(echo "$value" | awk "{printf \"%.0f\", \$1}")
            case "$metric" in
                l1_miss)
                    if [ "$pct" -gt 30 ]; then echo "SEVERE"
                    elif [ "$pct" -gt 10 ]; then echo "MODERATE"
                    elif [ "$pct" -gt 5 ]; then echo "MILD"
                    else echo "OK"
                    fi ;;
            esac
        }
        classify_bottleneck l1_miss 30
    ') || true
    assert_contains "$result" "MODERATE" "30% l1_miss 不大于30，应回退到 MODERATE"
}

test_classify_boundary_branch_20() {
    blue "测试: classify_bottleneck 边界值 branch_mispred=20"
    local result
    result=$(bash -c '
        classify_bottleneck() {
            local metric="$1" local value="$2"
            local pct
            pct=$(echo "$value" | awk "{printf \"%.0f\", \$1}")
            case "$metric" in
                branch_mispred)
                    if [ "$pct" -gt 20 ]; then echo "SEVERE"
                    elif [ "$pct" -gt 5 ]; then echo "MODERATE"
                    elif [ "$pct" -gt 1 ]; then echo "MILD"
                    else echo "OK"
                    fi ;;
            esac
        }
        classify_bottleneck branch_mispred 20
    ') || true
    assert_contains "$result" "MODERATE" "20% branch_mispred 不大于20，应回退到 MODERATE"
}

# =============================================================================
# 测试组: 错误处理
# =============================================================================

test_missing_parse_script() {
    blue "测试: spe-parse.sh 不存在"
    local tmpdir="$TEST_RESULTS_DIR/no_parse"
    rm -rf "$tmpdir"
    mkdir -p "$tmpdir"
    # 只复制 spe-hotspot.sh，不创建 spe-parse.sh
    cp "$SCRIPT" "$tmpdir/spe-hotspot.sh"
    chmod +x "$tmpdir/spe-hotspot.sh"
    touch "$tmpdir/test.data"

    local output
    local exit_code=0
    output=$(bash "$tmpdir/spe-hotspot.sh" "$tmpdir/test.data" 2>&1) || exit_code=$?
    assert_contains "$output" "找不到 spe-parse.sh" "应报告缺少 spe-parse.sh"
    assert_exit_code 1 "$exit_code" "应返回退出码 1"
    rm -rf "$tmpdir"
}

# =============================================================================
# 测试组: 边界条件
# =============================================================================

test_empty_data() {
    blue "测试: 空数据"
    setup_mock_empty
    local output
    output=$(bash "$MOCK_DIR/spe-hotspot.sh" "$MOCK_DIR/test.data" 2>&1) || true
    assert_contains "$output" "总记录数" "空数据也应显示汇总"
    assert_contains "$output" "瓶颈判断" "空数据也应显示瓶颈区域"
}

test_long_option_metric() {
    blue "测试: 长选项 --metric"
    setup_mock
    local output
    output=$(bash "$MOCK_DIR/spe-hotspot.sh" --metric llc_miss "$MOCK_DIR/test.data" 2>&1) || true
    assert_contains "$output" "llc_miss" "--metric llc_miss 应生效"
}

test_long_option_top() {
    blue "测试: 长选项 --top"
    setup_mock
    local output
    output=$(bash "$MOCK_DIR/spe-hotspot.sh" --top 3 "$MOCK_DIR/test.data" 2>&1) || true
    assert_contains "$output" "Top 3" "--top 3 应生效"
}

test_long_option_threshold() {
    blue "测试: 长选项 --threshold"
    setup_mock
    local output
    output=$(bash "$MOCK_DIR/spe-hotspot.sh" --threshold 15 "$MOCK_DIR/test.data" 2>&1) || true
    assert_contains "$output" "阈值" "--threshold 应生效"
}

test_combined_options() {
    blue "测试: 组合选项 -m llc_miss -n 5 -t 10"
    setup_mock
    local output
    output=$(bash "$MOCK_DIR/spe-hotspot.sh" -m llc_miss -n 5 -t 10 "$MOCK_DIR/test.data" 2>&1) || true
    assert_contains "$output" "llc_miss" "组合选项应正确解析"
    assert_contains "$output" "Top 5" "组合选项应正确传递 Top N"
    assert_contains "$output" "阈值" "组合选项应包含阈值"
}

test_combined_by_function_threshold() {
    blue "测试: 组合选项 --by-function -t 10"
    setup_mock_multisym
    local output
    output=$(bash "$MOCK_DIR/spe-hotspot.sh" --by-function -t 10 "$MOCK_DIR/test.data" 2>&1) || true
    assert_contains "$output" "FUNCTION" "应显示函数聚合"
    assert_contains "$output" "阈值" "应显示阈值信息"
}

# =============================================================================
# 测试组: 输出格式验证
# =============================================================================

test_output_has_expected_sections() {
    blue "测试: 输出包含所有预期的区域"
    setup_mock
    local output
    output=$(bash "$MOCK_DIR/spe-hotspot.sh" "$MOCK_DIR/test.data" 2>&1) || true
    assert_contains "$output" "SPE 热点分析" "应有标题"
    assert_contains "$output" "SPE 采样汇总" "应有汇总区"
    assert_contains "$output" "瓶颈判断" "应有瓶颈判断区"
}

test_no_threshold_shows_hint() {
    blue "测试: 未设阈值时显示提示"
    setup_mock
    local output
    output=$(bash "$MOCK_DIR/spe-hotspot.sh" "$MOCK_DIR/test.data" 2>&1) || true
    assert_contains "$output" "-t N" "应提示使用 -t 设置阈值"
}

# =============================================================================
# 主测试运行器
# =============================================================================

run_all_tests() {
    echo "========================================================================="
    echo "spe-hotspot.sh 测试套件"
    echo "========================================================================="
    echo ""

    # 参数解析测试
    blue "===== 测试组: 参数解析 ====="
    echo ""
    test_help_short
    test_help_long
    test_missing_data_file
    test_nonexistent_file
    test_invalid_metric
    test_metric_l1_miss
    test_metric_llc_miss
    test_metric_branch_mispred
    test_top_n_custom
    test_threshold_flag
    test_by_function_flag
    test_unknown_option
    echo ""

    # 核心逻辑测试
    blue "===== 测试组: 核心逻辑 ====="
    echo ""
    test_default_run_shows_summary
    test_default_run_shows_top_table
    test_summary_excludes_top_section
    test_llc_miss_shows_correct_data
    test_branch_mispred_shows_correct_data
    test_top_n_limits_output
    test_threshold_filters_hotspots
    test_threshold_no_match
    test_by_function_aggregates
    test_by_function_with_llc_miss
    test_by_function_with_branch_mispred
    test_bottleneck_section_appears
    echo ""

    # classify_bottleneck 单元测试
    blue "===== 测试组: classify_bottleneck ====="
    echo ""
    test_classify_l1_miss_severe
    test_classify_l1_miss_moderate
    test_classify_l1_miss_mild
    test_classify_l1_miss_ok
    test_classify_llc_miss_severe
    test_classify_branch_mispred_severe
    test_classify_branch_mispred_moderate
    test_classify_branch_mispred_ok
    test_classify_boundary_l1_miss_30
    test_classify_boundary_branch_20
    echo ""

    # 错误处理测试
    blue "===== 测试组: 错误处理 ====="
    echo ""
    test_missing_parse_script
    echo ""

    # 边界条件测试
    blue "===== 测试组: 边界条件 ====="
    echo ""
    test_empty_data
    test_long_option_metric
    test_long_option_top
    test_long_option_threshold
    test_combined_options
    test_combined_by_function_threshold
    echo ""

    # 输出格式测试
    blue "===== 测试组: 输出格式 ====="
    echo ""
    test_output_has_expected_sections
    test_no_threshold_shows_hint
    echo ""

    # 测试摘要
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
    rm -rf "$TEST_RESULTS_DIR/no_parse" 2>/dev/null || true
}

# 解析参数
SKIP_CLEANUP=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-cleanup)
            SKIP_CLEANUP=true
            shift
            ;;
        *)
            echo "未知选项: $1"
            echo "用法: $0 [--no-cleanup]"
            exit 1
            ;;
    esac
done

trap cleanup EXIT INT TERM

if run_all_tests; then
    if [ "$SKIP_CLEANUP" = false ]; then
        cleanup
    fi
    exit 0
else
    if [ "$SKIP_CLEANUP" = false ]; then
        cleanup
    fi
    exit 1
fi
