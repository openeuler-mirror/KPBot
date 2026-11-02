#!/usr/bin/env bash
set -uo pipefail
cd /workspace
mkdir -p results
SKILL_MODE="${SKILL_MODE:-with}"
SKILL_NAME="kptools-os-collector"

if [ "${SKILL_MODE}" = "with" ]; then
    mkdir -p .opencode/skills
    cp -r /staging/skill ".opencode/skills/${SKILL_NAME}"
fi

PROMPT_FILE="prompts/${SKILL_MODE}-skill.md"
write_fail() { SKILL_MODE="${SKILL_MODE}" python3 -c "import json,os;json.dump({'success':False,'reason':'$1','skill_mode':os.environ['SKILL_MODE']},open('results/result.json','w'),indent=2)"; exit 1; }
[ -f "${PROMPT_FILE}" ] || { write_fail "missing prompt"; }

if ! opencode run --auto --format json -m "${MODEL:-opencode/glm-5.3-flash-free}" "$(cat "${PROMPT_FILE}")"         > results/trace.jsonl 2> results/opencode.log; then
    write_fail "opencode run failed"
fi

SKILL_NAME="${SKILL_NAME}" SKILL_MODE="${SKILL_MODE}" python3 - <<'PY'
import json, os
tools, skills = [], []
for line in open("results/trace.jsonl", encoding="utf-8", errors="replace"):
    line = line.strip()
    if not line:
        continue
    try:
        e = json.loads(line)
    except Exception:
        continue
    if e.get("type") != "tool_use":
        continue
    p = e.get("part") or {}
    t = p.get("tool") or e.get("tool")
    if t:
        tools.append(t)
    if t == "skill":
        inp = p.get("input") or (p.get("state") or {}).get("input") or {}
        if inp.get("name"):
            skills.append(inp["name"])
invoked = os.environ["SKILL_NAME"] in skills
out = {
    "success": True,
    "skill_mode": os.environ["SKILL_MODE"],
    "trigger": invoked,
    "skills_invoked": sorted(set(skills)),
    "tools_used": sorted(set(tools)),
}
json.dump(out, open("results/result.json", "w"), indent=2)
# case-spec §4.2/§6：trace 解析摘要单独落盘
json.dump({"skills_invoked": out["skills_invoked"], "tools_used": out["tools_used"],
           "tool_calls": len(tools)}, open("results/trace_summary.json", "w"), indent=2)
PY
exit 0
