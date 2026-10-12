#!/usr/bin/env bash
set -uo pipefail

cd /workspace
mkdir -p results

SKILL_MODE="${SKILL_MODE:-with}"          # with | without

# ---- 凭证重组：OPENCODE_AUTH_JSON 整文件 base64 → 隔离 HOME 下 auth.json ----
umask 077
AUTH_TMP="$(mktemp /tmp/opencode_auth.XXXXXX)"
printf '%s' "${OPENCODE_AUTH_JSON}" | base64 -d > "${AUTH_TMP}"
export HOME=/tmp/opencode-home
mkdir -p "$HOME/.local/share/opencode"
cp "${AUTH_TMP}" "$HOME/.local/share/opencode/auth.json"

# ---- 项目级权限配置：deny bash/webfetch/websearch（显式 deny 在 --auto 下依然生效）----
printf '%s\n' '{"permission":{"bash":"deny","webfetch":"deny","websearch":"deny","edit":"allow"}}' > /workspace/opencode.json

# ---- 运行结束后清理凭证 ----
cleanup() {
    rm -rf "${AUTH_TMP}" /tmp/opencode-home
}
trap cleanup EXIT

if [ "${SKILL_MODE}" = "with" ]; then
    mkdir -p .opencode/skills
    cp -r /staging/skill .opencode/skills/special-case-optimization
fi

PROMPT_FILE="prompts/${SKILL_MODE}-skill.md"
if [ ! -f "${PROMPT_FILE}" ]; then
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"unknown SKILL_MODE","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    exit 2
fi

echo "[entrypoint] SKILL_MODE=${SKILL_MODE} model=${MODEL:-deepseek/deepseek-v4-flash}"

# 运行 agent：--format json 落 trace，stderr 落日志
if ! opencode run --auto --format json -m "${MODEL:-deepseek/deepseek-v4-flash}" "$(cat "${PROMPT_FILE}")" \
        > results/trace.jsonl 2> results/opencode.log; then
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"opencode run failed","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    exit 1
fi

if [ ! -f opt/kernel_opt.rs ]; then
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"opt/kernel_opt.rs not generated","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    exit 1
fi

# 解析 trace：工具调用序列 + 被调用的 skill
python3 - <<'PY'
import json
tools = []
skills = []
calls = []
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

# 把 skill 调用证据并入 result.json
if [ -f results/result.json ] && [ -f results/trace_summary.json ]; then
    python3 - <<'PY'
import json
r = json.load(open("results/result.json"))
t = json.load(open("results/trace_summary.json"))
r["skills_invoked"] = t.get("skills_invoked", [])
r["tools_used"] = t.get("tools_used", [])
json.dump(r, open("results/result.json", "w"), ensure_ascii=False, indent=2)
PY
fi
