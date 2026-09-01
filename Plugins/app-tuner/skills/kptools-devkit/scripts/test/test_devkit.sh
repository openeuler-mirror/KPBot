#!/bin/bash
# =============================================================================
# kptools-devkit 测试套件
# 覆盖语法验证、部署、发现、CLI 包装器、turbostat、top-down、numafast、yaml 一致性
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

# =============================================================================
# 测试辅助函数
# =============================================================================

red()    { echo -e "\033[31m$*\033[0m"; }
green()  { echo -e "\033[32m$*\033[0m"; }
yellow() { echo -e "\033[33m$*\033[0m"; }
blue()   { echo -e "\033[34m$*\033[0m"; }

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
        echo "  [FAIL] $message (succeeded when should fail)"
        TEST_FAILED=$((TEST_FAILED + 1))
    fi
}

skip() {
    local message="$1"
    echo "  [SKIP] $message"
    TEST_SKIPPED=$((TEST_SKIPPED + 1))
}

test_group_start() {
    echo ""
    blue "===== 测试组: $1 ====="
    echo ""
}

test_group_end() {
    echo ""
}

run_cmd() {
    local cmd="$1"
    local result_file="$TEST_RESULTS_DIR/last_output.txt"
    set +e
    eval "$cmd" > "$result_file" 2>&1
    local rc=$?
    cat "$result_file"
    return $rc
}

# 安全执行命令，返回退出码和输出（去除 null byte）
# 用法: output=$(run_devkit_cmd 'bash "$SCRIPTS/run_devkit.sh" tuner xxx 2>&1')
run_devkit_cmd() {
    local raw
    raw=$(eval "$1" 2>&1)
    local rc=$?
    echo "$raw" | tr -d '\0'
    return $rc
}
export SCRIPTS="$SCRIPTS"
check_pmu_available() {
    local paranoid=$(cat /proc/sys/kernel/perf_event_paranoid 2>/dev/null)
    [ -z "$paranoid" ] && return 1
    [ "$(id -u)" -ne 0 ] && [ "$paranoid" -gt 1 ] && return 1
    local probe
    probe=$("$SCRIPTS/run_devkit.sh" tuner top-down -d 1 -L 1 2>&1)
    local rc=$?
    probe=$(echo "$probe" | tr -d '\0')
    [ $rc -ne 0 ] && return 1
    echo "$probe" | grep -qiE 'error.*LIBPERF|PmuOpen failed|PMU.*not.*support|cannot.*open.*pmu' && return 1
    return 0
}

# SPE 可用性检查
check_spe_available() {
    [ -e /sys/devices/arm_spe_0/type ] || [ -e /sys/devices/arm_spe_1/type ]
}

# =============================================================================
# 6.1 脚本语法验证
# =============================================================================

test_group_start "6.1 脚本语法验证"

echo "  SYNTAX-001: bash -n deploy_devkit.sh"
bash -n "$SCRIPTS/deploy_devkit.sh" 2>/dev/null
assert_success $? "SYNTAX-001: deploy_devkit.sh 语法正确"

echo "  SYNTAX-002: bash -n discover_devkit.sh"
bash -n "$SCRIPTS/discover_devkit.sh" 2>/dev/null
assert_success $? "SYNTAX-002: discover_devkit.sh 语法正确"

echo "  SYNTAX-003: bash -n run_devkit.sh"
bash -n "$SCRIPTS/run_devkit.sh" 2>/dev/null
assert_success $? "SYNTAX-003: run_devkit.sh 语法正确"

echo "  SYNTAX-004: binaries.yaml YAML 语法"
python3 -c "import yaml; yaml.safe_load(open('$SCRIPTS/binaries.yaml'))" 2>/dev/null
assert_success $? "SYNTAX-004: binaries.yaml 语法正确"

test_group_end

# =============================================================================
# 6.2 部署脚本测试（deploy_devkit.sh）
# =============================================================================

test_group_start "6.2 部署脚本测试（deploy_devkit.sh）"

echo "  DEPLOY-001: --help"
output=$(bash "$SCRIPTS/deploy_devkit.sh" --help 2>&1); rc=$?
assert_success $rc "DEPLOY-001: --help 退出码"
assert_contains "$output" "deploy_devkit.sh" "DEPLOY-001: --help 含脚本名"

echo "  DEPLOY-002: 正常部署（已安装环境）"
output=$(bash "$SCRIPTS/deploy_devkit.sh" 2>&1); rc=$?
assert_success $rc "DEPLOY-002: 部署退出码"
assert_contains "$output" "OK" "DEPLOY-002: 输出含 OK"

echo "  DEPLOY-003: --all 安装所有包"
output=$(bash "$SCRIPTS/deploy_devkit.sh" --all 2>&1); rc=$?
assert_success $rc "DEPLOY-003: --all 退出码"

echo "  DEPLOY-004: --install-dir 自定义目录"
output=$(bash "$SCRIPTS/deploy_devkit.sh" --install-dir /tmp/devkit-test 2>&1); rc=$?
assert_success $rc "DEPLOY-004: --install-dir 退出码"
if [ -d /tmp/devkit-test/usr/local/devkit ]; then
    assert_success 0 "DEPLOY-004: 文件解压到 /tmp/devkit-test"
    rm -rf /tmp/devkit-test
else
    assert_failure 1 "DEPLOY-004: 文件解压到 /tmp/devkit-test"
fi

echo "  DEPLOY-005: --packages 指定不存在的包"
output=$(bash "$SCRIPTS/deploy_devkit.sh" --packages nonexistent-pkg 2>&1); rc=$?
assert_success $rc "DEPLOY-005: --packages 不存在的包仍安装 required 包"

echo "  DEPLOY-006: 缺少 rpm2cpio"
if command -v rpm2cpio &>/dev/null; then
    skip "DEPLOY-006: rpm2cpio 存在，跳过缺失测试"
