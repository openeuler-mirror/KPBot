#!/usr/bin/env bash
set -uo pipefail

cd /workspace
mkdir -p build results

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

python3 - <<'PY'
import json, os
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

# with 臂必须产出 rust 源语言字段+瓶颈归因；without 臂允许缺（这正是 A/B 差异，由 run_ab 汇总）
skill_mode = os.environ.get("SKILL_MODE", "with")
if skill_mode == "with":
    if not source_language_ok:
        ok = False
        reasons.append("input.source_language != 'rust'")
    if not bottleneck_ok:
        ok = False
        reasons.append("bottleneck field empty/missing")

json.dump({
    "success": ok,
    "reason": "; ".join(reasons) if reasons else "ok",
    "skill_mode": skill_mode,
    "source_language_rust": source_language_ok,
    "bottleneck_present": bottleneck_ok,
}, open("results/result.json", "w"), indent=2)
PY

if [ -f results/result.json ]; then
    grep -q '"success": true' results/result.json && exit 0
fi
exit 1
