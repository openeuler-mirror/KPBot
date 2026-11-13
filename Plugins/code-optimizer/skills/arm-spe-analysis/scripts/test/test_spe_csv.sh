#!/bin/bash
# =============================================================================
# spe-csv.sh / spe-to-csv.py 测试套件
# 覆盖：perf script 记录 → 标准 CSV 转换、字段提取（weight/data_src/addr）、
#       字段降级回退、schema 校验、外部 spe-parser 接入、错误处理
# =============================================================================

set -e

red()     { echo -e "\033[31m$*\033[0m"; }
green()   { echo -e "\033[32m$*\033[0m"; }
yellow()  { echo -e "\033[33m$*\033[0m"; }
blue()    { echo -e "\033[34m$*\033[0m"; }

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="$(dirname "$TEST_DIR")"
CONVERTER="$SCRIPT_DIR/spe-to-csv.py"
CSV_SCRIPT="$SCRIPT_DIR/spe-csv.sh"
TEST_RESULTS_DIR="$TEST_DIR/test_results_csv"
TEST_PASSED=0
TEST_FAILED=0
MOCK_BIN="$TEST_RESULTS_DIR/mock_bin"
MOCK_DATA="$TEST_RESULTS_DIR/mock_perf.data"
MOCK_DATA_NOFIELDS="$TEST_RESULTS_DIR/mock_perf_nofields.data"

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

assert_not_contains() {
    local haystack="$1" needle="$2" message="${3:-AssertNotContains}"
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
    local expected="$1" actual="$2" message="${3:-AssertExitCode}"
    if [ "$actual" -eq "$expected" ]; then
        echo "  [PASS] $message"
        TEST_PASSED=$((TEST_PASSED + 1))
    else
        echo "  [FAIL] $message (期望 exit=$expected, 实际 exit=$actual)"
        TEST_FAILED=$((TEST_FAILED + 1))
    fi
}

assert_eq() {
    local expected="$1" actual="$2" message="${3:-AssertEq}"
    if [ "$actual" = "$expected" ]; then
        echo "  [PASS] $message"
        TEST_PASSED=$((TEST_PASSED + 1))
    else
        echo "  [FAIL] $message (期望 $expected, 实际 $actual)"
        TEST_FAILED=$((TEST_FAILED + 1))
    fi
}

# =============================================================================
# 夹具
# =============================================================================

setup_fixtures() {
    mkdir -p "$TEST_RESULTS_DIR" "$MOCK_BIN"

    # 完整字段的 perf script 输出（含 weight/data_src/addr，裸字段布局）
    cat > "$MOCK_DATA" <<'EOF'
claude 219296 [000] 12345.678: 1000000 l1d-miss: ffff80001000 func_hot+0x10 (/usr/lib/libc.so) 200 L1_or_0x0 0x7fffb000
claude 219296 [001] 12345.679: 1000000 l1d-miss: ffff80001000 func_hot+0x10 (/usr/lib/libc.so) 300 1 0x7fffb100
node 223175 [002] 12345.680: 1000000 llc-miss: ffff80002000 func_warm+0x20 (/usr/lib/libm.so) 900 6 0x7fffc000
claude 219296 [000] 12345.681: 1000000 tlb-miss: ffff80003000 func_tlb+0x8 (/usr/lib/libc.so) 1200 5 0x7fffd000
claude 219296 [001] 12345.683: 1000000 branch-miss: ffff80004000 func_br (/usr/lib/libc.so) 80 data_src:NA addr:0x0
EOF

    # 基础字段（无 weight/data_src），用于降级回退测试
    cat > "$MOCK_DATA_NOFIELDS" <<'EOF'
claude 219296 [000] 12345.678: 1000000 l1d-miss: ffff80001000 func_hot+0x10 (/usr/lib/libc.so)
node 223175 [002] 12345.680: 1000000 llc-miss: ffff80002000 func_warm+0x20 (/usr/lib/libm.so)
claude 219296 [001] 12345.683: 1000000 branch-miss: ffff80004000 func_br (/usr/lib/libc.so)
EOF

    # mock perf：支持 -F 时退出 1（模拟不支持 weight/data_src 的旧 perf），
    # 否则原样输出 mock 数据
    cat > "$MOCK_BIN/perf" <<EOF
#!/bin/bash
if [ "\$1" = "script" ]; then
    for a in "\$@"; do
        [ "\$a" = "-F" ] && exit 1
    done
    cat "$MOCK_DATA_NOFIELDS"
    exit 0
fi
exit 1
EOF
    chmod +x "$MOCK_BIN/perf"
}

run_conv() { python3 "$CONVERTER" "$@"; }

# =============================================================================
# 测试组
# =============================================================================

test_conversion() {
    blue "===== 测试组: 记录转换（spe-to-csv.py） ====="
    echo ""
    local out
    out=$(run_conv "$MOCK_DATA" 2>&1)
    assert_contains "$out" "comm,pid,tid,cpu,time,period,event,ip,sym,dso,weight,data_src,addr" "CSV 表头为标准 schema"
    assert_contains "$out" "claude,219296,,000,12345.678,1000000,l1d-miss,ffff80001000,func_hot+0x10,/usr/lib/libc.so,200,L1_or_0x0,0x7fffb000" "裸字段行正确解析 weight/data_src/addr"
    assert_contains "$out" "claude,219296,,001,12345.679,1000000,l1d-miss,ffff80001000,func_hot+0x10,/usr/lib/libc.so,300,1,0x7fffb100" "数值 data_src(1) 正确提取"
    assert_contains "$out" "node,223175,,002,12345.680,1000000,llc-miss,ffff80002000,func_warm+0x20,/usr/lib/libm.so,900,6,0x7fffc000" "数值 data_src(6) 正确提取"
    assert_contains "$out" "claude,219296,,001,12345.683,1000000,branch-miss,ffff80004000,func_br,/usr/lib/libc.so,80,NA,0x0" "关键字形式 data_src:NA 正确提取"
    echo ""

    local base_out
    base_out=$(run_conv "$MOCK_DATA_NOFIELDS" 2>&1)
    assert_contains "$base_out" "claude,219296,,000,12345.678,1000000,l1d-miss,ffff80001000,func_hot+0x10,/usr/lib/libc.so,,," "无扩展字段时 weight/data_src/addr 为空"
    echo ""
}