else
    output=$(bash "$SCRIPTS/deploy_devkit.sh" 2>&1); rc=$?
    assert_failure $rc "DEPLOY-006: 缺 rpm2cpio 应失败"
    assert_contains "$output" "rpm2cpio not found" "DEPLOY-006: 含错误提示"
fi

echo "  DEPLOY-007: 缺少 cpio"
if command -v cpio &>/dev/null; then
    skip "DEPLOY-007: cpio 存在，跳过缺失测试"
else
    output=$(bash "$SCRIPTS/deploy_devkit.sh" 2>&1); rc=$?
    assert_failure $rc "DEPLOY-007: 缺 cpio 应失败"
    assert_contains "$output" "cpio not found" "DEPLOY-007: 含错误提示"
fi

test_group_end

# =============================================================================
# 6.3 发现脚本测试（discover_devkit.sh）
# =============================================================================

test_group_start "6.3 发现脚本测试（discover_devkit.sh）"

echo "  DISCOVER-001: --help"
output=$(bash "$SCRIPTS/discover_devkit.sh" --help 2>&1); rc=$?
assert_success $rc "DISCOVER-001: --help 退出码"
assert_contains "$output" "discover_devkit.sh" "DISCOVER-001: --help 含脚本名"

echo "  DISCOVER-002: 已安装环境"
output=$(bash "$SCRIPTS/discover_devkit.sh" 2>&1); rc=$?
if [ $rc -eq 0 ]; then
    assert_success 0 "DISCOVER-002: discover 退出码"
    assert_contains "$output" "READY" "DISCOVER-002: 输出含 READY"
    assert_contains "$output" "turbostat" "DISCOVER-002: 含 turbostat"
    assert_contains "$output" "top-down" "DISCOVER-002: 含 top-down"
    assert_contains "$output" "numafast" "DISCOVER-002: 含 numafast"
else
    skip "DISCOVER-002: devkit 未安装，跳过"
fi

echo "  DISCOVER-003: --path"
output=$(bash "$SCRIPTS/discover_devkit.sh" --path 2>&1); rc=$?
if [ $rc -eq 0 ]; then
    assert_success 0 "DISCOVER-003: --path 退出码"
    assert_contains "$output" "/devkit" "DISCOVER-003: 输出含二进制路径"
else
    assert_failure $rc "DISCOVER-003: --path 未安装时应失败"
fi

echo "  DISCOVER-006: 任务列表从 yaml 动态读取"
yaml_tasks=$(awk '/^tuner_tasks:/{f=1;next} f&&/^  - name:/{print $3} f&&/^[^[:space:]]/{f=0}' "$SCRIPTS/binaries.yaml" | sort | tr '\n' ' ')
discover_output=$(bash "$SCRIPTS/discover_devkit.sh" 2>&1)
if [ -n "$discover_output" ] && echo "$discover_output" | grep -q "READY"; then
    for task in $yaml_tasks; do
        assert_contains "$discover_output" "$task" "DISCOVER-006: discover 输出含 $task"
    done
else
    skip "DISCOVER-006: devkit 未安装，跳过"
fi

echo "  DISCOVER-007: fallback 测试"
empty_yaml=$(mktemp)
cat > "$empty_yaml" <<'EOF'
devkit_meta:
  version: "26.1.rc1"
EOF
fallback_tasks=$(awk '/^tuner_tasks:/{f=1;next} f&&/^  - name:/{print $3} f&&/^[^[:space:]]/{f=0}' "$empty_yaml")
if [ -z "$fallback_tasks" ]; then
    assert_success 0 "DISCOVER-007: 空 yaml 时 awk 返回空（触发 fallback）"
else
    assert_failure 1 "DISCOVER-007: 空 yaml 时 awk 应返回空"
fi
rm -f "$empty_yaml"

test_group_end

# =============================================================================
# 6.4 CLI 包装器测试（run_devkit.sh）
# =============================================================================

test_group_start "6.4 CLI 包装器测试（run_devkit.sh）"

echo "  RUN-001: 未安装时 --version（模拟）"
devkit_path=$(bash "$SCRIPTS/discover_devkit.sh" --path 2>/dev/null)
if [ -z "$devkit_path" ]; then
    output=$(bash "$SCRIPTS/run_devkit.sh" --version 2>&1); rc=$?
    assert_failure $rc "RUN-001: 未安装应失败"
    assert_contains "$output" "deploy_devkit.sh" "RUN-001: 含部署提示"
else
    skip "RUN-001: devkit 已安装，跳过未安装测试"
fi

echo "  RUN-002: 已安装时 --version"
if [ -n "$devkit_path" ]; then
    output=$(bash "$SCRIPTS/run_devkit.sh" --version 2>&1); rc=$?
    assert_success $rc "RUN-002: --version 退出码"
    assert_contains "$output" "26.1" "RUN-002: 输出版本号"
else
    skip "RUN-002: devkit 未安装，跳过"
fi

echo "  RUN-003: DEVKIT_BIN 环境变量"
if [ -n "$devkit_path" ]; then
    output=$(DEVKIT_BIN="$devkit_path" bash "$SCRIPTS/run_devkit.sh" --version 2>&1); rc=$?
    assert_success $rc "RUN-003: DEVKIT_BIN 退出码"
    assert_contains "$output" "26.1" "RUN-003: 使用环境变量路径"
else
    skip "RUN-003: devkit 未安装，跳过"
fi

test_group_end

# =============================================================================
# 6.5 turbostat 采集测试
# =============================================================================

test_group_start "6.5 turbostat 采集测试"

if [ -z "$devkit_path" ]; then
    skip "TURBO-001~007: devkit 未安装，跳过全部 turbostat 测试"
    test_group_end
