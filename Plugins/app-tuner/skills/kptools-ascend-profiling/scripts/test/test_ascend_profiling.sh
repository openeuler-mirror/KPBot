#!/bin/bash
# =============================================================================
# ascend-profiling 测试套件（无 NPU 环境：组件缺失分支 + HTTP stub + mock 产物）
# 对应设计文档：ascend-profiling-skill-design.md §7（AP-001 ~ AP-009）
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

# ── mock vLLM HTTP stub：记录 POST 并可配置 404 ───────────────
start_mock_vllm() {
    local port="$1" mode="$2"   # mode: ok | noapi
    cat > "$TEST_RESULTS_DIR/mock_vllm.py" <<PYEOF
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

MODE = "$mode"
LOG = "$TEST_RESULTS_DIR/mock_vllm_requests.log"

class H(BaseHTTPRequestHandler):
    def do_POST(self):
        with open(LOG, "a") as f:
            f.write(self.command + " " + self.path + "\n")
        if MODE == "noapi":
            self.send_response(404); self.end_headers(); self.wfile.write(b"not found")
            return
        if self.path in ("/start_profile", "/stop_profile"):
            self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
        else:
            self.send_response(404); self.end_headers()
    def do_GET(self):
        with open(LOG, "a") as f:
            f.write(self.command + " " + self.path + "\n")
        if self.path == "/health":
            self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
        else:
            self.send_response(404); self.end_headers()
    def log_message(self, *a):
        pass

HTTPServer(("127.0.0.1", $port), H).serve_forever()
PYEOF
    python3 "$TEST_RESULTS_DIR/mock_vllm.py" &>/dev/null &
    MOCK_PID=$!
    sleep 1
    : > "$TEST_RESULTS_DIR/mock_vllm_requests.log"
}

stop_mock_vllm() {
    [ -n "${MOCK_PID:-}" ] && kill "$MOCK_PID" 2>/dev/null
    wait "$MOCK_PID" 2>/dev/null
    MOCK_PID=""
}

# =============================================================================
# 1. 语法验证
# =============================================================================
test_group_start "1. 语法验证"

for script in discover.sh collect.sh; do
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
# 2. discover（AP-001 / AP-002）
# =============================================================================
test_group_start "2. discover"

echo "  AP-001: discover（本机无 NPU/torch_npu/msserviceprofiler）"
output=$(bash "$SCRIPTS/discover.sh" 2>&1); rc=$?
assert_failure $rc "AP-001: 组件缺失时退出码非 0（2=环境不可用）"
assert_contains "$output" "NO_ASCEND_HARDWARE" "AP-001: 报告无 NPU 设备"
assert_contains "$output" "torch_npu" "AP-001: 报告 torch_npu 状态"
assert_contains "$output" "msserviceprofiler" "AP-001: 报告 msserviceprofiler 状态"

echo "  AP-002: discover（mock torch_npu/msserviceprofiler）"
mkdir -p "$TEST_RESULTS_DIR/fake_mod" "$TEST_RESULTS_DIR/shim_bin"
printf '' > "$TEST_RESULTS_DIR/fake_mod/torch_npu.py"
printf '' > "$TEST_RESULTS_DIR/fake_mod/torch.py"
for t in curl sed grep python3 ls; do ln -sf "$(command -v $t)" "$TEST_RESULTS_DIR/shim_bin/$t"; done
printf '#!/bin/bash\necho "msserviceprofiler mock"\n' > "$TEST_RESULTS_DIR/shim_bin/msserviceprofiler"
chmod +x "$TEST_RESULTS_DIR/shim_bin/msserviceprofiler"
output=$(PYTHONPATH="$TEST_RESULTS_DIR/fake_mod" PATH="$TEST_RESULTS_DIR/shim_bin" "$BASH_BIN" "$SCRIPTS/discover.sh" 2>&1); rc=$?
assert_contains "$output" "torch_npu: AVAILABLE" "AP-002: mock 后 torch_npu 可用"
assert_contains "$output" "msserviceprofiler: AVAILABLE" "AP-002: mock 后 msserviceprofiler 可用"

# =============================================================================
# 3. 方案一 pt-*（AP-003 / AP-008 / AP-004）
# =============================================================================
test_group_start "3. 方案一（Ascend PyTorch Profiler）"

PORT=18766
start_mock_vllm "$PORT" ok
echo "  AP-003: pt-start / pt-stop 对 mock vLLM"
output=$(bash "$SCRIPTS/collect.sh" pt-start -u "http://127.0.0.1:$PORT" -o "$TEST_RESULTS_DIR/pt_out" 2>&1); rc=$?
assert_success $rc "AP-003: pt-start 退出码 0"
output=$(bash "$SCRIPTS/collect.sh" pt-stop -u "http://127.0.0.1:$PORT" 2>&1); rc=$?
assert_success $rc "AP-003: pt-stop 退出码 0"
grep -q 'POST /start_profile' "$TEST_RESULTS_DIR/mock_vllm_requests.log"
assert_success $? "AP-003: stub 收到 start_profile 请求"
grep -q 'POST /stop_profile' "$TEST_RESULTS_DIR/mock_vllm_requests.log"
assert_success $? "AP-003: stub 收到 stop_profile 请求"
stop_mock_vllm

