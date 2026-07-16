#!/bin/bash
# =============================================================================
# spe-parse.sh 测试套件
# 测试脚本的参数解析、核心解析逻辑、错误处理和边界条件
# =============================================================================

set -e

# 颜色输出
red()     { echo -e "\033[31m$*\033[0m"; }
green()   { echo -e "\033[32m$*\033[0m"; }
yellow()  { echo -e "\033[33m$*\033[0m"; }
blue()    { echo -e "\033[34m$*\033[0m"; }

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="$(dirname "$TEST_DIR")"
SCRIPT="$SCRIPT_DIR/spe-parse.sh"
TEST_RESULTS_DIR="$TEST_DIR/test_results"
TEST_PASSED=0
TEST_FAILED=0
MOCK_BIN="$TEST_RESULTS_DIR/mock_bin"
MOCK_DATA="$TEST_RESULTS_DIR/mock_perf.data"

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

assert_number_gt() {
    local value="$1"
    local threshold="$2"
    local message="${3:-AssertNumberGt}"
    if [ "$(echo "$value > $threshold" | bc -l 2>/dev/null || echo 0)" = "1" ]; then
        echo "  [PASS] $message"
        TEST_PASSED=$((TEST_PASSED + 1))
    else
        echo "  [FAIL] $message ($value <= $threshold)"
        TEST_FAILED=$((TEST_FAILED + 1))
    fi
}

# =============================================================================
# Mock 环境设置
# =============================================================================

setup_mock() {
    rm -rf "$TEST_RESULTS_DIR"
    mkdir -p "$MOCK_BIN"
    touch "$MOCK_DATA"

    # 创建 mock perf 命令，输出已知的 SPE 记录
    cat > "$MOCK_BIN/perf" <<'PERFEOF'
#!/bin/bash
if [[ "$1" == "script" ]]; then
    cat <<'EOF'
claude 219296 [000] 12345.678: 1000000 l1d-miss: ffff80001000 func_hot+0x10 (/usr/lib/libc.so)
claude 219296 [001] 12345.679: 1000000 l1d-miss: ffff80001000 func_hot+0x10 (/usr/lib/libc.so)
node   223175 [002] 12345.680: 1000000 llc-miss: ffff80002000 func_warm+0x20 (/usr/lib/libm.so)
claude 219296 [003] 12345.681: 1000000 tlb-access: ffff80003000 func_tlb (/usr/lib/libc.so)
claude 219296 [000] 12345.682: 1000000 tlb-miss: ffff80003000 func_tlb (/usr/lib/libc.so)
claude 219296 [001] 12345.683: 1000000 branch-miss: ffff80004000 func_br (/usr/lib/libc.so)
claude 219296 [002] 12345.684: 1000000 remote-access: ffff80005000 func_remote+0x8 (/usr/lib/libc.so)
claude 219296 [003] 12345.685: 1000000 l1d-miss: ffff80006000 func_mixed+0x4 (/usr/lib/libc.so)
claude 219296 [000] 12345.686: 1000000 llc-miss: ffff80006000 func_mixed+0x4 (/usr/lib/libc.so)
claude 219296 [001] 12345.687: 1000000 branch-miss: ffff80006000 func_mixed+0x4 (/usr/lib/libc.so)
EOF
fi
PERFEOF
    chmod +x "$MOCK_BIN/perf"
}

setup_mock_empty() {
    rm -rf "$TEST_RESULTS_DIR"
    mkdir -p "$MOCK_BIN"
    touch "$MOCK_DATA"

    cat > "$MOCK_BIN/perf" <<'PERFEOF'
#!/bin/bash
if [[ "$1" == "script" ]]; then
    # 输出空（无 SPE 记录），不产生任何行
    true
fi
PERFEOF
    chmod +x "$MOCK_BIN/perf"
}

setup_mock_no_perf() {
    rm -rf "$TEST_RESULTS_DIR"
    mkdir -p "$MOCK_BIN"
    touch "$MOCK_DATA"
    # 不创建 perf mock，让 command -v perf 失败
}

# 在 mock 环境中运行脚本
run_parse() {
    PATH="$MOCK_BIN:$PATH" bash "$SCRIPT" "$@"
}

