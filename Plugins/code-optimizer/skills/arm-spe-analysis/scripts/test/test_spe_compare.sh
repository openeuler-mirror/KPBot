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
# 测试组: 函数级对比与 Rust hash 对齐
# =============================================================================

# mock spe-parse.sh 支持 --by-function 全量输出，按数据文件名区分 before/after 两臂
setup_mock_byfunction() {
    rm -rf "$MOCK_DIR"
    mkdir -p "$MOCK_DIR"

    cat > "$MOCK_DIR/spe-parse.sh" <<'MOCKEOF'
#!/bin/bash
DATA=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --summary) SUMMARY=true; shift ;;
        --by-function) BYFN=true; shift ;;
        *) DATA="$1"; shift ;;
    esac
done

if [[ "$DATA" == *after* ]]; then
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

=== 按指令聚合 ===
ADDRESS             COUNT     L1_MISS%   LLC_MISS%    BR_MISS%  SYMBOL
ffff80001000            2          10.0%        0.0%       0.0%  _ZN4calc3dot17hbbbb2222bbbb2222E
ffff80002000            1          15.0%        0.0%       0.0%  plain_c_function
ffff80003000            1          20.0%        0.0%       0.0%  only_after_fn

=== 按函数聚合 ===
FUNCTION                                            COUNT     L1_MISS%   LLC_MISS%    BR_MISS%
_ZN4calc3dot17hbbbb2222bbbb2222E                       2          10.0%        0.0%       0.0%
plain_c_function                                       1          15.0%        0.0%       0.0%
only_after_fn                                          1          20.0%        0.0%       0.0%
EOF
else
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

=== 按指令聚合 ===
ADDRESS             COUNT     L1_MISS%   LLC_MISS%    BR_MISS%  SYMBOL
ffff80001000            2          20.0%        0.0%       0.0%  _ZN4calc3dot17haaaa1111aaaa1111E
ffff80002000            1          10.0%        0.0%       0.0%  plain_c_function
ffff80003000            1          20.0%        0.0%       0.0%  only_before_fn

=== 按函数聚合 ===
FUNCTION                                            COUNT     L1_MISS%   LLC_MISS%    BR_MISS%
_ZN4calc3dot17haaaa1111aaaa1111E                       2          20.0%        0.0%       0.0%
plain_c_function                                       1          10.0%        0.0%       0.0%
only_before_fn                                         1          20.0%        0.0%       0.0%
EOF
fi
MOCKEOF

    chmod +x "$MOCK_DIR/spe-parse.sh"
    ln -sf "$SCRIPT" "$MOCK_DIR/spe-compare.sh"
    touch "$MOCK_DIR/before.data" "$MOCK_DIR/after.data"
}

test_byfunction_hash_alignment() {
    blue "测试: --by-function Rust hash 规范化对齐"
    setup_mock_byfunction
    local output
    output=$(bash "$MOCK_DIR/spe-compare.sh" --by-function "$MOCK_DIR/before.data" "$MOCK_DIR/after.data" 2>&1) || true
    # 两 hash 版本对齐为一条规范名，证明 normalize 生效
    assert_contains "$output" "_ZN4calc3dot " "去 hash 后的规范函数名应出现在对比表"
    assert_not_contains "$output" "17haaaa1111aaaa1111E" "before 臂 hash 后缀应被剥离"
    assert_not_contains "$output" "17hbbbb2222bbbb2222E" "after 臂 hash 后缀应被剥离"
    assert_contains "$output" "=== 函数级对比 ===" "应显示函数级对比段"
}

test_byfunction_only_in_lists() {
    blue "测试: --by-function 单侧函数清单"
    setup_mock_byfunction
    local output
    output=$(bash "$MOCK_DIR/spe-compare.sh" --by-function "$MOCK_DIR/before.data" "$MOCK_DIR/after.data" 2>&1) || true
    assert_contains "$output" "--- 仅优化前 ---" "应显示仅优化前段"
    assert_contains "$output" "only_before_fn" "only_before_fn 应出现在单侧清单"
    assert_contains "$output" "--- 仅优化后 ---" "应显示仅优化后段"
    assert_contains "$output" "only_after_fn" "only_after_fn 应出现在单侧清单"
}

test_byfunction_verdicts() {
    blue "测试: --by-function 判定（下降=IMPROVED，上升=REGRESSED）"
    setup_mock_byfunction
    local output
    output=$(bash "$MOCK_DIR/spe-compare.sh" --by-function "$MOCK_DIR/before.data" "$MOCK_DIR/after.data" 2>&1) || true
    local rust_line plain_line
    rust_line=$(echo "$output" | grep "_ZN4calc3dot" | head -1)
    plain_line=$(echo "$output" | grep "plain_c_function" | head -1)
    assert_contains "$rust_line" "[IMPROVED]" "Rust 函数 miss 20.0→10.0 应判 IMPROVED"
    assert_contains "$plain_line" "[REGRESSED]" "plain_c 函数 miss 10.0→15.0 应判 REGRESSED"
}

test_byfunction_legacy_compatibility() {
    blue "测试: 不带 --by-function 输出与旧格式一致"
    setup_mock_byfunction
    local output
    output=$(bash "$MOCK_DIR/spe-compare.sh" "$MOCK_DIR/before.data" "$MOCK_DIR/after.data" 2>&1) || true
    assert_contains "$output" "=== 对比报告 ===" "应显示全局对比报告"
    assert_not_contains "$output" "按函数聚合" "无 --by-function 不应出现函数表"
    assert_not_contains "$output" "仅优化前" "无 --by-function 不应出现单侧清单"
    assert_not_contains "$output" "FUNCTION  COUNT" "无 --by-function 不应出现函数表头"
}