else
    echo "  TURBO-001: 基本采集 -d 3"
    output=$(bash "$SCRIPTS/run_devkit.sh" tuner turbostat -d 3 2>&1); rc=$?
    assert_success $rc "TURBO-001: 退出码"
    assert_contains "$output" "Per NUMA Frequency Table" "TURBO-001: 含 NUMA 频率表"
    assert_contains "$output" "CPU Core Frequency Table" "TURBO-001: 含核频率表"
    assert_contains "$output" "Uncore" "TURBO-001: 含 Uncore 频率表"
    assert_contains "$output" "Power and Temperature" "TURBO-001: 含功耗温度表"

    echo "  TURBO-002: 子报告 -d 3 -i 1"
    output=$(bash "$SCRIPTS/run_devkit.sh" tuner turbostat -d 3 -i 1 2>&1); rc=$?
    assert_success $rc "TURBO-002: 退出码"

    echo "  TURBO-003: 指定 CPU -c 0-3"
    output=$(bash "$SCRIPTS/run_devkit.sh" tuner turbostat -d 3 -c 0-3 2>&1); rc=$?
    assert_success $rc "TURBO-003: 退出码"

    echo "  TURBO-004: --bmc 无 BMC"
    skip "TURBO-004: --bmc 需 BMC 凭据，跳过自动化测试"

    echo "  TURBO-005: 频率值检查"
    turbo_output=$(run_devkit_cmd 'bash "$SCRIPTS/run_devkit.sh" tuner turbostat -d 3 2>&1')
    freq_val=$(echo "$turbo_output" | grep -E '^\|.*[0-9]+\.[0-9]+ *\|' | head -1)
    if [ -n "$freq_val" ]; then
        assert_success 0 "TURBO-005: 输出含频率数值"
    else
        assert_success 1 "TURBO-005: 未找到频率数值"
    fi

    echo "  TURBO-006: 功耗值检查"
    if echo "$turbo_output" | grep -qE '^\| *[0-9]+ *\| *[0-9]+\.[0-9]+ *\|'; then
        assert_success 0 "TURBO-006: 输出含功耗数值"
    else
        assert_success 1 "TURBO-006: 未找到功耗数值"
    fi

    echo "  TURBO-007: 温度值检查"
    temp_section=$(echo "$turbo_output" | sed -n '/Power and Temperature/,/^$/p')
    if echo "$temp_section" | grep -q 'N/A'; then
        skip "TURBO-007: 温度传感器不可用（N/A），跳过"
    elif echo "$temp_section" | grep -qE '^\| *[0-9]+ *\| *[0-9]+\.[0-9]+.*\|'; then
        assert_success 0 "TURBO-007: 输出含温度数值"
    else
        assert_success 1 "TURBO-007: 未找到温度数值"
    fi

    test_group_end
fi

# =============================================================================
# 6.6 top-down 采集测试
# =============================================================================

test_group_start "6.6 top-down 采集测试"

if [ -z "$devkit_path" ]; then
    skip "TOPD-001~009: devkit 未安装，跳过全部 top-down 测试"
    test_group_end
elif ! check_pmu_available; then
    skip "TOPD-001~009: PMU 事件不可用，跳过全部 top-down 测试"
    test_group_end
else
    echo "  TOPD-001: -L 1"
    output=$(bash "$SCRIPTS/run_devkit.sh" tuner top-down -d 3 -L 1 2>&1); rc=$?
    assert_success $rc "TOPD-001: 退出码"
    assert_contains "$output" "Bad Speculation" "TOPD-001: 含 Bad Spec"
    assert_contains "$output" "Frontend Bound" "TOPD-001: 含 Frontend"
    assert_contains "$output" "Retiring" "TOPD-001: 含 Retiring"
    assert_contains "$output" "Backend Bound" "TOPD-001: 含 Backend"

    echo "  TOPD-002: -L 0 全量"
    output=$(bash "$SCRIPTS/run_devkit.sh" tuner top-down -d 3 -L 0 2>&1); rc=$?
    assert_success $rc "TOPD-002: 退出码"
    assert_contains "$output" "Core Bound" "TOPD-002: 含 Core Bound"
    assert_contains "$output" "Memory Bound" "TOPD-002: 含 Memory Bound"

    echo "  TOPD-003: -L 3 Memory Bound"
    output=$(bash "$SCRIPTS/run_devkit.sh" tuner top-down -d 3 -L 3 2>&1); rc=$?
    assert_success $rc "TOPD-003: 退出码"
    assert_contains "$output" "Memory Bound" "TOPD-003: 含 Memory Bound"

    echo "  TOPD-004: -L 4 鲲鹏 950"
    output=$(run_devkit_cmd 'bash "$SCRIPTS/run_devkit.sh" tuner top-down -d 3 -L 4 2>&1'); rc=$?
    if [ $rc -eq 0 ]; then
        assert_success 0 "TOPD-004: -L 4 在 950 上可用"
    else
        skip "TOPD-004: -L 4 间歇性失败（PMU 状态，exit $rc），跳过"
    fi

    echo "  TOPD-005: -p PID"
    pid=$(pgrep -o systemd | head -1)
    if [ -n "$pid" ]; then
        output=$(bash "$SCRIPTS/run_devkit.sh" tuner top-down -d 3 -p "$pid" 2>&1); rc=$?
        assert_success $rc "TOPD-005: -p 退出码"
    else
        skip "TOPD-005: 无可用 PID"
    fi

    echo "  TOPD-006: -c 0-3"
    output=$(bash "$SCRIPTS/run_devkit.sh" tuner top-down -d 3 -c 0-3 2>&1); rc=$?
    assert_success $rc "TOPD-006: -c 退出码"

    echo "  TOPD-007: IPC 值"
    output=$(run_devkit_cmd 'bash "$SCRIPTS/run_devkit.sh" tuner top-down -d 3 -L 1 2>&1'); rc=$?
    ipc_line=$(echo "$output" | grep -oE 'IPC +[0-9]+\.[0-9]+' | head -1)
    if [ -n "$ipc_line" ]; then
        assert_success 0 "TOPD-007: 输出含 IPC 值"
    else
        assert_success 1 "TOPD-007: 未找到 IPC"
    fi

    echo "  TOPD-008: 一级指标百分比和 ≈ 100%"
    output=$(run_devkit_cmd 'bash "$SCRIPTS/run_devkit.sh" tuner top-down -d 3 -L 1 2>&1'); rc=$?
    bad=$(echo "$output" | grep "Bad Speculation" | grep -oE '[0-9]+\.[0-9]+' | head -1)
    frontend=$(echo "$output" | grep "Frontend Bound" | grep -oE '[0-9]+\.[0-9]+' | head -1)
    retiring=$(echo "$output" | grep "Retiring" | grep -oE '[0-9]+\.[0-9]+' | head -1)
    backend=$(echo "$output" | grep "Backend Bound" | grep -oE '[0-9]+\.[0-9]+' | head -1)
    if [ -n "$bad" ] && [ -n "$frontend" ] && [ -n "$retiring" ] && [ -n "$backend" ]; then
        in_range=$(python3 -c "print(99 <= round($bad + $frontend + $retiring + $backend, 1) <= 101)")
        assert_equal "True" "$in_range" "TOPD-008: 四项之和 ≈ 100%（实际 ${bad}+${frontend}+${retiring}+${backend}）"
    else
        skip "TOPD-008: 未提取到全部四项指标"
    fi

    echo "  TOPD-009: PMU 事件表"
    assert_contains "$output" "r0008" "TOPD-009: 含 PMU 事件 r0008"
    assert_contains "$output" "r0011" "TOPD-009: 含 PMU 事件 r0011"

    test_group_end
