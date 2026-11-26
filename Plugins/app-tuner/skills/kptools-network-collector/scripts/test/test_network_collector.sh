#!/bin/bash
# =============================================================================
# kptools-network-collector 测试套件
# 覆盖语法、discover、collect 各模式、只读性、错误路径
# =============================================================================

set +e

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$TEST_DIR/../.." && pwd)"
SCRIPTS="$SKILL_DIR/scripts"
TEST_RESULTS_DIR="$TEST_DIR/test_results"
TEST_PASSED=0
TEST_FAILED=0
TEST_SKIPPED=0
BASH_BIN=$(command -v bash)

mkdir -p "$TEST_RESULTS_DIR"

red()    { echo -e "\033[31m$*\033[0m"; }
green()  { echo -e "\033[32m$*\033[0m"; }
yellow() { echo -e "\033[33m$*\033[0m"; }

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
test_group_end() { :; }

# =============================================================================
# 1. 语法验证
# =============================================================================

test_group_start "1. 语法验证"

for script in discover.sh collect.sh; do
    bash -n "$SCRIPTS/$script" 2>/dev/null
    assert_success $? "SYNTAX: $script 语法正确"
done

python3 -c "import json; json.load(open('$SKILL_DIR/evals/eval_queries.json'))" 2>/dev/null
assert_success $? "SYNTAX: eval_queries.json 合法 JSON"
python3 -c "import json; json.load(open('$SKILL_DIR/evals/evals.json'))" 2>/dev/null
assert_success $? "SYNTAX: evals.json 合法 JSON"

test_group_end

# =============================================================================
# 2. discover
# =============================================================================

test_group_start "2. discover 测试"

output=$(bash "$SCRIPTS/discover.sh" --help 2>&1); rc=$?
assert_success $rc "PC-D-001: --help 退出码"
output=$(bash "$SCRIPTS/discover.sh" 2>&1); rc=$?
assert_success $rc "NC-D-002: 退出码 0"
for tool in ip ss sar netstat nstat ethtool; do
    if echo "$output" | grep -qE "^$tool: (AVAILABLE|MISSING)"; then
        assert_success 0 "NC-D-002: $tool 有状态行"
    else
        assert_success 1 "NC-D-002: $tool 缺少状态行"
    fi
done

test_group_end

# =============================================================================
# 3. collect 各模式
# =============================================================================

IFACE=$(ip -o link show 2>/dev/null | awk -F': ' '/state UP/ {print $2}' | cut -d@ -f1 | head -1)

test_group_start "3. collect 模式测试（接口=${IFACE:-无}）"

echo "  NC-002: --help"
output=$(bash "$SCRIPTS/collect.sh" --help 2>&1); rc=$?
assert_success $rc "NC-002: --help 退出码"

echo "  NC-003: --iface"
output=$(bash "$SCRIPTS/collect.sh" --iface 2>&1); rc=$?
assert_success $rc "NC-003: 退出码"
if [ -n "$IFACE" ]; then
    assert_contains "$output" "$IFACE" "NC-003: 含 UP 接口"
fi

echo "  NC-004: --bandwidth"
if command -v sar &>/dev/null; then
    output=$(bash "$SCRIPTS/collect.sh" --bandwidth -d 2 2>&1); rc=$?
    assert_success $rc "NC-004: 退出码"
    assert_contains "$output" "IFACE" "NC-004: 含 IFACE 表"
else
    output=$(bash "$SCRIPTS/collect.sh" --bandwidth -d 2 2>&1); rc=$?
    assert_failure $rc "NC-004: sar 缺失时报错"
fi

echo "  NC-005: --drop"
output=$(bash "$SCRIPTS/collect.sh" --drop -d 2 2>&1); rc=$?
assert_success $rc "NC-005: 退出码"
assert_contains "$output" "rx_bytes_per_sec=" "NC-005: 含 delta 汇总"
assert_contains "$output" "drop_status=" "NC-005: 含状态行"
# 数值正确性回归：字段错位曾导致负速率与虚假 drop_detected
rx_rate=$(echo "$output" | grep -oE 'rx_bytes_per_sec=-?[0-9]+' | cut -d= -f2)
tx_rate=$(echo "$output" | grep -oE 'tx_bytes_per_sec=-?[0-9]+' | cut -d= -f2)
rx_drop=$(echo "$output" | grep -oE 'rx_drop_delta=-?[0-9]+' | cut -d= -f2)
tx_drop=$(echo "$output" | grep -oE 'tx_drop_delta=-?[0-9]+' | cut -d= -f2)
if [ -n "$rx_rate" ] && [ "$rx_rate" -ge 0 ] && [ -n "$tx_rate" ] && [ "$tx_rate" -ge 0 ] \
   && [ -n "$rx_drop" ] && [ "$rx_drop" -ge 0 ] && [ -n "$tx_drop" ] && [ "$tx_drop" -ge 0 ]; then
    assert_success 0 "NC-005: delta 数值均为非负（字段对应正确）"