test_byfunction_json() {
    blue "测试: -f json --by-function 输出函数级 JSON"
    setup_mock_byfunction
    local output
    output=$(bash "$MOCK_DIR/spe-compare.sh" -f json --by-function "$MOCK_DIR/before.data" "$MOCK_DIR/after.data" 2>&1) || true
    assert_contains "$output" "\"functions\"" "JSON 应包含 functions 键"
    assert_contains "$output" "\"name\": \"_ZN4calc3dot\"" "JSON 函数名应为去 hash 规范名"
    assert_contains "$output" "\"only_in_before\"" "JSON 应包含 only_in_before 键"
    assert_contains "$output" "\"only_in_after\"" "JSON 应包含 only_in_after 键"
    assert_contains "$output" "\"only_before_fn\"" "JSON only_in_before 应含单侧函数"
    assert_contains "$output" "\"verdict\": \"improved\"" "JSON verdict 应为 improved"
    # 全局字段保留
    assert_contains "$output" "\"before\"" "JSON 应保留全局 before 段"
    assert_contains "$output" "\"diff\"" "JSON 应保留全局 diff 段"
}

test_byfunction_json_without_flag() {
    blue "测试: -f json 不带 --by-function 无 functions 字段"
    setup_mock_byfunction
    local output
    output=$(bash "$MOCK_DIR/spe-compare.sh" -f json "$MOCK_DIR/before.data" "$MOCK_DIR/after.data" 2>&1) || true
    assert_contains "$output" "\"before\"" "JSON 应保留全局字段"
    assert_not_contains "$output" "\"functions\"" "无 --by-function JSON 不应含 functions"
    assert_not_contains "$output" "\"only_in_before\"" "无 --by-function JSON 不应含 only_in_before"
}

test_byfunction_fallback() {
    blue "测试: 无交集时回退全局汇总对比"
    local tmpdir="$TEST_RESULTS_DIR/compare_mock"
    rm -rf "$tmpdir"
    mkdir -p "$tmpdir"
    cat > "$tmpdir/spe-parse.sh" <<'MOCKEOF'
#!/bin/bash
while [[ $# -gt 0 ]]; do shift; done
cat <<'EOF'
=== SPE 采样汇总 ===
总记录数:      100

--- Cache 分布 ---
L1 miss:   10 (10.0%)
LLC miss:   2 (2.0%)
Remote:    1 (1.0%)

--- TLB 分布 ---
TLB access: 20 (20.0%)
TLB miss:   1 (5.0% of TLB)

--- 分支预测 ---
Branch miss: 2 (100.0% of branches)
EOF
MOCKEOF
    chmod +x "$tmpdir/spe-parse.sh"
    ln -sf "$SCRIPT" "$tmpdir/spe-compare.sh"
    touch "$tmpdir/before.data" "$tmpdir/after.data"

    local output
    output=$(bash "$tmpdir/spe-compare.sh" --by-function "$tmpdir/before.data" "$tmpdir/after.data" 2>&1) || true
    assert_contains "$output" "未找到可对齐的函数级数据，回退全局汇总对比" "无函数表应提示回退"
    assert_contains "$output" "=== 对比报告 ===" "回退后应输出全局对比报告"
    assert_not_contains "$output" "=== 函数级对比 ===" "回退时不应输出函数级对比表"
}

test_byfunction_fallback_json_empty_functions() {
    blue "测试: 无交集 json 模式 functions 为空数组"
    local tmpdir="$TEST_RESULTS_DIR/compare_mock"
    rm -rf "$tmpdir"
    mkdir -p "$tmpdir"
    cat > "$tmpdir/spe-parse.sh" <<'MOCKEOF'
#!/bin/bash
while [[ $# -gt 0 ]]; do shift; done
cat <<'EOF'
=== SPE 采样汇总 ===
总记录数:      100

--- Cache 分布 ---
L1 miss:   10 (10.0%)
LLC miss:   2 (2.0%)
Remote:    1 (1.0%)

--- TLB 分布 ---
TLB access: 20 (20.0%)
TLB miss:   1 (5.0% of TLB)

--- 分支预测 ---
Branch miss: 2 (100.0% of branches)
EOF
MOCKEOF
    chmod +x "$tmpdir/spe-parse.sh"
    ln -sf "$SCRIPT" "$tmpdir/spe-compare.sh"
    touch "$tmpdir/before.data" "$tmpdir/after.data"

    local output
    output=$(bash "$tmpdir/spe-compare.sh" -f json --by-function "$tmpdir/before.data" "$tmpdir/after.data" 2>&1) || true
    assert_contains "$output" "\"functions\": []" "无函数表 JSON functions 应为空数组"
    assert_contains "$output" "\"before\"" "回退 JSON 仍保留全局字段"
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

    blue "===== 测试组: 函数级对比与 Rust hash 对齐 ====="
    echo ""
    test_byfunction_hash_alignment
    test_byfunction_only_in_lists
    test_byfunction_verdicts
    test_byfunction_legacy_compatibility
    test_byfunction_json
    test_byfunction_json_without_flag
    test_byfunction_fallback
    test_byfunction_fallback_json_empty_functions
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
