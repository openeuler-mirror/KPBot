#!/bin/bash
# =============================================================================
# spe-latency-report.py 测试套件
# 覆盖：报告章节生成、门禁判定数值、时延分布统计、缓存命中层级、指令追逐、
#       高时延根因分析、JSON 输出、前后对比、缺 weight 降级、错误处理
# =============================================================================

set -e

red()     { echo -e "\033[31m$*\033[0m"; }
green()   { echo -e "\033[32m$*\033[0m"; }
yellow()  { echo -e "\033[33m$*\033[0m"; }
blue()    { echo -e "\033[34m$*\033[0m"; }

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="$(dirname "$TEST_DIR")"
REPORT="$SCRIPT_DIR/spe-latency-report.py"
TEST_RESULTS_DIR="$TEST_DIR/test_results_report"
TEST_PASSED=0
TEST_FAILED=0
MOCK_CSV="$TEST_RESULTS_DIR/mock_samples.csv"
MOCK_NO_LAT="$TEST_RESULTS_DIR/mock_nolat.csv"
BASELINE_CSV="$TEST_RESULTS_DIR/baseline.csv"

# =============================================================================
# 断言函数
# =============================================================================

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

assert_exit_code() {
    local expected="$1" actual="$2" message="${3:-AssertExitCode}"
    if [ "$actual" -eq "$expected" ]; then
        echo "  [PASS] $message"
        TEST_PASSED=$((TEST_PASSED + 1))
    else
        echo "  [FAIL] $message (期望 exit=$expected, 实际 exit=$actual)"
        TEST_FAILED=$((TEST_FAILED + 1))
    fi
}

# =============================================================================
# 夹具：9 条样本，已知手算期望值
#   latencies = 200,300,900,1200,80,180,250,850,95
#   sorted: 80,95,180,200,250,300,850,900,1200
#   P50=250, P90=900, P99=1200, mean=450.6, max=1200
#   l1d-miss=4 (44.44%), llc-miss=2 (22.22%), branch-miss=2, tlb-miss=1
#   data_src: L1=3, L2=1, L3=1, REMOTE=1, DRAM=1, UNKNOWN=2
#   tail(>=500): 900,1200,850 → 3 (33.33%)
# =============================================================================