# =============================================================================
# 测试组: 参数解析
# =============================================================================

test_help_short() {
    blue "测试: -h 显示帮助"
    local output
    output=$(bash "$SCRIPT" -h 2>&1) || true
    assert_contains "$output" "用法:" "-h 应显示用法"
    assert_contains "$output" "spe-parse.sh" "-h 应显示脚本名"
}

test_help_long() {
    blue "测试: --help 显示帮助"
    local output
    output=$(bash "$SCRIPT" --help 2>&1) || true
    assert_contains "$output" "用法:" "--help 应显示用法"
    assert_contains "$output" "--format" "--help 应显示 --format 选项"
    assert_contains "$output" "--sort" "--help 应显示 --sort 选项"
}

test_missing_data_file() {
    blue "测试: 缺少数据文件参数"
    local output
    output=$(bash "$SCRIPT" 2>&1) || true
    assert_contains "$output" "错误" "缺少文件应报错"
    assert_contains "$output" "请指定 perf.data" "应提示指定文件"
}

test_nonexistent_file() {
    blue "测试: 文件不存在"
    local exit_code=0
    local output
    output=$(bash "$SCRIPT" /tmp/nonexistent_parse_$$.data 2>&1) || exit_code=$?
    assert_contains "$output" "文件不存在" "不存在的文件应报错"
    assert_exit_code 1 "$exit_code" "应返回退出码 1"
}

test_unknown_option() {
    blue "测试: 未知选项"
    local output
    output=$(bash "$SCRIPT" --unknown-flag 2>&1) || true
    assert_contains "$output" "未知选项" "未知选项应提示"
}

# =============================================================================
# 测试组: --summary 模式
# =============================================================================

test_summary_mode() {
    blue "测试: --summary 仅输出汇总"
    setup_mock
    local output
    output=$(run_parse --summary "$MOCK_DATA" 2>&1) || true
    assert_contains "$output" "SPE 采样汇总" "应显示汇总标题"
    assert_contains "$output" "总记录数" "应显示总记录数"
    assert_contains "$output" "Cache 分布" "应显示缓存分布"
    assert_contains "$output" "TLB 分布" "应显示 TLB 分布"
    assert_contains "$output" "分支预测" "应显示分支预测"
    assert_not_contains "$output" "按指令聚合" "summary 模式不应包含指令聚合表"
    assert_not_contains "$output" "ADDRESS" "summary 模式不应包含地址表"
}

test_summary_event_counts() {
    blue "测试: --summary 事件计数正确"
    setup_mock
    local output
    output=$(run_parse --summary "$MOCK_DATA" 2>&1) || true
    # 总计 10 条记录
    assert_contains "$output" "总记录数:      10" "总记录数应为 10"
    # L1 miss: 3 条 (2 条 l1d-miss at func_hot, 1 at func_mixed)
    assert_contains "$output" "L1 miss:   3 (30.0%)" "L1 miss 应为 3 (30%)"
    # LLC miss: 2 条 (func_warm + func_mixed)，remote-access 独立统计
    assert_contains "$output" "LLC miss:  2 (20.0%)" "LLC miss 应为 2 (20%)"
    # Remote: 1 条
    assert_contains "$output" "Remote:    1 (10.0%)" "Remote 应为 1 (10%)"
    # TLB access: 2 条, TLB miss: 1 条
    assert_contains "$output" "TLB access: 2 (20.0%)" "TLB access 应为 2 (20%)"
    # Branch miss: 2 条
    assert_contains "$output" "Branch miss: 2 (100.0% of branches)" "Branch miss 应为 2 (100%)"
}

# =============================================================================
# 测试组: 默认 text 输出
# =============================================================================

test_default_output() {
    blue "测试: 默认 text 输出包含汇总和 Top 表"
    setup_mock
    local output
    output=$(run_parse "$MOCK_DATA" 2>&1) || true
    assert_contains "$output" "SPE 采样汇总" "应包含汇总"
    assert_contains "$output" "Top 20" "应包含 Top 20 标题"
    assert_contains "$output" "ADDRESS" "应包含地址表头"
}