else
    assert_success 1 "NC-005: delta 数值异常 rx_rate=$rx_rate tx_rate=$tx_rate rx_drop=$rx_drop tx_drop=$tx_drop"
fi
if echo "$output" | grep -qE 'drop_status=(none|drop_detected)$'; then
    assert_success 0 "NC-005: drop_status 取值合法"
else
    assert_success 1 "NC-005: drop_status 取值非法"
fi

echo "  NC-006: --retrans"
output=$(bash "$SCRIPTS/collect.sh" --retrans 2>&1); rc=$?
assert_success $rc "NC-006: 退出码"

echo "  NC-007: --sock"
output=$(bash "$SCRIPTS/collect.sh" --sock 2>&1); rc=$?
assert_success $rc "NC-007: 退出码"
output=$(bash "$SCRIPTS/collect.sh" --sock -p 1 2>&1); rc=$?
assert_success $rc "NC-007: -p 过滤退出码"

if [ -n "$IFACE" ] && command -v ethtool &>/dev/null; then
    echo "  NC-008: --nic -i $IFACE"
    output=$(bash "$SCRIPTS/collect.sh" --nic -i "$IFACE" 2>&1); rc=$?
    assert_success $rc "NC-008: 退出码"
    assert_contains "$output" "ethtool -l" "NC-008: 含队列段"
else
    skip "NC-008: 无接口或 ethtool 缺失"
fi

if [ -n "$IFACE" ]; then
    echo "  NC-009: --rss / --irq / --irqdelta / --numa / --softirq"
    output=$(bash "$SCRIPTS/collect.sh" --rss -i "$IFACE" 2>&1); rc=$?
    assert_success $rc "NC-009: --rss 退出码"
    output=$(bash "$SCRIPTS/collect.sh" --irq -i "$IFACE" 2>&1); rc=$?
    assert_success $rc "NC-009: --irq 退出码"
    output=$(bash "$SCRIPTS/collect.sh" --irqdelta -i "$IFACE" -d 2 2>&1); rc=$?
    assert_success $rc "NC-009: --irqdelta 退出码"
    if echo "$output" | grep -q "max_min_ratio\|active_irqs=0\|matched_irqs=0"; then
        assert_success 0 "NC-009: 含 ratio 或无活跃 IRQ"
    else
        assert_success 1 "NC-009: 缺少 delta 汇总"
    fi
    # 数值正确性回归：快照丢冒号曾导致恒报 active_irqs=0
    output=$(bash "$SCRIPTS/collect.sh" --irqdelta -d 1 2>&1); rc=$?
    assert_success $rc "NC-009: --irqdelta（默认 pattern）退出码"
    matched=$(grep -cE "eth|enp|ens|eno|bond|virtio|mlx5" /proc/interrupts 2>/dev/null || true)
    delta_lines=$(echo "$output" | grep -cE '^irq [0-9]+: delta=' || true)
    if [ "$matched" -eq 0 ] || [ "$delta_lines" -eq "$matched" ]; then
        assert_success 0 "NC-009: delta 行数与匹配 IRQ 行数一致（$delta_lines/$matched）"
    else
        assert_success 1 "NC-009: delta 行数 $delta_lines != 匹配 IRQ 行数 $matched（采样数据丢失）"
    fi
    output=$(bash "$SCRIPTS/collect.sh" --numa 2>&1); rc=$?
    assert_success $rc "NC-009: --numa 退出码"
    output=$(bash "$SCRIPTS/collect.sh" --softirq 2>&1); rc=$?
    assert_success $rc "NC-009: --softirq 退出码"
    assert_contains "$output" "NET_RX" "NC-009: softirq 含 NET_RX"
else
    skip "NC-009: 无 UP 接口"
fi

echo "  NC-010: 不存在的接口"
output=$(bash "$SCRIPTS/collect.sh" --nic -i nosuchif0 2>&1); rc=$?
assert_failure $rc "NC-010: 退出码非 0"
assert_contains "$output" "does not exist" "NC-010: 报接口不存在"