setup_fixtures() {
    mkdir -p "$TEST_RESULTS_DIR"

    cat > "$MOCK_CSV" <<'EOF'
comm,pid,tid,cpu,time,period,event,ip,sym,dso,weight,data_src,addr
claude,219296,,0,12345.678,1000000,l1d-miss,ffff80001000,func_hot+0x10,/usr/lib/libc.so,200,L1,0x7fffb000
claude,219296,,1,12345.679,1000000,l1d-miss,ffff80001000,func_hot+0x10,/usr/lib/libc.so,300,1,0x7fffb100
node,223175,,2,12345.680,1000000,llc-miss,ffff80002000,func_warm+0x20,/usr/lib/libm.so,900,6,0x7fffc000
claude,219296,,0,12345.681,1000000,tlb-miss,ffff80003000,func_tlb+0x8,/usr/lib/libc.so,1200,5,0x7fffd000
claude,219296,,1,12345.683,1000000,branch-miss,ffff80004000,func_br,/usr/lib/libc.so,80,NA,0x0
claude,219296,,2,12345.684,1000000,l1d-miss,ffff80005000,func_llc+0x4,/usr/lib/libc.so,180,L1,0x7fffe000
claude,219296,,3,12345.685,1000000,l1d-miss,ffff80006000,func_mixed+0x4,/usr/lib/libc.so,250,2,0x7ffff000
claude,219296,,0,12345.686,1000000,llc-miss,ffff80006000,func_mixed+0x4,/usr/lib/libc.so,850,3,0x7ffff000
claude,219296,,1,12345.687,1000000,branch-miss,ffff80006000,func_mixed+0x4,/usr/lib/libc.so,95,NA,0x0
EOF

    # 无 weight/data_src 的降级 CSV
    cat > "$MOCK_NO_LAT" <<'EOF'
comm,pid,tid,cpu,time,period,event,ip,sym,dso,weight,data_src,addr
claude,219296,,0,12345.678,1000000,l1d-miss,ffff80001000,func_hot+0x10,/usr/lib/libc.so,,
claude,219296,,1,12345.679,1000000,llc-miss,ffff80002000,func_warm+0x20,/usr/lib/libm.so,,
claude,219296,,0,12345.680,1000000,branch-miss,ffff80004000,func_br,/usr/lib/libc.so,,
EOF

    # 基线：把 func_mixed 的 3 条改为 func_hot（制造 l1_miss 变化）
    cat > "$BASELINE_CSV" <<'EOF'
comm,pid,tid,cpu,time,period,event,ip,sym,dso,weight,data_src,addr
claude,219296,,0,12345.678,1000000,l1d-miss,ffff80001000,func_hot+0x10,/usr/lib/libc.so,200,L1,0x7fffb000
claude,219296,,1,12345.679,1000000,l1d-miss,ffff80001000,func_hot+0x10,/usr/lib/libc.so,300,1,0x7fffb100
node,223175,,2,12345.680,1000000,llc-miss,ffff80002000,func_warm+0x20,/usr/lib/libm.so,900,6,0x7fffc000
claude,219296,,0,12345.681,1000000,tlb-miss,ffff80003000,func_tlb+0x8,/usr/lib/libc.so,1200,5,0x7fffd000
claude,219296,,1,12345.683,1000000,branch-miss,ffff80004000,func_br,/usr/lib/libc.so,80,NA,0x0
claude,219296,,2,12345.684,1000000,l1d-miss,ffff80005000,func_llc+0x4,/usr/lib/libc.so,180,L1,0x7fffe000
claude,219296,,3,12345.685,1000000,l1d-miss,ffff80001000,func_hot+0x10,/usr/lib/libc.so,250,2,0x7ffff000
claude,219296,,0,12345.686,1000000,llc-miss,ffff80001000,func_hot+0x10,/usr/lib/libc.so,850,3,0x7ffff000
claude,219296,,1,12345.687,1000000,branch-miss,ffff80001000,func_hot+0x10,/usr/lib/libc.so,95,NA,0x0
EOF
}

run_report() { python3 "$REPORT" "$@"; }

# =============================================================================
# 测试组
# =============================================================================

test_report_sections() {
    blue "===== 测试组: 报告章节与数值 ====="
    echo ""
    local md="$TEST_RESULTS_DIR/report.md"
    local out
    out=$(run_report "$MOCK_CSV" -o "$md" 2>&1)
    assert_contains "$out" "报告已生成" "报告生成成功"
    assert_contains "$out" "$md" "报告路径输出"

    local r
    r=$(cat "$md")
    assert_contains "$r" "## 0. 执行摘要" "章节: 执行摘要"
    assert_contains "$r" "## 1. 指标门禁判定" "章节: 指标门禁判定"
    assert_contains "$r" "## 2. 指令时延分布" "章节: 指令时延分布"
    assert_contains "$r" "## 3. 缓存命中层级" "章节: 缓存命中层级"
    assert_contains "$r" "## 4. 指令追逐" "章节: 指令追逐"
    assert_contains "$r" "## 5. 高时延指令根因分析" "章节: 高时延根因分析"
    assert_contains "$r" "## 6. 补充指标" "章节: 补充指标"
    assert_contains "$r" "P50 | 250" "P50=250 cycles"
    assert_contains "$r" "P90 | 900" "P90=900 cycles"
    assert_contains "$r" "P99 | 1200" "P99=1200 cycles"
    assert_contains "$r" "| 最大值 | 1200" "最大时延 1200"
    assert_contains "$r" "l1_miss | 44.44% | **SEVERE**" "l1_miss SEVERE 判定"
    assert_contains "$r" "tail_ratio(≥1000cyc) | 11.11% | **SEVERE**" "tail_ratio 判定"
    assert_contains "$r" "func_hot+0x10" "函数热点表含 func_hot"
    assert_contains "$r" "ffff80003000" "指令热点表含 func_tlb 指令"
    assert_contains "$r" "阈值: 时延 ≥ 500 cycles，尾部样本 3 条" "根因阈值与尾部样本数"
    assert_contains "$r" "REMOTE" "根因因子含 REMOTE"
    assert_contains "$r" "tlb_miss" "根因因子含 tlb_miss"
    assert_contains "$r" "DRAM | 1" "data_src 分布含 DRAM"
    echo ""
}

