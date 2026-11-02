#!/bin/bash
# =============================================================================
# bios-collector 测试套件
# 覆盖语法验证、discover、--dmi、--redfish（mock BMC）、OS 推断模式、
# --all、redfish_attrs 归一化、SKILL.md/evals 格式
# 对应设计文档：bios-collector-skill-design.md §7（BC-001 ~ BC-010）
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
BASH_BIN=$(command -v bash)

red()    { echo -e "\033[31m$*\033[0m"; }
green()  { echo -e "\033[32m$*\033[0m"; }
yellow() { echo -e "\033[33m$*\033[0m"; }

assert_equal() {
    local expected="$1" actual="$2" message="${3:-AssertEqual}"
    if [ "$expected" = "$actual" ]; then
        echo "  [PASS] $message"; TEST_PASSED=$((TEST_PASSED + 1))
    else
        echo "  [FAIL] $message"; echo "         期望: $expected"; echo "         实际: $actual"; TEST_FAILED=$((TEST_FAILED + 1))
    fi
}

assert_success() {
    local exit_code="$1" message="${2:-Command should succeed}"
    if [ "$exit_code" -eq 0 ]; then
        echo "  [PASS] $message"; TEST_PASSED=$((TEST_PASSED + 1))
    else
        echo "  [FAIL] $message (exit code: $exit_code)"; TEST_FAILED=$((TEST_FAILED + 1))
    fi
}

assert_failure() {
    local exit_code="$1" message="${2:-Command should fail}"
    if [ "$exit_code" -ne 0 ]; then
        echo "  [PASS] $message"; TEST_PASSED=$((TEST_PASSED + 1))
    else
        echo "  [FAIL] $message (exit code: $exit_code)"; TEST_FAILED=$((TEST_FAILED + 1))
    fi
}

assert_contains() {
    local haystack="$1" needle="$2" message="${3:-AssertContains}"
    if [[ "$haystack" == *"$needle"* ]]; then
        echo "  [PASS] $message"; TEST_PASSED=$((TEST_PASSED + 1))
    else
        echo "  [FAIL] $message"; echo "         未找到: $needle"; TEST_FAILED=$((TEST_FAILED + 1))
    fi
}

skip() { echo "  [SKIP] $1"; TEST_SKIPPED=$((TEST_SKIPPED + 1)); }

test_group_start() { echo -e "\n\033[34m===== 测试组: $1 =====\033[0m"; }

# ── mock BMC HTTP stub ────────────────────────────────────────
start_mock_bmc() {
    local port="$1" mode="$2"   # mode: ok | badpass
    cat > "$TEST_RESULTS_DIR/mock_bmc.py" <<PYEOF
import base64, json
from http.server import BaseHTTPRequestHandler, HTTPServer

MODE = "$mode"
GOOD_AUTH = "Basic " + base64.b64encode(b"user:goodpass").decode()

ATTRS = {
    "Attributes": {
        "WorkloadProfile": "HighPerformance",
        "ProcHyperthreading": "Enabled",
        "ProcTurbo": "Enabled",
        "ProcessorCstate": "Enabled",
        "NodeInterleaving": "Disabled",
        "PcieAspmSupport": "Auto",
        "HWPrefetcher": "Enabled",
        "MemorySpeed": "4800 MT/s"
    }
}

class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode() if isinstance(obj, dict) else obj.encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        if MODE == "badpass" and self.path != "/redfish/v1":
            auth = self.headers.get("Authorization", "")
            if auth != GOOD_AUTH:
                self._send(401, {"error": "unauthorized"})
                return
        if self.path == "/redfish/v1":
            self._send(200, {"RedfishVersion": "1.15.0"})
        elif self.path == "/redfish/v1/Systems":
            self._send(200, {"Members": [{"@odata.id": "/redfish/v1/Systems/1"}]})
        elif self.path == "/redfish/v1/Systems/1":
            self._send(200, {"Id": "1", "PowerState": "On"})
        elif self.path == "/redfish/v1/Systems/1/Bios":
            self._send(200, ATTRS)
        elif self.path == "/redfish/v1/Systems/1/Bios/Settings":
            self._send(200, ATTRS)
        else:
            self._send(404, {"error": "not found"})
    def log_message(self, *a):
        pass

HTTPServer(("127.0.0.1", $port), H).serve_forever()
PYEOF
    python3 "$TEST_RESULTS_DIR/mock_bmc.py" &>/dev/null &
    MOCK_PID=$!
    sleep 1
}