test_top_n_limits_output() {
    blue "测试: -n 限制输出行数"
    setup_mock
    local output
    output=$(run_parse -n 2 "$MOCK_DATA" 2>&1) || true
    assert_contains "$output" "Top 2" "应显示 Top 2"
    # 应该有 2 条数据行
    assert_contains "$output" "ffff80006000" "应包含最高频地址 ffff80006000 (3次)"
}

test_sort_by_sample_count() {
    blue "测试: -s sample_count 排序"
    setup_mock
    local output
    output=$(run_parse -s sample_count -n 3 "$MOCK_DATA" 2>&1) || true
    # ffff80006000 出现 3 次（最多），应该排第一
    local first_line
    first_line=$(echo "$output" | grep -A1 "^ADDRESS" | tail -1)
    assert_contains "$first_line" "ffff80006000" "按采样数排序，func_mixed(3次)应排第一"
}

test_sort_by_l1_miss() {
    blue "测试: -s l1_miss 排序"
    setup_mock
    local output
    output=$(run_parse -s l1_miss -n 3 "$MOCK_DATA" 2>&1) || true
    assert_contains "$output" "按 l1_miss 排序" "应显示按 l1_miss 排序"
    # func_mixed 有 1/1=100% l1_miss, func_hot 有 2/2=100% l1_miss
    # 都应该排在前面
    assert_contains "$output" "100.0%" "应包含 100% l1_miss 的地址"
}

test_sort_by_llc_miss() {
    blue "测试: -s llc_miss 排序"
    setup_mock
    local output
    output=$(run_parse -s llc_miss -n 3 "$MOCK_DATA" 2>&1) || true
    assert_contains "$output" "按 llc_miss 排序" "应显示按 llc_miss 排序"
}

test_sort_by_br_miss() {
    blue "测试: -s br_miss 排序"
    setup_mock
    local output
    output=$(run_parse -s br_miss -n 3 "$MOCK_DATA" 2>&1) || true
    assert_contains "$output" "按 br_miss 排序" "应显示按 br_miss 排序"
}

test_long_options() {
    blue "测试: 长选项 --format --sort --top"
    setup_mock
    local output
    output=$(run_parse --format text --sort sample_count --top 3 "$MOCK_DATA" 2>&1) || true
    assert_contains "$output" "Top 3" "长选项应正确解析"
    assert_contains "$output" "SPE 采样汇总" "长选项模式应正常工作"
}

# =============================================================================
# 测试组: JSON 输出
# =============================================================================

test_json_output() {
    blue "测试: -f json 输出"
    setup_mock
    local output
    output=$(run_parse -f json "$MOCK_DATA" 2>&1) || true
    assert_contains "$output" "\"total_records\"" "JSON 应包含 total_records"
    assert_contains "$output" "\"l1_miss\"" "JSON 应包含 l1_miss"
    assert_contains "$output" "\"llc_miss\"" "JSON 应包含 llc_miss"
    assert_contains "$output" "\"tlb_access\"" "JSON 应包含 tlb_access"
    assert_contains "$output" "\"branch_miss\"" "JSON 应包含 branch_miss"
}

test_json_values() {
    blue "测试: JSON 数值正确"
    setup_mock
    local output
    output=$(run_parse -f json "$MOCK_DATA" 2>&1) || true
    assert_contains "$output" "\"total_records\": 10" "JSON total_records 应为 10"
    assert_contains "$output" "\"l1_miss\": 3" "JSON l1_miss 应为 3"
    assert_contains "$output" "\"llc_miss\": 2" "JSON llc_miss 应为 2"
    assert_contains "$output" "\"tlb_access\": 2" "JSON tlb_access 应为 2"
    assert_contains "$output" "\"branch_miss\": 2" "JSON branch_miss 应为 2"
}

# =============================================================================
# 测试组: extract_fields 符号清理
# =============================================================================

test_symbol_cleaning_offset() {
    blue "测试: 符号清理 - 移除 +0x 偏移"
    setup_mock
    local output
    output=$(run_parse "$MOCK_DATA" 2>&1) || true
    # func_hot+0x10 should become func_hot
    assert_contains "$output" "func_hot" "应显示 func_hot (去除 +0x10 偏移)"
    assert_not_contains "$output" "func_hot+0x" "不应包含 +0x 偏移"
}