test_json_output() {
    blue "===== 测试组: JSON 输出 ====="
    echo ""
    local json="$TEST_RESULTS_DIR/report.json"
    run_report "$MOCK_CSV" --json "$json" -o "$TEST_RESULTS_DIR/j.md" >/dev/null 2>&1
    local out
    out=$(python3 -c "
import json,sys
d=json.load(open('$json'))
assert d['meta']['total']==9
assert d['latency']['p99']==1200
assert d['latency']['p50']==250
assert d['grades']['l1_miss']=='SEVERE'
assert d['grades']['branch_mispred']=='SEVERE'
assert d['grades']['p99_latency']=='SEVERE'
assert d['tail_analysis']['n']==3
assert abs(d['rates']['llc_miss']-22.22)<0.01
print('json-ok')
" 2>&1)
    assert_contains "$out" "json-ok" "JSON 关键数值与门禁等级正确"
    echo ""
}

test_min_lat() {
    blue "===== 测试组: --min-lat 阈值调整 ====="
    echo ""
    local md="$TEST_RESULTS_DIR/tail.md"
    run_report "$MOCK_CSV" --min-lat 1000 -o "$md" >/dev/null 2>&1
    local r
    r=$(cat "$md")
    assert_contains "$r" "阈值: 时延 ≥ 1000 cycles，尾部样本 1 条" "阈值 1000 时仅 1 条尾部"
    echo ""
}

test_baseline() {
    blue "===== 测试组: 前后对比（--baseline） ====="
    echo ""
    local md="$TEST_RESULTS_DIR/diff.md"
    run_report "$MOCK_CSV" --baseline "$BASELINE_CSV" --metric l1_miss -o "$md" >/dev/null 2>&1
    local r
    r=$(cat "$md")
    assert_contains "$r" "## 7. 前后对比" "对比章节存在"
    assert_contains "$r" "func_hot+0x10" "对比表含 func_hot"
    assert_contains "$r" "REGRESSED" "存在 REGRESSED 判定"
    assert_contains "$r" "NO CHANGE" "存在 NO CHANGE 判定"
    echo ""
}

test_degraded() {
    blue "===== 测试组: 缺 weight/data_src 降级 ====="
    echo ""
    local md="$TEST_RESULTS_DIR/nolat.md"
    run_report "$MOCK_NO_LAT" -o "$md" >/dev/null 2>&1
    local r
    r=$(cat "$md")
    assert_contains "$r" "本数据无 weight（延迟）字段" "无 weight 降级提示"
    assert_contains "$r" "无法做命中层级分析" "无 data_src 降级提示"
    assert_contains "$r" "无 weight 字段，以下按采样数排序" "指令追逐降级提示"
    echo ""
}

test_errors() {
    blue "===== 测试组: 错误处理 ====="
    echo ""
    local ret=0
    run_report /nonexistent.csv -o /tmp/x.md >/dev/null 2>&1 || ret=$?
    assert_exit_code 1 "$ret" "CSV 不存在 exit=1"

    ret=0
    printf 'comm,pid,tid,cpu,time,period,event,ip,sym,dso,weight,data_src,addr\n' > "$TEST_RESULTS_DIR/emptyish.csv"
    run_report "$TEST_RESULTS_DIR/emptyish.csv" -o /tmp/x.md >/dev/null 2>&1 || ret=$?
    assert_exit_code 1 "$ret" "仅表头(无样本) CSV exit=1"

    ret=0
    printf 'comm\nx\n' > "$TEST_RESULTS_DIR/badheader.csv"
    run_report "$TEST_RESULTS_DIR/badheader.csv" -o /tmp/x.md >/dev/null 2>&1 || ret=$?
    assert_exit_code 1 "$ret" "缺必需列 CSV exit=1"
    echo ""
}

run_all_tests() {
    echo "========================================================================="
    yellow "spe-latency-report.py 测试套件"
    echo "========================================================================="
    echo ""

    setup_fixtures
    test_report_sections
    test_json_output
    test_min_lat
    test_baseline
    test_degraded
    test_errors

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