stop_mock_bmc() {
    [ -n "${MOCK_PID:-}" ] && kill "$MOCK_PID" 2>/dev/null
    wait "$MOCK_PID" 2>/dev/null
    MOCK_PID=""
}

# =============================================================================
# 1. 语法验证
# =============================================================================
test_group_start "1. 语法验证"

for script in discover.sh collect.sh redfish_attrs.sh; do
    [ -f "$SCRIPTS/$script" ]; assert_success $? "$script 存在"
    if [ -f "$SCRIPTS/$script" ]; then
        bash -n "$SCRIPTS/$script" 2>"$TEST_RESULTS_DIR/syntax_$script.err"
        assert_success $? "$script 语法检查"
    fi
done

if [ ! -f "$SCRIPTS/collect.sh" ]; then
    echo "collect.sh 不存在，跳过后续功能测试"
    echo "============================================"
    echo "  测试汇总"
    echo "============================================"
    echo "  PASS: $TEST_PASSED"
    echo "  FAIL: $TEST_FAILED"
    echo "  SKIP: $TEST_SKIPPED"
    echo "  总计: $((TEST_PASSED + TEST_FAILED + TEST_SKIPPED))"
    echo "============================================"
    exit 1
fi

# =============================================================================
# 2. discover / --help（BC-001 / BC-002）
# =============================================================================
test_group_start "2. discover / --help"

echo "  BC-001: discover.sh"
output=$(bash "$SCRIPTS/discover.sh" 2>&1); rc=$?
assert_success $rc "BC-001: discover 退出码 0"
for tool in dmidecode cpupower lspci numactl curl; do
    assert_contains "$output" "$tool" "BC-001: 输出含 $tool 状态"
done
assert_contains "$output" "BMC" "BC-001: 声明 BMC 可达性未检测"

echo "  BC-002: --help"
output=$(bash "$SCRIPTS/collect.sh" --help 2>&1); rc=$?
assert_success $rc "BC-002: --help 退出码 0"
for token in "--dmi" "--redfish" "--smt" "--cstate" "--turbo" "--numa" "--ddr" "--aspm" "--prefetcher" "--all" "--bmc-host"; do
    assert_contains "$output" "$token" "BC-002: 帮助含 $token"
done

# =============================================================================
# 3. --dmi / OS 推断模式（BC-003 / BC-007 / BC-008）
# =============================================================================
test_group_start "3. --dmi / OS 推断"

echo "  BC-003: --dmi"
output=$(bash "$SCRIPTS/collect.sh" --dmi 2>/dev/null); rc=$?
assert_success $rc "BC-003: --dmi 退出码 0"
assert_contains "$output" "dmidecode -t bios" "BC-003: 含 dmidecode bios 段"
assert_contains "$output" "sysfs DMI" "BC-003: 含 sysfs DMI 段"

echo "  BC-007: --smt / --turbo（无 Redfish，OS 推断）"
output=$(bash "$SCRIPTS/collect.sh" --smt 2>/dev/null); rc=$?
assert_success $rc "BC-007: --smt 退出码 0"
assert_contains "$output" "smt=" "BC-007: 输出 smt 状态行"
output=$(bash "$SCRIPTS/collect.sh" --turbo 2>/dev/null); rc=$?
assert_success $rc "BC-007: --turbo 退出码 0"
assert_contains "$output" "turbo=" "BC-007: 输出 turbo 推断行"
assert_contains "$output" "source=os_inferred" "BC-007: 标注 os_inferred 来源"

echo "  BC-008: --cstate（cpupower 在/不在 PATH）"
output=$(bash "$SCRIPTS/collect.sh" --cstate 2>"$TEST_RESULTS_DIR/bc008.stderr"); rc=$?
assert_success $rc "BC-008: --cstate 退出码 0"
assert_contains "$output" "cpuidle states" "BC-008: 输出 cpuidle states 段"
mkdir -p "$TEST_RESULTS_DIR/shim_bin"
for t in id basename cat; do
    ln -sf "$(command -v $t)" "$TEST_RESULTS_DIR/shim_bin/$t"