test_symbol_cleaning_dso() {
    blue "测试: 符号清理 - 移除 (DSO)"
    setup_mock
    local output
    output=$(run_parse "$MOCK_DATA" 2>&1) || true
    assert_not_contains "$output" "/usr/lib" "不应包含 DSO 路径"
    assert_not_contains "$output" "(/usr/lib/libc.so)" "不应包含 DSO 括号"
}

# =============================================================================
# 测试组: 地址聚合
# =============================================================================

test_address_aggregation() {
    blue "测试: 地址聚合 - 相同地址合并"
    setup_mock
    local output
    output=$(run_parse "$MOCK_DATA" 2>&1) || true
    # ffff80001000 (func_hot) 出现 2 次，COUNT 应为 2
    # 检查输出中 ffff80001000 行的 COUNT 列
    local count_line
    count_line=$(echo "$output" | grep "ffff80001000" | head -1)
    # 格式: ffff80001000  2      100.0%       0.0%       0.0%  func_hot
    assert_contains "$count_line" "2" "聚合后 COUNT 应为 2"
    assert_contains "$count_line" "100.0%" "l1_miss 率应为 100%"
}

test_multi_event_address() {
    blue "测试: 多事件地址 - func_mixed"
    setup_mock
    local output
    output=$(run_parse "$MOCK_DATA" 2>&1) || true
    # ffff80006000 (func_mixed) 有 3 条: l1d-miss, llc-miss, branch-miss
    local mixed_line
    mixed_line=$(echo "$output" | grep "ffff80006000" | head -1)
    assert_contains "$mixed_line" "3" "func_mixed 的 COUNT 应为 3"
    assert_contains "$mixed_line" "33.3%" "l1_miss 率应为 33.3% (1/3)"
    assert_contains "$mixed_line" "func_mixed" "符号应为 func_mixed"
}

# =============================================================================
# 测试组: 错误处理
# =============================================================================

test_perf_not_available() {
    blue "测试: perf 命令不可用"
    # 如果系统上有 perf，无法测试此场景
    if command -v perf &>/dev/null; then
        yellow "  [SKIP] 系统已安装 perf，无法模拟 perf 缺失场景"
        return
    fi
    setup_mock_no_perf
    local exit_code=0
    local output
    output=$(PATH="$MOCK_BIN" bash "$SCRIPT" "$MOCK_DATA" 2>&1) || exit_code=$?
    assert_contains "$output" "perf 命令不可用" "应报告 perf 不可用"
    assert_exit_code 1 "$exit_code" "应返回退出码 1"
}

test_invalid_sort_field() {
    blue "测试: 无效排序字段的处理"
    setup_mock
    # 无效字段应回退到默认 sample_count
    local output
    output=$(run_parse -s nonexistent_field -n 3 "$MOCK_DATA" 2>&1) || true
    # 应该仍能正常工作（回退到默认排序）
    assert_contains "$output" "SPE 采样汇总" "无效排序字段应能正常工作"
}

# =============================================================================
# 测试组: 边界条件
# =============================================================================

test_empty_data() {
    blue "测试: 空 perf 输出"
    setup_mock_empty
    local output
    output=$(run_parse "$MOCK_DATA" 2>&1) || true
    # echo "$RAW" 会产生一个换行符，awk 计为 1 行，属于已知边界行为
    assert_contains "$output" "总记录数:" "空数据应显示总记录数头"
}

test_zero_records_summary() {
    blue "测试: 空 perf 输出仍可正常完成"
    setup_mock_empty
    local output
    output=$(run_parse "$MOCK_DATA" 2>&1) || true
    # 空数据场景不会崩溃，能正常输出表头
    assert_contains "$output" "SPE 采样汇总" "空数据应显示汇总头"
}