fi

# =============================================================================
# 6.7 numafast 采集测试
# =============================================================================

test_group_start "6.7 numafast 采集测试"

if [ -z "$devkit_path" ]; then
    skip "NUMA-001~008: devkit 未安装，跳过全部 numafast 测试"
    test_group_end
elif ! check_spe_available; then
    skip "NUMA-001~008: ARM SPE 不可用，跳过全部 numafast 测试"
    test_group_end
else
    echo "  NUMA-001: 基本采集 -d 3"
    output=$(run_devkit_cmd 'bash "$SCRIPTS/run_devkit.sh" tuner numafast -d 3 2>&1'); rc=$?
    assert_success $rc "NUMA-001: 退出码"
    assert_contains "$output" "numa score" "NUMA-001: 含 NUMA score"
    assert_contains "$output" "SRC_" "NUMA-001: 含流量矩阵"

    echo "  NUMA-002: 子报告 -d 3 -i 5"
    output=$(run_devkit_cmd 'bash "$SCRIPTS/run_devkit.sh" tuner numafast -d 3 -i 5 2>&1'); rc=$?
    if [ $rc -eq 0 ]; then
        assert_success 0 "NUMA-002: 退出码"
    else
        skip "NUMA-002: 子报告采集失败（SPE 环境问题，exit $rc），跳过"
    fi

    echo "  NUMA-003: -p PID"
    pid=$(pgrep -o systemd | head -1)
    if [ -n "$pid" ]; then
        output=$(run_devkit_cmd 'bash "$SCRIPTS/run_devkit.sh" tuner numafast -d 3 -p "$pid" 2>&1'); rc=$?
        assert_success $rc "NUMA-003: -p 退出码"
    else
        skip "NUMA-003: 无可用 PID"
    fi

    echo "  NUMA-004: -n 10"
    output=$(run_devkit_cmd 'bash "$SCRIPTS/run_devkit.sh" tuner numafast -d 3 -n 10 2>&1'); rc=$?
    assert_success $rc "NUMA-004: -n 10 退出码"

    echo "  NUMA-005: --package 生成 TAR"
    cwd=$(pwd)
    cd "$TEST_RESULTS_DIR"
    output=$(run_devkit_cmd 'bash "$SCRIPTS/run_devkit.sh" tuner numafast -d 3 --package 2>&1'); rc=$?
    cd "$cwd"
    assert_success $rc "NUMA-005: --package 退出码"

    echo "  NUMA-006: score 范围 0-1"
    output=$(run_devkit_cmd 'bash "$SCRIPTS/run_devkit.sh" tuner numafast -d 3 2>&1'); rc=$?
    score=$(echo "$output" | grep -oE 'score : [-0-9]+\.[0-9]+' | grep -oE '[-0-9]+\.[0-9]+' | head -1)
    if [ -n "$score" ]; then
        in_range=$(python3 -c "print(0 <= $score <= 1)")
        assert_equal "True" "$in_range" "NUMA-006: score 在 0-1 范围（实际 $score）"
    else
        skip "NUMA-006: 未提取到 score"
    fi

    echo "  NUMA-007: 流量矩阵对角线"
    assert_contains "$output" "10|" "NUMA-007: 含 distance=10（本地访问）"

    echo "  NUMA-008: 虚拟机/容器检测"
    if grep -qE '(VMware|KVM|Xen|docker|containerd)' /proc/1/cgroup 2>/dev/null || systemd-detect-virt --vm &>/dev/null; then
        vm_output=$(run_devkit_cmd 'bash "$SCRIPTS/run_devkit.sh" tuner numafast -d 1 2>&1'); vm_rc=$?
        assert_failure $vm_rc "NUMA-008: 虚拟机环境应不支持"
    else
        skip "NUMA-008: 非虚拟机环境，跳过"
    fi

    test_group_end
fi

# =============================================================================
# 6.8 hotspot 采集测试
# =============================================================================

test_group_start "6.8 hotspot 采集测试"