done
output=$(PATH="$TEST_RESULTS_DIR/shim_bin" "$BASH_BIN" "$SCRIPTS/collect.sh" --cstate 2>&1); rc=$?
assert_success $rc "BC-008: 无 cpupower 降级退出码 0"
assert_contains "$output" "sysfs only" "BC-008: 降级标注 sysfs only"

# =============================================================================
# 4. --redfish（BC-004 / BC-005，mock BMC）
# =============================================================================
test_group_start "4. --redfish（mock BMC）"

PORT=18765
start_mock_bmc "$PORT" ok

echo "  BC-004a: 正常采集（凭据正确）"
output=$(bash "$SCRIPTS/collect.sh" --redfish --bmc-host "http://127.0.0.1:$PORT" -u user -p goodpass 2>"$TEST_RESULTS_DIR/bc004a.stderr"); rc=$?
assert_success $rc "BC-004a: 采集退出码 0"
assert_contains "$output" "Bios" "BC-004a: 输出含 Bios 段"
assert_contains "$output" "ProcHyperthreading" "BC-004a: 输出含 BIOS 属性原文"
if [[ "$output" != *"goodpass"* ]]; then
    assert_success 0 "BC-004a: 凭据不回显"
else
    assert_failure 0 "BC-004a: 凭据泄漏到输出"
fi
stop_mock_bmc

start_mock_bmc "$PORT" badpass
echo "  BC-004b: 凭据错误 → 401 分类"
output=$(bash "$SCRIPTS/collect.sh" --redfish --bmc-host "http://127.0.0.1:$PORT" -u user -p badpass 2>&1); rc=$?
assert_failure $rc "BC-004b: 401 退出码非 0"
assert_contains "$output" "凭据" "BC-004b: 报因含凭据错误"
stop_mock_bmc

echo "  BC-004c: BMC 不可达分类"
output=$(bash "$SCRIPTS/collect.sh" --redfish --bmc-host "http://127.0.0.1:1" -u user -p goodpass 2>&1); rc=$?
assert_failure $rc "BC-004c: 不可达退出码非 0"
assert_contains "$output" "不可达" "BC-004c: 报因含不可达"

echo "  BC-005: 无凭据 SKIP"
output=$(bash "$SCRIPTS/collect.sh" --redfish 2>&1); rc=$?
assert_success $rc "BC-005: 无凭据不阻断（退出码 0）"
assert_contains "$output" "SKIP" "BC-005: 输出 SKIP 声明"

echo "  BC-011: --prefetcher（mock BMC，Redfish 真实采集）"
start_mock_bmc "$PORT" ok
output=$(bash "$SCRIPTS/collect.sh" --prefetcher --bmc-host "http://127.0.0.1:$PORT" -u user -p goodpass 2>"$TEST_RESULTS_DIR/bc009.stderr"); rc=$?
assert_success $rc "BC-011: 退出码 0"
assert_contains "$output" "hardware_prefetcher=Enabled" "BC-011: 输出 Redfish 实值（HWPrefetcher）"
assert_contains "$output" "source=redfish" "BC-011: 标注 redfish 来源"
stop_mock_bmc

echo "  BC-012: --prefetcher 无凭据 → SKIP unavailable"
output=$(bash "$SCRIPTS/collect.sh" --prefetcher 2>&1); rc=$?
assert_success $rc "BC-012: 退出码 0"
assert_contains "$output" "hardware_prefetcher=unavailable" "BC-012: 无凭据输出 unavailable"

# =============================================================================
# 5. redfish_attrs 归一化（BC-006）
# =============================================================================
test_group_start "5. redfish_attrs 归一化"

