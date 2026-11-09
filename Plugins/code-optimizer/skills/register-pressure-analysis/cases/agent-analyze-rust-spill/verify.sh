#!/usr/bin/env bash
set -uo pipefail

# 脚本自身目录（cd /workspace 前解析；非容器部署时用于回退定位仓库内技能脚本副本）
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"

cd /workspace
mkdir -p build results

# ---- P1-a 判定文件先重置：agent 运行期预置的 result.json 即刻失效 ----
# （results 历史上 agent 可写；不重置则交叉核对异常退出时尾部 grep 会采信预置内容）
rm -f results/result.json

# ---- R4 完整性自校验（fail-closed）----
if [ -f .pristine.sha256 ]; then
    if ! sha256sum -c .pristine.sha256 > results/integrity.log 2>&1; then
        SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"integrity check failed","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
        exit 1
    fi
else
    echo "警告: .pristine.sha256 缺失（非容器部署），跳过完整性校验" >&2
fi

SKILL_MODE="${SKILL_MODE:-with}"

# ---- R12 kernel 语义正确性验证（防御性编译开关，编译即验证）----
mkdir -p build/base
cp bench/main.rs build/base/main.rs
cp src/kernel.rs build/base/kernel.rs
rustc -O -C overflow-checks=on -C debug-assertions=on --edition 2021 -o build/bench_check build/base/main.rs 2>build/base_compile.log || {
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"bench compile failed","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    exit 1
}
./build/bench_check 8192 > results/bench.out || {
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"bench correctness check failed","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    exit 1
}
grep -q "CORRECT=1" results/bench.out || {
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"bench CORRECT!=1","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    exit 1
}

# ---- 分析产物判据：opt/pressure.json ----
if [ ! -f opt/pressure.json ]; then
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"opt/pressure.json not generated","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    exit 1
fi

# ---- 复算 ground truth（V4 判据与自报导耦的核心段）----
# verify.sh 由 entrypoint 在 agent 退出后调用：reference 产物在 agent 运行期不存在
# （天然防抄），此刻以 optimizer 用户用镜像内工具链重跑技能脚本复算参考结果。
# 复算失败或产物非法 JSON 一律判负（fail-closed），绝不静默退化为只查自报字段。
SPILL_SCRIPT=""
if [ -f /staging/skill/scripts/analyze_assembly_spill.py ]; then
    SPILL_SCRIPT=/staging/skill/scripts/analyze_assembly_spill.py
elif [ -f "${SELF_DIR}/../../scripts/analyze_assembly_spill.py" ]; then
    # 非容器部署（T6 脚本独立可用性）：回退仓库内技能脚本副本（绝对路径）
    SPILL_SCRIPT="$(cd "${SELF_DIR}/../../scripts" && pwd)/analyze_assembly_spill.py"
else
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"reference recalc failed: analyze_assembly_spill.py not found (staging or repo copy)","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    exit 1
fi
SPILL_SCRIPT_DIR="$(dirname "${SPILL_SCRIPT}")"
# rm -f 防预置：agent 运行期可在 /tmp 预写同名文件/符号链接，rustc -o 重建全新文件
rm -f /tmp/pr2_ref_kernel.s
REF_RUSTC_CMD="rustc -O --edition 2021 --crate-type=lib --target aarch64-unknown-linux-gnu -C target-feature=+neon --emit asm -o /tmp/pr2_ref_kernel.s src/kernel.rs"
REF_RUSTC_RC=0
${REF_RUSTC_CMD} > /dev/null 2> results/reference.rustc.log || REF_RUSTC_RC=$?
REF_CMD=""
REF_RC=0
if [ "${REF_RUSTC_RC}" -eq 0 ]; then
    REF_CMD="python3 ${SPILL_SCRIPT} --asm /tmp/pr2_ref_kernel.s --function microkernel::fma_4x4 --language rust --source-kind rust_intrinsics"
    ${REF_CMD} > results/reference.json 2> results/reference.stderr.log || REF_RC=$?
fi
REF_SHA256="$(sha256sum results/reference.json 2>/dev/null | awk '{print $1}')"
python3 - "${REF_RUSTC_CMD}" "${REF_RUSTC_RC}" "${REF_CMD}" "${REF_RC}" "${REF_SHA256}" <<'PY'
import json, sys
commands = [{"command": sys.argv[1], "exit_code": int(sys.argv[2])}]
if sys.argv[3]:
    commands.append({"command": sys.argv[3], "exit_code": int(sys.argv[4])})
json.dump({
    "purpose": "post-agent ground-truth recalc (verify runs after agent exit; artifact not visible to agent)",
    "commands": commands,
    "reference_sha256": sys.argv[5] or None,
}, open("results/provenance.json", "w"), indent=2)
PY
if [ "${REF_RUSTC_RC}" -ne 0 ] || [ "${REF_RC}" -ne 0 ] || [ ! -s results/reference.json ]; then
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"reference recalc failed (rustc or analyze non-zero exit / empty output; see results/reference.rustc.log and results/reference.stderr.log)","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    exit 1
fi

# ---- 交叉核对：agent 自报 opt/pressure.json vs 复算 reference.json ----
# 硬断言（with 臂 gating）：function 与 reference 一致（段级尾部对齐）+ 计数非负健全；
# 结论类记布尔（match_level）不作硬断言，作 A/B 读数由 run_ab 汇总呈现。
# P1-b：捕获交叉核对进程退出码，非零直接判负（见 heredoc 后检查）
CROSSCHECK_RC=0
SPILL_SCRIPT_DIR="${SPILL_SCRIPT_DIR}" REFERENCE_SHA256="${REF_SHA256}" python3 - <<'PY' || CROSSCHECK_RC=$?
import json, os, sys