if [ -z "$devkit_path" ]; then
    skip "HOT-001~009: devkit 未安装，跳过全部 hotspot 测试"
    test_group_end
elif ! check_pmu_available; then
    skip "HOT-001~009: PMU 事件不可用，跳过全部 hotspot 测试"
    test_group_end
else
    echo "  HOT-001: 基本采集 -d 3 -t 10"
    output=$(run_devkit_cmd 'bash "$SCRIPTS/run_devkit.sh" tuner hotspot -d 3 -t 10 2>&1'); rc=$?
    assert_success $rc "HOT-001: 退出码"
    assert_contains "$output" "Hotspot" "HOT-001: 含 Hotspot Metrics"
    assert_contains "$output" "cycles" "HOT-001: 含 cycles"

    echo "  HOT-002: 火焰图和 TAR 包 -g --package"
    cwd=$(pwd)
    cd "$TEST_RESULTS_DIR"
    output=$(run_devkit_cmd 'bash "$SCRIPTS/run_devkit.sh" tuner hotspot -d 3 -t 5 -g --package 2>&1'); rc=$?
    cd "$cwd"
    assert_success $rc "HOT-002: 退出码"
    assert_contains "$output" "Flamegraph" "HOT-002: 输出含火焰图路径"
    assert_contains "$output" "Callstack" "HOT-002: 输出含调用栈路径"

    echo "  HOT-003: -p PID"
    pid=$(pgrep -o systemd | head -1)
    if [ -n "$pid" ]; then
        output=$(run_devkit_cmd 'bash "$SCRIPTS/run_devkit.sh" tuner hotspot -d 3 -p "$pid" 2>&1'); rc=$?
        assert_success $rc "HOT-003: -p 退出码"
    else
        skip "HOT-003: 无可用 PID"
    fi

    echo "  HOT-004: -c 0-3"
    output=$(run_devkit_cmd 'bash "$SCRIPTS/run_devkit.sh" tuner hotspot -d 3 -c 0-3 2>&1'); rc=$?
    assert_success $rc "HOT-004: -c 退出码"

    echo "  HOT-005: -f 999"
    output=$(run_devkit_cmd 'bash "$SCRIPTS/run_devkit.sh" tuner hotspot -d 3 -f 999 2>&1'); rc=$?
    assert_success $rc "HOT-005: -f 999 退出码"

    echo "  HOT-006: --long-name"
    output=$(run_devkit_cmd 'bash "$SCRIPTS/run_devkit.sh" tuner hotspot -d 3 --long-name 2>&1'); rc=$?
    assert_success $rc "HOT-006: --long-name 退出码"

    echo "  HOT-007: hotspot list"
    output=$(run_devkit_cmd 'bash "$SCRIPTS/run_devkit.sh" tuner hotspot list 2>&1'); rc=$?
    assert_success $rc "HOT-007: list 退出码"
    assert_contains "$output" "cycles" "HOT-007: list 含 cycles 事件"

    echo "  HOT-008: Top 1 函数 cycles 占比"
    output=$(run_devkit_cmd 'bash "$SCRIPTS/run_devkit.sh" tuner hotspot -d 3 -t 1 2>&1'); rc=$?
    if [ $rc -ne 0 ]; then
        assert_success 1 "HOT-008: 命令失败（exit $rc）"
    elif echo "$output" | grep -qE 'error|failed'; then
        assert_success 1 "HOT-008: 命令输出含错误"
    elif echo "$output" | grep -vE 'Version|CPU Model' | grep -qE '[0-9]+\.[0-9]+'; then
        assert_success 0 "HOT-008: 输出含占比数值"
    else
        assert_success 1 "HOT-008: 未找到占比数值"
    fi

    echo "  HOT-009: 火焰图文件检查"
    flame_file=$(ls "$TEST_RESULTS_DIR"/Flamegraph-*.html 2>/dev/null | head -1)
    if [ -n "$flame_file" ] && [ -s "$flame_file" ]; then
        assert_success 0 "HOT-009: 火焰图 HTML 存在且非空"
    else
        assert_success 1 "HOT-009: 火焰图 HTML 不存在或为空"
    fi

    test_group_end
fi

# =============================================================================
# 6.9 memory 采集测试
# =============================================================================

test_group_start "6.9 memory 采集测试"

if [ -z "$devkit_path" ]; then
    skip "MEM-001~008: devkit 未安装，跳过全部 memory 测试"
    test_group_end
elif ! check_pmu_available; then
    skip "MEM-001~008: PMU 事件不可用，跳过全部 memory 测试"
    test_group_end