echo "  BC-006: 三级匹配"
cat > "$TEST_RESULTS_DIR/bc006_bios.json" <<'EOF'
{"Attributes": {
    "ProcHyperthreading": "Enabled",
    "TurboMode": "Enabled",
    "SomeVendorPrefetchThing": "Enabled",
    "AspmA": "Auto", "AspmB": "Disabled",
    "UnknownAttr": "1"
}}
EOF
output=$(bash "$SCRIPTS/redfish_attrs.sh" -f "$TEST_RESULTS_DIR/bc006_bios.json" 2>/dev/null); rc=$?
assert_success $rc "BC-006: 归一化退出码 0"
assert_contains "$output" '"smt"' "BC-006: 精确匹配 smt（ProcHyperthreading）"
assert_contains "$output" '"match_method": "exact"' "BC-006: 精确匹配方法标注"
assert_contains "$output" '"turbo"' "BC-006: 精确匹配 turbo（TurboMode）"
assert_contains "$output" '"ambiguous"' "BC-006: 关键词歧义标注（aspm 两候选）"
python3 -c "
import json,sys
d=json.load(open('$TEST_RESULTS_DIR/../bc006_bios.json')) if False else json.loads('''$output''')
smt=d.get('smt',{})
assert smt.get('value')=='Enabled' and smt.get('confidence')=='high', smt
aspm=d.get('pcie_aspm',{})
assert aspm.get('match_method')=='ambiguous' and len(aspm.get('ambiguous_candidates',[]))==2, aspm
assert 'hardware_prefetcher' in d and d['hardware_prefetcher'].get('match_method')=='keyword'
" 2>"$TEST_RESULTS_DIR/bc006.assert.err"
assert_success $? "BC-006: 精确/歧义/关键词三级结果正确"

# =============================================================================
# 6. --all（BC-009）
# =============================================================================
test_group_start "6. --all"

echo "  BC-009: --all 无 BMC 凭据"
output=$(bash "$SCRIPTS/collect.sh" --all 2>"$TEST_RESULTS_DIR/bc009.stderr"); rc=$?
assert_success $rc "BC-009: --all 总退出码 0"
for sec in "dmidecode -t bios" "cpuidle states" "turbo=" "numa_interleaving" "dmidecode -t memory" "PCIe ASPM"; do
    assert_contains "$output" "$sec" "BC-009: 输出含 $sec"
done
assert_contains "$(cat "$TEST_RESULTS_DIR/bc009.stderr")" "SKIP" "BC-009: stderr 含 SKIP"

# =============================================================================
# 7. SKILL.md / evals（BC-010）
# =============================================================================
test_group_start "7. SKILL.md / evals"

assert_contains "$(head -3 "$SKILL_DIR/SKILL.md" 2>/dev/null)" "name: kptools-bios-collector" "BC-010: frontmatter name 正确"
if [ -f "$SKILL_DIR/SKILL.md" ]; then
    assert_success 0 "BC-010: SKILL.md 存在"
else
    assert_failure 1 "BC-010: SKILL.md 存在"
fi
for f in eval_queries.json evals.json; do
    if [ -f "$SKILL_DIR/evals/$f" ]; then
        python3 -c "import json; json.load(open('$SKILL_DIR/evals/$f'))" 2>/dev/null
        assert_success $? "BC-010: evals/$f JSON 合法"
        grep -q '"skill_name": "kptools-bios-collector"' "$SKILL_DIR/evals/$f"
        assert_success $? "BC-010: evals/$f skill_name 正确"
    else
        assert_failure 1 "BC-010: evals/$f 存在"
    fi
done
grep -q 'routes to kptools-os-collector' "$SKILL_DIR/evals/eval_queries.json" 2>/dev/null
assert_success $? "BC-010: 含跨 SKILL 负例路由（os-collector）"

# =============================================================================
# 汇总
# =============================================================================
echo ""
echo "============================================"
echo "  测试汇总"
echo "============================================"
if [ $TEST_FAILED -eq 0 ]; then
    green "  PASS: $TEST_PASSED"
else
    red "  PASS: $TEST_PASSED"
fi
red "  FAIL: $TEST_FAILED"
yellow "  SKIP: $TEST_SKIPPED"
echo "  总计: $((TEST_PASSED + TEST_FAILED + TEST_SKIPPED))"
echo "============================================"
exit $TEST_FAILED
