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

# ---- 分析产物判据：opt/analysis.json ----
if [ ! -f opt/analysis.json ]; then
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"opt/analysis.json not generated","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    exit 1
fi

# ---- 复算 ground truth（V4 判据与自报导耦的核心段）----
# verify.sh 由 entrypoint 在 agent 退出后调用：reference 产物在 agent 运行期不存在
# （天然防抄），此刻以 optimizer 用户用镜像内工具链重跑技能脚本复算参考结果。
# 复算失败或产物非法 JSON 一律判负（fail-closed），绝不静默退化为只查自报字段。
MCA_SCRIPT=""
if [ -f /staging/skill/scripts/mca_analyze.py ]; then
    MCA_SCRIPT=/staging/skill/scripts/mca_analyze.py
elif [ -f "${SELF_DIR}/../../scripts/mca_analyze.py" ]; then
    # 非容器部署（T6 脚本独立可用性）：回退仓库内技能脚本副本（绝对路径）
    MCA_SCRIPT="$(cd "${SELF_DIR}/../../scripts" && pwd)/mca_analyze.py"
else
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"reference recalc failed: mca_analyze.py not found (staging or repo copy)","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    exit 1
fi
MCA_SCRIPT_DIR="$(dirname "${MCA_SCRIPT}")"
REF_CMD="python3 ${MCA_SCRIPT} --uarch hip08 --source src/kernel.rs --function calc::dot --source-language rust"
REF_RC=0
${REF_CMD} > results/reference.json 2> results/reference.stderr.log || REF_RC=$?
REF_SHA256="$(sha256sum results/reference.json 2>/dev/null | awk '{print $1}')"
python3 - "${REF_CMD}" "${REF_RC}" "${REF_SHA256}" <<'PY'
import json, sys
json.dump({
    "purpose": "post-agent ground-truth recalc (verify runs after agent exit; artifact not visible to agent)",
    "commands": [{"command": sys.argv[1], "exit_code": int(sys.argv[2])}],
    "reference_sha256": sys.argv[3] or None,
}, open("results/provenance.json", "w"), indent=2)
PY
if [ "${REF_RC}" -ne 0 ] || [ ! -s results/reference.json ]; then
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"reference recalc failed (non-zero exit or empty output; see results/reference.stderr.log)","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    exit 1
fi

# ---- 交叉核对：agent 自报 opt/analysis.json vs 复算 reference.json ----
# 硬断言（with 臂 gating）：uarch/mcpu/function 逐字段对 reference 一致 + 指令数健全；
# 结论类记布尔（match_bottleneck）不作硬断言，作 A/B 读数由 run_ab 汇总呈现。
# P1-b：捕获交叉核对进程退出码，非零直接判负（见 heredoc 后检查）
CROSSCHECK_RC=0
MCA_SCRIPT_DIR="${MCA_SCRIPT_DIR}" REFERENCE_SHA256="${REF_SHA256}" python3 - <<'PY' || CROSSCHECK_RC=$?
import json, os, sys

# 段级尾部对齐比较复用技能脚本自身实现（D4：与 mca_analyze 的符号匹配语义一致）
sys.path.insert(0, os.environ["MCA_SCRIPT_DIR"])
import mca_analyze as mca

# ---- P1-b catch-all：交叉核对主体任何未预期异常一律 fail-closed 判负 ----
# （防 agent 植入极端构造令解释器带原始异常退出、result.json 未更新，
#   从而落入尾部 grep 只信预置文件的旁路；异常落盘 success:false+原因后
#   sys.exit(1)，bash 段对非零退出码直接 exit 1。）
try:
    ok = True
    reasons = []
    try:
        with open("opt/analysis.json", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except Exception as e:
        data = None
        ok = False
        reasons.append("analysis.json is not valid JSON: %s" % e)

    source_language_ok = False
    bottleneck_ok = False
    inner = None
    if isinstance(data, dict):
        # 脚本契约：外层键 llvm_mca_analysis_result（或 agent 嵌套保存的等价形态）
        inner = data.get("llvm_mca_analysis_result") if "llvm_mca_analysis_result" in data else data
        if isinstance(inner, dict):
            inp = inner.get("input") or {}
            source_language_ok = (inp.get("source_language") == "rust")
            bn = inner.get("bottleneck")
            bottleneck_ok = bool(bn)  # 非空即有效（dict/str/list 均接受）
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
    ref_inner = reference.get("llvm_mca_analysis_result") if isinstance(reference, dict) else None
    if not (isinstance(ref_inner, dict) and ref_inner.get("success")):
        ok = False
        reasons.append("reference recalc failed: reference.json missing or unsuccessful")

    # function 字段记录的是传入形态（如 calc::dot / _ZN4calc3dot），按段级尾部对齐归一化后比较
    def fn_segments(name):
        if not name:
            return []
        name = str(name)
        if name.startswith("_ZN"):
            return mca.demangle_legacy_segments(name)
        return [s for s in name.split("::") if s]

    def function_matches(a, b):
        sa, sb = fn_segments(a), fn_segments(b)
        if not sa or not sb:
            return bool(a) and bool(b) and str(a) == str(b)
        n = min(len(sa), len(sb))
        return n > 0 and sa[-n:] == sb[-n:]

    uarch_match = False
    mcpu_match = False
    function_match = False
    instructions_sane = False
    match_bottleneck = False
    if isinstance(inner, dict) and isinstance(ref_inner, dict):
        uarch_match = (inner.get("uarch") == ref_inner.get("uarch"))
        mcpu_match = (inner.get("mcpu") == ref_inner.get("mcpu"))
        function_match = function_matches(
            (inner.get("input") or {}).get("function"),
            (ref_inner.get("input") or {}).get("function"))
        instructions = (inner.get("summary") or {}).get("instructions")
        instructions_sane = (isinstance(instructions, (int, float))
                             and not isinstance(instructions, bool) and instructions > 0)
        agent_type = (inner.get("bottleneck") or {}).get("primary_type")
        ref_type = (ref_inner.get("bottleneck") or {}).get("primary_type")
        match_bottleneck = bool(ref_type) and agent_type == ref_type

    # with 臂必须产出 rust 源语言字段+瓶颈归因+硬断言全部通过；without 臂只记录不 gating
    # （这正是 A/B 差异，由 run_ab 汇总）
    skill_mode = os.environ.get("SKILL_MODE", "with")
    if skill_mode == "with":
        if not source_language_ok:
            ok = False
            reasons.append("input.source_language != 'rust'")
        if not bottleneck_ok:
            ok = False
            reasons.append("bottleneck field empty/missing")
        if not uarch_match:
            ok = False
            reasons.append("uarch != reference uarch")
        if not mcpu_match:
            ok = False
            reasons.append("mcpu != reference mcpu")
        if not function_match:
            ok = False
            reasons.append("function != reference function (segment-tail aligned)")
        if not instructions_sane:
            ok = False
            reasons.append("summary.instructions not a positive number")

    json.dump({
        "success": ok,
        "reason": "; ".join(reasons) if reasons else "ok",
        "skill_mode": skill_mode,
        "source_language_rust": source_language_ok,
        "bottleneck_present": bottleneck_ok,
        "uarch_match": uarch_match,
        "mcpu_match": mcpu_match,
        "function_match": function_match,
        "instructions_sane": instructions_sane,
        "match_bottleneck": match_bottleneck,
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