# 段级尾部对齐比较复用技能脚本自身实现（D4：与 analyze_assembly_spill 的符号匹配语义一致）
sys.path.insert(0, os.environ["SPILL_SCRIPT_DIR"])
import analyze_assembly_spill as spill

# ---- P1-b catch-all：交叉核对主体任何未预期异常一律 fail-closed 判负 ----
# （防 agent 植入极端构造令解释器带原始异常退出、result.json 未更新，
#   从而落入尾部 grep 只信预置文件的旁路；异常落盘 success:false+原因后
#   sys.exit(1)，bash 段对非零退出码直接 exit 1。）
try:
    ok = True
    reasons = []
    try:
        with open("opt/pressure.json", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except Exception as e:
        data = None
        ok = False
        reasons.append("pressure.json is not valid JSON: %s" % e)

    language_ok = False
    source_kind_ok = False
    pressure_level_ok = False
    inner = None
    VALID_LEVELS = ["none", "low", "medium", "high", "severe"]
    if isinstance(data, dict):
        # 脚本契约：外层键 register_pressure_result（或 agent 嵌套保存的等价形态）
        inner = data.get("register_pressure_result") if "register_pressure_result" in data else data
        if isinstance(inner, dict):
            language_ok = (inner.get("language") == "rust")
            source_kind_ok = (inner.get("source_kind") == "rust_intrinsics")
            pressure_level_ok = (inner.get("pressure_level") in VALID_LEVELS)
            if not inner.get("success"):
                ok = False
                reasons.append("analysis success != true")

    # ---- reference（harness 复算 ground truth；复算失败已在 bash 段 fail-closed，此处双保险）----
    reference = None
    try:
        with open("results/reference.json", encoding="utf-8", errors="replace") as f:
            reference = json.load(f)
    except Exception as e:
        ok = False
        reasons.append("reference recalc failed: reference.json is not valid JSON: %s" % e)
    ref_inner = reference.get("register_pressure_result") if isinstance(reference, dict) else None
    if not (isinstance(ref_inner, dict) and ref_inner.get("success")):
        ok = False
        reasons.append("reference recalc failed: reference.json missing or unsuccessful")

    # function 字段记录的是传入形态（如 microkernel::fma_4x4 / _ZN6kernel11microkernel7fma_4x4），
    # 按段级尾部对齐归一化后比较
    def fn_segments(name):
        if not name:
            return []
        name = str(name)
        if name.startswith("_ZN"):
            return spill.demangle_legacy_segments(name)
        return [s for s in name.split("::") if s]

    def function_matches(a, b):
        sa, sb = fn_segments(a), fn_segments(b)
        if not sa or not sb:
            return bool(a) and bool(b) and str(a) == str(b)
        n = min(len(sa), len(sb))
        return n > 0 and sa[-n:] == sb[-n:]

    def count_sane(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0

    function_match = False
    counts_sane = False
    match_level = False
    if isinstance(inner, dict) and isinstance(ref_inner, dict):
        function_match = function_matches(inner.get("function"), ref_inner.get("function"))
        counts_sane = all(count_sane(inner.get(k))
                          for k in ("spill_store_count", "spill_reload_count", "fma_count"))
        match_level = (inner.get("pressure_level") == ref_inner.get("pressure_level")
                       and ref_inner.get("pressure_level") in VALID_LEVELS)

    # with 臂必须产出 rust 语言字段+source_kind+合法压力等级+硬断言通过；without 臂只记录不 gating
    # （这正是 A/B 差异，由 run_ab 汇总）
    skill_mode = os.environ.get("SKILL_MODE", "with")
    if skill_mode == "with":
        if not language_ok:
            ok = False
            reasons.append("language != 'rust'")
        if not source_kind_ok:
            ok = False
            reasons.append("source_kind != 'rust_intrinsics'")
        if not pressure_level_ok:
            ok = False
            reasons.append("pressure_level not in valid enum")
        if not function_match:
            ok = False
            reasons.append("function != reference function (segment-tail aligned)")
        if not counts_sane:
            ok = False
            reasons.append("spill/reload/fma counts not non-negative numbers")

    json.dump({
        "success": ok,
        "reason": "; ".join(reasons) if reasons else "ok",
        "skill_mode": skill_mode,
        "language_rust": language_ok,
        "source_kind_rust_intrinsics": source_kind_ok,
        "pressure_level_valid": pressure_level_ok,
        "function_match": function_match,
        "counts_sane": counts_sane,
        "match_level": match_level,
        "reference_sha256": os.environ.get("REFERENCE_SHA256") or None,
    }, open("results/result.json", "w"), indent=2)
except Exception as e:
    json.dump({
        "success": False,
        "reason": "crosscheck exception: %s: %s" % (type(e).__name__, e),
        "skill_mode": os.environ.get("SKILL_MODE", "with"),
    }, open("results/result.json", "w"), indent=2)
    sys.exit(1)

PY

# P1-b：交叉核对进程非零退出（含 catch-all 落盘自身失败）直接判负，
# 不落入尾部 grep（result.json 未必存在或未必是本脚本写入）
if [ "${CROSSCHECK_RC}" -ne 0 ]; then
    exit 1
fi

if [ -f results/result.json ]; then
    grep -q '"success": true' results/result.json && exit 0
fi
exit 1