echo "  NC-011: 只读性（IRQ 绑定不变）"
if [ -n "$IFACE" ]; then
    before_irq=$(cat /proc/irq/*/smp_affinity_list 2>/dev/null | md5sum)
    # 对照窗口：宿主机 irqbalance 会并发变更 IRQ 绑定（容器环境实测存在），
    # 先用与采集等长的空窗口检测干扰；有干扰则只读性在此环境无法验证，跳过
    sleep 15
    control_irq=$(cat /proc/irq/*/smp_affinity_list 2>/dev/null | md5sum)
    bash "$SCRIPTS/collect.sh" --all >/dev/null 2>&1
    after_irq=$(cat /proc/irq/*/smp_affinity_list 2>/dev/null | md5sum)
    if [ "$before_irq" != "$control_irq" ]; then
        skip "NC-011: 宿主机 irqbalance 正在变更 IRQ 绑定，只读性无法在此环境验证"
    elif [ "$control_irq" != "$after_irq" ]; then
        assert_success 1 "NC-011: IRQ 绑定发生变化"
    else
        assert_success 0 "NC-011: IRQ 绑定未变"
    fi
else
    skip "NC-011: 无 UP 接口"
fi

echo "  NC-012: --all"
output=$(bash "$SCRIPTS/collect.sh" --all 2>&1); rc=$?
assert_success $rc "NC-012: --all 退出码"

echo "  NC-013: --all 单模式失败不中断（缺工具时错误隔离）"
# PATH 只保留 dirname（脚本头需要），ip/sar/ss/ethtool/python3 等全部缺失：
# 每个模式都会触发 require_tool exit——修复前脚本在首个失败模式处整体终止
NC013_SHIM="$TEST_RESULTS_DIR/nc013-shim.$$"
mkdir -p "$NC013_SHIM"
ln -sf "$(command -v dirname)" "$NC013_SHIM/dirname"
# 用绝对路径调 bash（shim PATH 中无 bash 本身，PATH= 赋值作用于整条命令行）
output=$(PATH="$NC013_SHIM" "$BASH_BIN" "$SCRIPTS/collect.sh" --all -d 1 2>&1); rc=$?
rm -rf "$NC013_SHIM"
assert_failure $rc "NC-013: 存在失败模式时退出码非 0"
nmiss=""
for m in --iface --bandwidth --drop --retrans --sock --nic --rss --irq --irqdelta --numa --softirq; do
    echo "$output" | grep -qF -- "========== $m" || nmiss="$nmiss $m"
done
if [ -z "$nmiss" ]; then
    assert_success 0 "NC-013: 失败后所有模式标记仍出现（错误隔离生效）"
else
    assert_success 1 "NC-013: 缺少模式标记:$nmiss"
fi

echo "  NC-014: --all 指定不存在接口仍不中断"
output=$(bash "$SCRIPTS/collect.sh" --all -i nc-test-nonexistent-if -d 1 2>&1); rc=$?
assert_failure $rc "NC-014: 退出码非 0"
if echo "$output" | grep -qF -- "========== --softirq"; then
    assert_success 0 "NC-014: 最后一个模式标记仍出现"
else
    assert_success 1 "NC-014: 流程被提前中断"
fi

test_group_end

# =============================================================================
# 4. 错误路径
# =============================================================================

test_group_start "4. 错误路径"

output=$(bash "$SCRIPTS/collect.sh" 2>&1); rc=$?
assert_failure $rc "无模式时报错"
output=$(bash "$SCRIPTS/collect.sh" --bogus 2>&1); rc=$?
assert_failure $rc "未知模式报错"
output=$(bash "$SCRIPTS/collect.sh" --drop -d 0 2>&1); rc=$?
assert_failure $rc "-d 0 报错"
assert_contains "$output" "must be a positive integer" "-d 0 含校验报错"
output=$(bash "$SCRIPTS/collect.sh" --drop -d 2>&1); rc=$?
assert_failure $rc "-d 缺值报错"
assert_contains "$output" "requires a value" "-d 缺值含提示"

test_group_end

# =============================================================================
# 汇总
# =============================================================================

echo ""
echo "============================================"
echo "  测试汇总"
echo "============================================"
green "  PASS: $TEST_PASSED"
red   "  FAIL: $TEST_FAILED"
yellow "  SKIP: $TEST_SKIPPED"
echo "  总计: $((TEST_PASSED + TEST_FAILED + TEST_SKIPPED))"
echo "============================================"

if [ "$TEST_FAILED" -gt 0 ]; then
    exit 1
fi
exit 0