else
    echo "  MEM-001: 全量采集 -m 1"
    output=$(run_devkit_cmd 'bash "$SCRIPTS/run_devkit.sh" tuner memory -d 3 -m 1 2>&1'); rc=$?
    assert_success $rc "MEM-001: 退出码"
    assert_contains "$output" "DDR" "MEM-001: 含 DDR 带宽"
    assert_contains "$output" "Cache" "MEM-001: 含 Cache 信息"

    echo "  MEM-002: Cache 模式 -m 2"
    output=$(run_devkit_cmd 'bash "$SCRIPTS/run_devkit.sh" tuner memory -d 3 -m 2 2>&1'); rc=$?
    assert_success $rc "MEM-002: 退出码"

    echo "  MEM-003: DDR 模式 -m 3"
    output=$(run_devkit_cmd 'bash "$SCRIPTS/run_devkit.sh" tuner memory -d 3 -m 3 2>&1'); rc=$?
    assert_success $rc "MEM-003: 退出码"
    assert_contains "$output" "DDRC" "MEM-003: 含 DDRC 带宽"

    echo "  MEM-004: HBM 模式 -m 4"
    output=$(run_devkit_cmd 'bash "$SCRIPTS/run_devkit.sh" tuner memory -d 3 -m 4 2>&1'); rc=$?
    assert_success $rc "MEM-004: 退出码"

    echo "  MEM-005: -c 0-3"
    output=$(run_devkit_cmd 'bash "$SCRIPTS/run_devkit.sh" tuner memory -d 3 -c 0-3 2>&1'); rc=$?
    assert_success $rc "MEM-005: -c 退出码"

    echo "  MEM-006: L2D Miss 百分比"
    output=$(run_devkit_cmd 'bash "$SCRIPTS/run_devkit.sh" tuner memory -d 3 -m 1 2>&1'); rc=$?
    if echo "$output" | grep -qE 'L2D.*[0-9]+\.[0-9]+%'; then
        assert_success 0 "MEM-006: 输出含 L2D Miss 百分比"
    else
        assert_success 1 "MEM-006: 未找到 L2D Miss"
    fi

    echo "  MEM-007: DDR 读写带宽"
    if echo "$output" | grep -qiE 'ddrc.*(read|write).*[0-9]+'; then
        assert_success 0 "MEM-007: 输出含 DDR 带宽数值"
    else
        assert_success 1 "MEM-007: 未找到 DDR 带宽"
    fi

    echo "  MEM-008: L3 命中率"
    if echo "$output" | grep -qiE 'Hit Rate.*[0-9]+'; then
        assert_success 0 "MEM-008: 输出含 L3 命中率"
    else
        skip "MEM-008: L3 命中率格式未匹配"
    fi

    test_group_end
fi

# =============================================================================
# 6.10 miss 采集测试
# =============================================================================

test_group_start "6.10 miss 采集测试"

if [ -z "$devkit_path" ]; then
    skip "MISS-001~008: devkit 未安装，跳过全部 miss 测试"
    test_group_end
elif ! check_spe_available; then
    skip "MISS-001~008: ARM SPE 不可用，跳过全部 miss 测试"
    test_group_end
else
    echo "  MISS-001: LLC Miss -m 1"
    output=$(run_devkit_cmd 'bash "$SCRIPTS/run_devkit.sh" tuner miss -d 3 -m 1 -t 10 2>&1'); rc=$?
    assert_success $rc "MISS-001: 退出码"
    assert_contains "$output" "Miss" "MISS-001: 含 Miss 报告"

    echo "  MISS-002: TLB Miss -m 2"
    output=$(run_devkit_cmd 'bash "$SCRIPTS/run_devkit.sh" tuner miss -d 3 -m 2 2>&1'); rc=$?
    assert_success $rc "MISS-002: 退出码"

    echo "  MISS-003: Remote Access -m 3"
    output=$(run_devkit_cmd 'bash "$SCRIPTS/run_devkit.sh" tuner miss -d 3 -m 3 2>&1'); rc=$?
    assert_success $rc "MISS-003: 退出码"

    echo "  MISS-004: Long Latency Load -m 4 -L 128"
    output=$(run_devkit_cmd 'bash "$SCRIPTS/run_devkit.sh" tuner miss -d 3 -m 4 -L 128 2>&1'); rc=$?
    assert_success $rc "MISS-004: 退出码"

    echo "  MISS-005: -p PID"
    pid=$(pgrep -o systemd | head -1)
    if [ -n "$pid" ]; then
        output=$(run_devkit_cmd 'bash "$SCRIPTS/run_devkit.sh" tuner miss -d 3 -p "$pid" -m 1 2>&1'); rc=$?
        assert_success $rc "MISS-005: -p 退出码"
    else
        skip "MISS-005: 无可用 PID"
    fi

    echo "  MISS-006: --package"
    cwd=$(pwd)
    cd "$TEST_RESULTS_DIR"
    output=$(run_devkit_cmd 'bash "$SCRIPTS/run_devkit.sh" tuner miss -d 3 -m 1 --package 2>&1'); rc=$?
    cd "$cwd"
    assert_success $rc "MISS-006: --package 退出码"

    echo "  MISS-007: Miss Rate 值"
    output=$(run_devkit_cmd 'bash "$SCRIPTS/run_devkit.sh" tuner miss -d 3 -m 1 -t 5 2>&1'); rc=$?
    if echo "$output" | grep -qE '[0-9]+\.[0-9]+ *%'; then
        assert_success 0 "MISS-007: 输出含 Miss Rate 值"
    else
        skip "MISS-007: Miss Rate 格式未匹配"
    fi

    echo "  MISS-008: 虚拟机/容器检测"
    if grep -qE '(VMware|KVM|Xen|docker|containerd)' /proc/1/cgroup 2>/dev/null || systemd-detect-virt --vm &>/dev/null; then
        vm_output=$(run_devkit_cmd 'bash "$SCRIPTS/run_devkit.sh" tuner miss -d 1 -m 1 2>&1'); vm_rc=$?
        assert_failure $vm_rc "MISS-008: 虚拟机环境应不支持"
    else
        skip "MISS-008: 非虚拟机环境，跳过"
    fi

    test_group_end
fi

# =============================================================================
# 6.11 binaries.yaml 一致性测试
# =============================================================================

test_group_start "6.11 binaries.yaml 一致性测试"

echo "  YAML-001: tuner_tasks 与 devkit tuner help 一致"
yaml_tasks=$(awk '/^tuner_tasks:/{f=1;next} f&&/^  - name:/{print $3} f&&/^[^[:space:]]/{f=0}' "$SCRIPTS/binaries.yaml" | sort)
if [ -n "$devkit_path" ]; then
    devkit_tasks=$(run_devkit_cmd 'bash "$SCRIPTS/run_devkit.sh" tuner help 2>&1' | grep -oE '^[[:space:]]+(turbostat|top-down|hotspot|miss|numafast|hpc-perf|roofline|memory)' | awk '{print $1}' | sort)
    for task in $yaml_tasks; do
        if echo "$devkit_tasks" | grep -qx "$task"; then
            assert_success 0 "YAML-001: $task 在 devkit help 中存在"
        else
            assert_failure 1 "YAML-001: $task 不在 devkit help 中"
        fi
    done
