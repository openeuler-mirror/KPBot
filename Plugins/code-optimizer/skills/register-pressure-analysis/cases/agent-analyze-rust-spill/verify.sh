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

# ---- 分析产物判据：opt/pressure.json ----
if [ ! -f opt/pressure.json ]; then
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"opt/pressure.json not generated","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    exit 1
fi

python3 - <<'PY'
import json, os
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

# with 臂必须产出 rust 语言字段+source_kind+合法压力等级；without 臂允许缺（这正是 A/B 差异，由 run_ab 汇总）
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

json.dump({
    "success": ok,
    "reason": "; ".join(reasons) if reasons else "ok",
    "skill_mode": skill_mode,
    "language_rust": language_ok,
    "source_kind_rust_intrinsics": source_kind_ok,
    "pressure_level_valid": pressure_level_ok,
}, open("results/result.json", "w"), indent=2)
PY

if [ -f results/result.json ]; then
    grep -q '"success": true' results/result.json && exit 0
fi
exit 1