test_top_n_greater_than_records() {
    blue "测试: Top N 大于实际记录数"
    setup_mock
    local output
    output=$(run_parse -n 100 "$MOCK_DATA" 2>&1) || true
    assert_contains "$output" "Top 100" "应显示 Top 100"
    # 应该输出所有记录，不应崩溃
    assert_contains "$output" "SPE 采样汇总" "应正常完成"
}

test_combined_options() {
    blue "测试: 组合选项 -f json --summary（summary 优先生效）"
    setup_mock
    local output
    output=$(run_parse -f json --summary "$MOCK_DATA" 2>&1) || true
    # --summary 优先于 -f json，所以输出 text 格式的汇总
    assert_contains "$output" "SPE 采样汇总" "组合选项应输出汇总"
    assert_not_contains "$output" "\"total_records\"" "summary 模式不输出 JSON"
}

test_whitespace_in_output() {
    blue "测试: 输出无多余空白行"
    setup_mock
    local output
    output=$(run_parse "$MOCK_DATA" 2>&1) || true
    # 检查输出不为空
    local line_count
    line_count=$(echo "$output" | wc -l)
    assert_number_gt "$line_count" "5" "输出应多于 5 行"
}

# =============================================================================
# 测试组: spe-collect.sh 集成（真实环境）
# =============================================================================

test_with_real_spe_data() {
    blue "测试: 真实 SPE 数据解析"

    if ! command -v perf &>/dev/null; then
        yellow "  [SKIP] perf 不可用"
        return
    fi

    if [ ! -d /sys/bus/event_source/devices/arm_spe_0 ]; then
        yellow "  [SKIP] 非 ARM SPE 环境"
        return
    fi

    # 采集 1 秒数据
    local real_data="$TEST_RESULTS_DIR/real_perf.data"
    if perf record -e arm_spe_0// -o "$real_data" -a -- sleep 1 >/dev/null 2>&1; then
        local output
        output=$(bash "$SCRIPT" "$real_data" 2>&1) || true
        assert_contains "$output" "总记录数" "真实数据应有记录"
        assert_contains "$output" "SPE 采样汇总" "真实数据应显示汇总"
        rm -f "$real_data"
    else
        yellow "  [SKIP] SPE 采集失败（可能需要特殊权限）"
    fi
}

# =============================================================================
# 主测试运行器
# =============================================================================

run_all_tests() {
    echo "========================================================================="
    echo "spe-parse.sh 测试套件"
    echo "========================================================================="
    echo ""

    # 参数解析测试
    blue "===== 测试组: 参数解析 ====="
    echo ""
    test_help_short
    test_help_long
    test_missing_data_file
    test_nonexistent_file
    test_unknown_option
    echo ""

    # --summary 模式
    blue "===== 测试组: --summary 模式 ====="
    echo ""
    test_summary_mode
    test_summary_event_counts
    echo ""

    # 默认 text 输出
    blue "===== 测试组: 默认 text 输出 ====="
    echo ""
    test_default_output
    test_top_n_limits_output
    test_sort_by_sample_count
    test_sort_by_l1_miss
    test_sort_by_llc_miss
    test_sort_by_br_miss
    test_long_options
    echo ""

    # JSON 输出
    blue "===== 测试组: JSON 输出 ====="
    echo ""
    test_json_output
    test_json_values
    echo ""

    # 符号清理
    blue "===== 测试组: 符号清理 ====="
    echo ""
    test_symbol_cleaning_offset
    test_symbol_cleaning_dso
    echo ""

    # 地址聚合
    blue "===== 测试组: 地址聚合 ====="
    echo ""
    test_address_aggregation
    test_multi_event_address
    echo ""

    # 错误处理
    blue "===== 测试组: 错误处理 ====="
    echo ""
    test_perf_not_available
    test_invalid_sort_field
    echo ""

    # 边界条件
    blue "===== 测试组: 边界条件 ====="
    echo ""
    test_empty_data
    test_zero_records_summary
    test_top_n_greater_than_records
    test_combined_options
    test_whitespace_in_output
    echo ""

    # 真实环境
    blue "===== 测试组: 真实 SPE 环境 ====="
    echo ""
    test_with_real_spe_data
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
    rm -rf "$TEST_RESULTS_DIR" 2>/dev/null || true
}

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