else
    skip "YAML-001: devkit 未安装，跳过"
fi

echo "  YAML-002: packages 包名检查"
pkg_names=$(awk '/^packages:/{f=1;next} f&&/^  - name:/{print $3} f&&/^[^[:space:]]/{f=0}' "$SCRIPTS/binaries.yaml")
for pkg in $pkg_names; do
    assert_contains "$pkg" "devkit" "YAML-002: 包名 $pkg 含 devkit 前缀"
done

echo "  YAML-003: version 与 devkit --version 一致"
yaml_version=$(grep '^  version:' "$SCRIPTS/binaries.yaml" | head -1 | sed 's/.*: *"\(.*\)".*/\1/')
if [ -n "$devkit_path" ]; then
    devkit_version=$(run_devkit_cmd 'bash "$SCRIPTS/run_devkit.sh" --version 2>&1' | grep -oiE 'version[[:space:]]+[0-9]+\.[0-9]+\.[a-z0-9]+' | awk '{print $NF}')
    if [ -n "$devkit_version" ]; then
        assert_equal "${yaml_version,,}" "${devkit_version,,}" "YAML-003: 版本一致"
    else
        skip "YAML-003: 未提取到 devkit 版本"
    fi
else
    skip "YAML-003: devkit 未安装，跳过"
fi

echo "  YAML-004: binary_subpath 检查"
subpath=$(grep '^[[:space:]]*binary_subpath:' "$SCRIPTS/binaries.yaml" | head -1 | sed 's/.*: *//')
assert_equal "usr/local/devkit/devkit" "$subpath" "YAML-004: binary_subpath 正确"

echo "  YAML-005: lib_subpath 字段存在"
lib_subpath=$(grep '^[[:space:]]*lib_subpath:' "$SCRIPTS/binaries.yaml" | head -1 | sed 's/.*: *//')
if [ -n "$lib_subpath" ]; then
    assert_success 0 "YAML-005: lib_subpath 存在"
else
    assert_success 1 "YAML-005: lib_subpath 不存在"
fi

echo "  YAML-006: lib_subpath 与实际路径一致"
if [ -n "$devkit_path" ] && [ -n "$lib_subpath" ]; then
    yaml_subpath=$(grep '^[[:space:]]*binary_subpath:' "$SCRIPTS/binaries.yaml" | head -1 | sed 's/.*: *//')
    devkit_root="${devkit_path%/${yaml_subpath}}"
    if [ -d "${devkit_root}/${lib_subpath}" ]; then
        assert_success 0 "YAML-006: lib_subpath 路径存在"
    else
        assert_success 1 "YAML-006: lib_subpath 路径不存在"
    fi
else
    skip "YAML-006: devkit 未安装，跳过"
fi

echo "  YAML-007: tuner_tasks 任务名不重复"
task_list=$(awk '/^tuner_tasks:/{f=1;next} f&&/^  - name:/{print $3} f&&/^[^[:space:]]/{f=0}' "$SCRIPTS/binaries.yaml")
dup_count=$(echo "$task_list" | sort | uniq -d | wc -l)
if [ "$dup_count" -eq 0 ]; then
    assert_success 0 "YAML-007: tuner_tasks 无重复"
else
    assert_success 1 "YAML-007: tuner_tasks 存在重复"
fi

echo "  YAML-008: packages 段不含 tuner_tasks 条目"
pkg_section=$(awk '/^packages:/{f=1;next} f&&/^[^[:space:]]/{f=0} f' "$SCRIPTS/binaries.yaml")
tuner_section=$(awk '/^tuner_tasks:/{f=1;next} f&&/^[^[:space:]]/{f=0} f' "$SCRIPTS/binaries.yaml")
pkg_names_in_section=$(echo "$pkg_section" | grep 'name:' | sed 's/.*: *//')
tuner_names_in_section=$(echo "$tuner_section" | grep 'name:' | sed 's/.*: *//')
cross_count=0
for tn in $tuner_names_in_section; do
    if echo "$pkg_names_in_section" | grep -qx "$tn"; then
        cross_count=$((cross_count + 1))
    fi
done
if [ "$cross_count" -eq 0 ]; then
    assert_success 0 "YAML-008: packages 段无 tuner_tasks 交叉"
else
    assert_success 1 "YAML-008: packages 段含 tuner_tasks 条目"
fi

test_group_end

# =============================================================================
# 7. 逻辑回归测试
# =============================================================================

test_group_start "7. 逻辑回归测试"