start_mock_vllm "$PORT" noapi
echo "  AP-008: 服务未配置 profiler（404）"
output=$(bash "$SCRIPTS/collect.sh" pt-start -u "http://127.0.0.1:$PORT" 2>&1); rc=$?
assert_failure $rc "AP-008: 404 退出码非 0"
assert_contains "$output" "profiler-config" "AP-008: 报因含 --profiler-config 注入指引"
stop_mock_vllm

echo "  AP-004: pt-analyse（无 torch_npu → 依赖缺失）"
mkdir -p "$TEST_RESULTS_DIR/fake_ascend_pt"
output=$(bash "$SCRIPTS/collect.sh" pt-analyse -d "$TEST_RESULTS_DIR/fake_ascend_pt" 2>&1); rc=$?
assert_failure $rc "AP-004: 无 torch_npu 退出码非 0"
assert_contains "$output" "torch_npu" "AP-004: 报因含 torch_npu 缺失"

echo "  AP-010: pt-analyse 注入防护（恶意路径不产生副作用）"
PWN_FILE="$TEST_RESULTS_DIR/pwned_marker"
rm -f "$PWN_FILE"
output=$(bash "$SCRIPTS/collect.sh" pt-analyse -d "x'; import os; os.system('touch $PWN_FILE'); analyse('x" 2>&1); rc=$?
assert_failure $rc "AP-010: 恶意路径退出码非 0"
[ ! -f "$PWN_FILE" ]; assert_success $? "AP-010: 未执行注入的 os.system（无 pwned 标记文件）"

# =============================================================================
# 4. 方案二 msp-*（AP-005 / AP-006 / AP-007）
# =============================================================================
test_group_start "4. 方案二（MS Service Profiler）"

echo "  AP-005: msp-enable / msp-disable 幂等"
CFG="$TEST_RESULTS_DIR/msprof_config.json"
cat > "$CFG" <<'EOF'
{"enable": 0, "metrics": ["Request", "KVCache"]}
EOF
output=$(bash "$SCRIPTS/collect.sh" msp-enable -c "$CFG" 2>&1); rc=$?
assert_success $rc "AP-005: msp-enable 退出码 0"
grep -q '"enable": 1' "$CFG"
assert_success $? "AP-005: enable 0 → 1"
output=$(bash "$SCRIPTS/collect.sh" msp-enable -c "$CFG" 2>&1); rc=$?
assert_success $rc "AP-005: 重复 enable 幂等"
grep -q '"enable": 1' "$CFG"
assert_success $? "AP-005: 仍为 1"
output=$(bash "$SCRIPTS/collect.sh" msp-disable -c "$CFG" 2>&1); rc=$?
assert_success $rc "AP-005: msp-disable 退出码 0"
grep -q '"enable": 0' "$CFG"
assert_success $? "AP-005: enable 1 → 0"

echo "  AP-006: msp-enable 缺配置文件"
output=$(bash "$SCRIPTS/collect.sh" msp-enable -c /nonexistent.json 2>&1); rc=$?
assert_failure $rc "AP-006: 退出码非 0（3=参数错误）"

echo "  AP-007: msp-parse（无 msserviceprofiler → 依赖缺失）"
mkdir -p "$TEST_RESULTS_DIR/fake_msp_out"
output=$(bash "$SCRIPTS/collect.sh" msp-parse -d "$TEST_RESULTS_DIR/fake_msp_out" 2>&1); rc=$?
assert_failure $rc "AP-007: 无工具退出码非 0"
assert_contains "$output" "msserviceprofiler" "AP-007: 报因含工具缺失"

echo "  msp-domains"
output=$(bash "$SCRIPTS/collect.sh" msp-domains 2>/dev/null); rc=$?
assert_success $rc "msp-domains 退出码 0"
assert_contains "$output" "Communication" "msp-domains 含 HCCL 通信 domain"

# =============================================================================
# 5. SKILL.md / evals（AP-009）
# =============================================================================
test_group_start "5. SKILL.md / evals"

assert_contains "$(head -3 "$SKILL_DIR/SKILL.md" 2>/dev/null)" "name: kptools-ascend-profiling" "AP-009: frontmatter name 正确"
if [ -f "$SKILL_DIR/SKILL.md" ]; then
    assert_success 0 "AP-009: SKILL.md 存在"
else
    assert_failure 1 "AP-009: SKILL.md 存在"
fi
for f in eval_queries.json evals.json; do
    if [ -f "$SKILL_DIR/evals/$f" ]; then
        python3 -c "import json; json.load(open('$SKILL_DIR/evals/$f'))" 2>/dev/null
        assert_success $? "AP-009: evals/$f JSON 合法"
        grep -q '"skill_name": "kptools-ascend-profiling"' "$SKILL_DIR/evals/$f"
        assert_success $? "AP-009: evals/$f skill_name 正确"
    else
        assert_failure 1 "AP-009: evals/$f 存在"
    fi
done
grep -q 'routes to kptools-ascend-smi' "$SKILL_DIR/evals/eval_queries.json" 2>/dev/null
assert_success $? "AP-009: 含跨 SKILL 负例路由（ascend-smi）"

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
