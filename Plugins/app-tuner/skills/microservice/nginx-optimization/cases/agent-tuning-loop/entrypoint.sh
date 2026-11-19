#!/usr/bin/env bash
# 驱动 opencode → 解析 trace → verify → 合并 skill 调用证据进 result.json。
set -uo pipefail

cd /workspace
mkdir -p results

SKILL_MODE="${SKILL_MODE:-with}" # with | without
export MOCK_LOG="${MOCK_LOG:-/workspace/results/mock_invocations.jsonl}"

# P1 教训：只接入被测内容本身（/staging/skill 仅含 SKILL.md 与 references/），
# 评分答案（evals/）从未进入镜像，with/without 对照不受污染。
if [ "${SKILL_MODE}" = "with" ]; then
    mkdir -p .opencode/skills/nginx-optimization
    cp -r /staging/skill/. .opencode/skills/nginx-optimization/
fi

PROMPT_FILE="prompts/${SKILL_MODE}-skill.md"
if [ ! -f "${PROMPT_FILE}" ]; then
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"unknown SKILL_MODE","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    exit 2
fi

echo "[entrypoint] SKILL_MODE=${SKILL_MODE} model=${MODEL:-deepseek/deepseek-v4-flash}"

if ! opencode run --auto --format json -m "${MODEL:-deepseek/deepseek-v4-flash}" "$(cat "${PROMPT_FILE}")" \
        > results/trace.jsonl 2> results/opencode.log; then
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"opencode run failed","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    exit 1
fi

# 解析 trace：工具使用与 skill 调用证据。
python3 - <<'PY'
import json
tools, skills, calls = [], [], []
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
    if p.get("type") != "tool":
        continue
    tool = p.get("tool", "?")
    st = (p.get("state") or {}).get("status")
    inp = (p.get("state") or {}).get("input") or {}
    if tool not in tools:
        tools.append(tool)
    if tool == "skill":
        name = inp.get("name")
        if name and name not in skills:
            skills.append(name)
    calls.append({"tool": tool, "status": st})
json.dump({"tools_used": tools, "skills_invoked": skills, "tool_calls": calls},
          open("results/trace_summary.json", "w"), ensure_ascii=False, indent=2)
PY

./verify.sh

# P2 教训：skills_invoked 证据合并进 result.json，供 run_all 的硬断言使用
# （with 臂未真正调用 skill 时不得计为通过）。
python3 - <<'PY'
import json, os
try:
    ts = json.load(open("results/trace_summary.json"))
except Exception:
    ts = {"skills_invoked": []}
path = "results/result.json"
data = {}
if os.path.exists(path) and os.path.getsize(path) > 0:
    try:
        data = json.load(open(path))
    except Exception:
        data = {}
data["skills_invoked"] = ts.get("skills_invoked", [])
data["skill_mode"] = os.environ.get("SKILL_MODE", "")
json.dump(data, open(path, "w"), ensure_ascii=False, indent=2)
PY