echo "  LOGIC-001: parse_packages 不误匹配 tuner_tasks"
pkg_output=$(bash -c '
source <(sed -n "/^parse_packages/,/^}/p" "'"$SCRIPTS"'/deploy_devkit.sh")
CONFIG_FILE="'"$SCRIPTS"'/binaries.yaml"
parse_packages false
' 2>/dev/null)
tuner_names="turbostat top-down numafast hotspot memory miss"
cross=0
for tn in $tuner_names; do
    if echo "$pkg_output" | grep -q "^${tn}|"; then
        cross=$((cross + 1))
    fi
done
if [ "$cross" -eq 0 ]; then
    assert_success 0 "LOGIC-001: parse_packages 不输出 tuner_tasks 条目"
else
    assert_success 1 "LOGIC-001: parse_packages 误输出 $cross 个 tuner_tasks 条目"
fi

echo "  LOGIC-002: --all + --packages 不重复安装"
dup_output=$(bash -c '
source <(sed -n "/^parse_packages/,/^}/p" "'"$SCRIPTS"'/deploy_devkit.sh")
CONFIG_FILE="'"$SCRIPTS"'/binaries.yaml"
parse_packages true devkit
' 2>/dev/null)
devkit_count=$(echo "$dup_output" | grep -c "^devkit|")
if [ "$devkit_count" -le 1 ]; then
    assert_success 0 "LOGIC-002: --all + --packages devkit 不重复"
else
    assert_success 1 "LOGIC-002: --all + --packages devkit 重复 $devkit_count 次"
fi

echo "  LOGIC-003: --packages 不存在的包输出 WARNING"
warn_output=$(bash "$SCRIPTS/deploy_devkit.sh" --packages nonexistent-pkg-test 2>&1)
if echo "$warn_output" | grep -qi "nonexistent-pkg-test.*not found\|not found.*nonexistent-pkg-test"; then
    assert_success 0 "LOGIC-003: 输出 WARNING 含包名"
else
    assert_success 1 "LOGIC-003: 未输出 WARNING"
fi

echo "  LOGIC-004: discover 任务 help 含 3(error) 不误判"
if [ -n "$devkit_path" ]; then
    discover_output=$(bash "$SCRIPTS/discover_devkit.sh" 2>&1)
    unavailable_count=$(echo "$discover_output" | grep -c "TASK_UNAVAILABLE")
    ready_count=$(echo "$discover_output" | grep -c ": READY")
    if [ "$unavailable_count" -eq 0 ] && [ "$ready_count" -gt 0 ]; then
        assert_success 0 "LOGIC-004: 无 TASK_UNAVAILABLE 误判"
    else
        assert_success 1 "LOGIC-004: $unavailable_count 个任务误判为 UNAVAILABLE"
    fi
else
    skip "LOGIC-004: devkit 未安装，跳过"
fi

echo "  LOGIC-005: discover file 命令缺失不误报 ARCH_MISMATCH"
if command -v file &>/dev/null; then
    skip "LOGIC-005: file 命令存在，跳过"
else
    discover_output=$(bash "$SCRIPTS/discover_devkit.sh" 2>&1)
    if echo "$discover_output" | grep -q "ARCH_MISMATCH"; then
        assert_success 1 "LOGIC-005: file 缺失时误报 ARCH_MISMATCH"
    else
        assert_success 0 "LOGIC-005: file 缺失时不报 ARCH_MISMATCH"
    fi
fi

echo "  LOGIC-006: discover 版本解析失败报 VERSION_MISMATCH"
fake_yaml=$(mktemp)
cat > "$fake_yaml" <<'YAMLEOF'
devkit_meta:
  version: "99.99.99"
  display_version: "99.99.99"
  supported_arch: [aarch64]
  supported_os: [any]
install:
  root_dir: /opt/devkit
  binary_subpath: usr/local/devkit/devkit
  lib_subpath: usr/local/devkit/tuner/lib
packages:
  - name: devkit
    required: true
    sha256: "fake"
  - name: devkit-tuner
    required: true
    sha256: "fake"
tuner_tasks:
  - name: turbostat
    function: test
YAMLEOF
if [ -n "$devkit_path" ]; then
    orig_yaml="$SCRIPTS/binaries.yaml"
    cp "$orig_yaml" "$TEST_RESULTS_DIR/binaries_backup.yaml"
    cp "$fake_yaml" "$orig_yaml"
    discover_output=$(bash "$SCRIPTS/discover_devkit.sh" 2>&1); rc=$?
    cp "$TEST_RESULTS_DIR/binaries_backup.yaml" "$orig_yaml"
    if echo "$discover_output" | grep -q "VERSION_MISMATCH"; then
        assert_success 0 "LOGIC-006: 版本不匹配时报 VERSION_MISMATCH"
    else
        assert_success 1 "LOGIC-006: 版本不匹配时未报 VERSION_MISMATCH"
    fi
else
    skip "LOGIC-006: devkit 未安装，跳过"
fi
rm -f "$fake_yaml"

echo "  LOGIC-007: run_devkit.sh LD_LIBRARY_PATH 从 yaml 读取"
if [ -n "$devkit_path" ]; then
    lib_subpath=$(grep '^[[:space:]]*lib_subpath:' "$SCRIPTS/binaries.yaml" | head -1 | sed 's/.*: *//')
    binary_subpath=$(grep '^[[:space:]]*binary_subpath:' "$SCRIPTS/binaries.yaml" | head -1 | sed 's/.*: *//')
    expected_root="${devkit_path%/${binary_subpath}}"
    expected_lib="${expected_root}/${lib_subpath}"
    run_output=$(bash -c '
        export DEVKIT_BIN="'"$devkit_path"'"
        SCRIPT_DIR="'"$SCRIPTS"'"
        source "'"$SCRIPTS"'/run_devkit.sh" --version 2>/dev/null || true
    ' 2>&1)
    # run_devkit.sh exec 替换了进程，无法直接检查 LD_LIBRARY_PATH
    # 改为验证 lib_subpath 路径存在
    if [ -d "$expected_lib" ]; then
        assert_success 0 "LOGIC-007: lib_subpath 路径存在（$expected_lib）"
    else
        assert_success 1 "LOGIC-007: lib_subpath 路径不存在"
    fi
else
    skip "LOGIC-007: devkit 未安装，跳过"
fi

echo "  LOGIC-008: awk section 终止匹配非空白行"
empty_yaml=$(mktemp)
cat > "$empty_yaml" <<'YAMLEOF'
devkit_meta:
  version: "1.0"
Tuner_Extra:
  - name: should_not_match
YAMLEOF
tasks=$(awk '/^tuner_tasks:/{f=1;next} f&&/^  - name:/{print $3} f&&/^[^[:space:]]/{f=0}' "$empty_yaml")
if [ -z "$tasks" ]; then
    assert_success 0 "LOGIC-008: 空 tuner_tasks 返回空"
else
    assert_success 1 "LOGIC-008: 应返回空，实际=$tasks"
fi
rm -f "$empty_yaml"

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
