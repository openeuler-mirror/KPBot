#!/usr/bin/env bash
# ak_compile_optimize.sh 纯逻辑函数单测（source-only 模式）
# 运行: bash test_ak_compile_helpers.sh
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_SCRIPT="${SCRIPT_DIR}/../skills/kpbot-app-tuner/subskills/compiler-optimization/scripts/ak_compile_optimize.sh"

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  PASS: $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }
assert_eq() { # desc expected actual
  if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1 (expected=[$2] actual=[$3])"; fi
}

scratch=$(mktemp -d) || exit 1
trap 'rm -rf "$scratch"' EXIT
export AK_TEST_SOURCE_ONLY=1
# shellcheck source=/dev/null
source "${SKILL_SCRIPT}" || { echo "source 失败"; exit 1; }

# ── latest_whl：按组件前缀过滤，而非裸 head -1 ──
echo "== latest_whl =="
run_in_target() { echo "[mock] $*"; }
latest_whl /src/dist "torch-*.whl" | grep -q 'torch-\*\.whl' && ok "模式进入命令" || bad "模式未进入命令"
latest_whl /src/dist "torch_npu-*.whl" | grep -q 'torch_npu-\*\.whl' && ok "torch_npu 模式" || bad "torch_npu 模式丢失"

# ── json_escape ──
echo "== json_escape =="
assert_eq "引号转义" 'a\"b' "$(json_escape 'a"b')"
assert_eq "反斜杠转义" 'a\\b' "$(json_escape 'a\b')"
out=$(json_escape "$(printf 'a\n\nb')")
assert_eq "空行折叠" 'a b' "${out}"
out=$(json_escape "$(printf 'line1\nline2\nline3')")
assert_eq "多行合并单行" 'line1 line2 line3' "${out}"

# ── known_failure_diagnosis：签名命中/未命中 ──
echo "== known_failure_diagnosis =="
LOG_FILE="$scratch/diag.log"
echo "fatal error: Python.h: No such file or directory" > "${LOG_FILE}"
echo "c++: error: linker command failed" >> "${LOG_FILE}"
diag=$(known_failure_diagnosis)
echo "${diag}" | grep -q 'python3-devel' && ok "Python.h 命中" || bad "Python.h 未命中"
echo "${diag}" | grep -q 'OOM' && bad "OOM 误命中" || ok "无 OOM 误报"
echo "[12345.678] Killed" >> "${LOG_FILE}"
diag=$(known_failure_diagnosis)
echo "${diag}" | grep -q 'OOM' && ok "Killed 命中" || bad "Killed 未命中"
echo "section .rodata load segment overlap" >> "${LOG_FILE}"
diag=$(known_failure_diagnosis)
echo "${diag}" | grep -q 'no-rosegment' && ok "rosegment 命中" || bad "rosegment 未命中"
LOG_FILE="/nonexistent"; diag=$(known_failure_diagnosis)
assert_eq "日志缺失返回空" "" "${diag}"
LOG_FILE=""

# ── resolve_chain_python：dry-run 占位 ──
echo "== resolve_chain_python =="
DRY_RUN=true; CHAIN_DIR="$scratch/chain-u"; CHAIN_PYTHON_EXE=""
resolve_chain_python
assert_eq "dry-run 占位路径" "$scratch/chain-u/python/bin/python3" "${CHAIN_PYTHON_EXE}"
resolve_chain_python
ok "二次调用幂等（已解析不再探测）"

# ── chain_state_write：dry-run 不落盘 ──
echo "== chain_state_write =="
DRY_RUN=true; CHAIN_DIR="$scratch/chain-state-u"
chain_state_write "python_build_done" "done"
[[ ! -f "${CHAIN_DIR}/state.json" ]] && ok "dry-run 未写 state.json" || bad "dry-run 写入了 state.json"
DRY_RUN=false
chain_state_write "python_build_done" "done"
[[ -f "${CHAIN_DIR}/state.json" ]] && ok "真实模式写入 state.json" || bad "真实模式未写 state.json"
v=$(chain_state read "python_build_done")
assert_eq "state 读回一致" "done" "${v}"
rm -rf "$scratch/chain-state-u"

# ── 多补丁钩子 run_patch_scripts：冒号分隔按序执行 ──
echo "== run_patch_scripts =="
DRY_RUN=true; SOURCE_DIR=/tmp/x; COMPONENT=torch_npu; CURRENT_VERSION="2.4.0-6.0.0"
out=$(AK_PATCH_SCRIPTS="/a.sh:/b.sh" AK_PATCH_SCRIPT="/c.sh" run_patch_scripts)
echo "${out}" | grep -q '/a.sh:/b.sh' && ok "dry-run 列出补丁链" || bad "补丁链未列出"
DRY_RUN=false
p1="$scratch/p1.sh"; p2="$scratch/p2.sh"
printf '#!/usr/bin/env bash\necho "patch1:$*"\n' > "${p1}"
printf '#!/usr/bin/env bash\necho "patch2:$*"\n' > "${p2}"
out=$(AK_PATCH_SCRIPTS="${p1}:${p2}" AK_PATCH_SCRIPT="" run_patch_scripts)
echo "${out}" | grep -q 'patch1:/tmp/x torch_npu 2.4.0-6.0.0' && ok "补丁1参数正确" || bad "补丁1参数错误: ${out}"
echo "${out}" | grep -q 'patch2:' && ok "补丁2按序执行" || bad "补丁2未执行"
rm -f "${p1}" "${p2}"

echo ""
echo "===== 结果: PASS=${PASS} FAIL=${FAIL} ====="
[[ ${FAIL} -eq 0 ]]