test_schema_output() {
    blue "===== 测试组: schema 说明与校验 ====="
    echo ""
    local out
    out=$(run_conv --schema 2>&1)
    assert_contains "$out" "comm,pid,tid,cpu,time,period,event,ip,sym,dso,weight,data_src,addr" "--schema 打印标准表头"
    assert_contains "$out" "weight" "--schema 含 weight 字段说明"
    assert_contains "$out" "data_src" "--schema 含 data_src 字段说明"
    echo ""

    local csv_file="$TEST_RESULTS_DIR/out.csv"
    run_conv "$MOCK_DATA" > "$csv_file"
    local vout
    vout=$(run_conv --validate "$csv_file" 2>&1)
    assert_contains "$vout" "校验通过" "合法 CSV 校验通过"
    assert_contains "$vout" "5" "校验输出样本行数 5"
    echo ""

    # 非法 CSV：坏表头
    local bad="$TEST_RESULTS_DIR/bad.csv"
    printf 'a,b,c\n1,2,3\n' > "$bad"
    local ret=0
    run_conv --validate "$bad" >/dev/null 2>&1 || ret=$?
    assert_exit_code 1 "$ret" "坏表头 CSV 校验失败"
    echo ""
}

test_csv_script() {
    blue "===== 测试组: spe-csv.sh 包装与降级 ====="
    echo ""
    # 用 mock perf（-F 失败 → 基础字段降级）
    local deg_csv="$TEST_RESULTS_DIR/degraded.csv"
    local deg_err="$TEST_RESULTS_DIR/degraded.err"
    local ret=0
    PATH="$MOCK_BIN:$PATH" bash "$CSV_SCRIPT" "$MOCK_DATA" > "$deg_csv" 2> "$deg_err" || ret=$?
    assert_exit_code 0 "$ret" "降级路径 exit=0"
    assert_contains "$(cat "$deg_csv")" "comm,pid,tid,cpu,time,period,event,ip,sym,dso,weight,data_src,addr" "降级后 CSV 仍为标准 schema"
    assert_contains "$(cat "$deg_err")" "回退为基础字段" "降级告警输出"
    assert_contains "$(cat "$deg_csv")" "func_hot+0x10,/usr/lib/libc.so,,," "降级行 weight/data_src/addr 为空"
    assert_eq 1 "$(grep -c '^comm,pid,tid,cpu,time,period,event,ip,sym,dso,weight,data_src,addr' "$deg_csv" 2>/dev/null || true)" "降级产物表头只出现一次（P1 回归）"
    local vout
    vout=$(run_conv --validate "$deg_csv" 2>&1)
    assert_contains "$vout" "校验通过" "降级产物通过 schema 校验（P1 回归）"
    echo ""

    # 外部 spe-parser 接入（模拟输出合法 CSV）
    local parser_fake="$TEST_RESULTS_DIR/fake_parser.sh"
    cat > "$parser_fake" <<'EOF'
#!/bin/bash
cat <<'CSVEOF'
comm,pid,tid,cpu,time,period,event,ip,sym,dso,weight,data_src,addr
fake,1,,0,1.0,1000,l1d-miss,ffff0000,fn+0x0,/bin/x,99,1,0x0
CSVEOF
EOF
    chmod +x "$parser_fake"
    out=$(PATH="$MOCK_BIN:$PATH" bash "$CSV_SCRIPT" --parser "$parser_fake" "$MOCK_DATA" 2>&1)
    assert_contains "$out" "fake,1,,0,1.0,1000,l1d-miss,ffff0000,fn+0x0,/bin/x,99,1,0x0" "外部 spe-parser 输出通过校验并被采纳"
    echo ""

    # 外部 spe-parser 输出非法 → 拒绝
    local parser_bad="$TEST_RESULTS_DIR/bad_parser.sh"
    printf '#!/bin/bash\nprintf "not-a-csv\\n"\n' > "$parser_bad"
    chmod +x "$parser_bad"
    ret=0
    PATH="$MOCK_BIN:$PATH" bash "$CSV_SCRIPT" --parser "$parser_bad" "$MOCK_DATA" >/dev/null 2>&1 || ret=$?
    assert_exit_code 1 "$ret" "非法 schema 的外部输出被拒绝"
    echo ""

    # 错误处理：文件不存在
    ret=0
    bash "$CSV_SCRIPT" /nonexistent.data >/dev/null 2>&1 || ret=$?
    assert_exit_code 1 "$ret" "文件不存在时 exit=1"
    echo ""
}

run_all_tests() {
    echo "========================================================================="
    yellow "spe-csv.sh / spe-to-csv.py 测试套件"
    echo "========================================================================="
    echo ""

    setup_fixtures
    test_conversion
    test_schema_output
    test_csv_script

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
