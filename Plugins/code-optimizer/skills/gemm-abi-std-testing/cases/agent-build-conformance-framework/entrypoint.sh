#!/usr/bin/env bash
# entrypoint.sh —— 容器内驱动 opencode 执行任务，解析 trace，调用 verify.sh
# 与 docs/case-spec.md 的运行时契约一致（容器内 /workspace）。
#
# 凭证与权限加固（对齐仓内 operation-fusion 等用例的既定做法）：
#   - 凭证整文件 base64 经 OPENCODE_AUTH_JSON env 注入，不落盘、不挂载
#   - umask 077 + 隔离 HOME 解码，trap cleanup EXIT 清理
#   - 项目级 opencode.json 显式 deny bash/webfetch/websearch（--auto 下依然生效）
set -uo pipefail

cd /workspace
mkdir -p results

# ---- 凭证重组：OPENCODE_AUTH_JSON 整文件 base64 → 隔离 HOME 下 auth.json ----
umask 077
AUTH_TMP="$(mktemp /tmp/opencode_auth.XXXXXX)"
printf '%s' "${OPENCODE_AUTH_JSON:-}" | base64 -d > "${AUTH_TMP}"
export HOME=/tmp/opencode-home
mkdir -p "${HOME}/.local/share/opencode"
cp "${AUTH_TMP}" "${HOME}/.local/share/opencode/auth.json"

# ---- 项目级权限：deny bash/webfetch/websearch（标准文本已本地提供，无需外网）----
printf '%s\n' '{"permission":{"bash":"deny","webfetch":"deny","websearch":"deny","edit":"allow"}}' > /workspace/opencode.json

# ---- 运行结束后清理凭证 ----
cleanup() {
    rm -rf "${AUTH_TMP}" /tmp/opencode-home
}
trap cleanup EXIT

SKILL_MODE="${SKILL_MODE:-with}"          # with | without

# with：把 skill 接入运行时 skills 目录；without：不接入任何 skill
if [ "${SKILL_MODE}" = "with" ]; then
    mkdir -p .opencode/skills
    cp -r /staging/skill .opencode/skills/gemm-abi-std-testing
fi

PROMPT_FILE="prompts/${SKILL_MODE}-skill.md"
if [ ! -f "${PROMPT_FILE}" ]; then
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"unknown SKILL_MODE","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    exit 2
fi

echo "[entrypoint] SKILL_MODE=${SKILL_MODE} model=${MODEL:-deepseek/deepseek-v4-flash}"

# 运行 agent：--format json 落 trace（机器可读事件流），stderr 落日志
if ! opencode run --auto --format json -m "${MODEL:-deepseek/deepseek-v4-flash}" "$(cat "${PROMPT_FILE}")" \
        > results/trace.jsonl 2> results/opencode.log; then
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"opencode run failed","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    exit 1
fi

if [ ! -d opt/conformance ] || [ -z "$(find opt/conformance -type f -print -quit 2>/dev/null)" ]; then
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"opt/conformance not generated","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    exit 1
fi

# 解析 trace：工具调用序列 + 被调用的 skill
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
