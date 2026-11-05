#!/usr/bin/env bash
# agent 用例驱动：探测目标容器 → 跑 agent → 解析 trace → verify → 并入 skill 证据
set -uo pipefail

cd /workspace
mkdir -p opt results

SKILL_MODE="${SKILL_MODE:-with}"          # with | without

if [ "${SKILL_MODE}" = "with" ]; then
    mkdir -p .opencode/skills
    cp -r /staging/skill .opencode/skills/vllm-ascend-profiling
fi

# ---- 目标容器探测（A/B 两模式完全一致，保证同任务）----
# TARGET_CONTAINER 显式优先；否则列出运行容器、按进程判定唯一 vllm 容器
resolve_target() {
    if [ -n "${TARGET_CONTAINER:-}" ]; then
        TARGET="${TARGET_CONTAINER}"
        TARGET_HOW="explicit"
        return
    fi
    local hits=""
    for c in $(docker ps --format '{{.Names}}' 2>/dev/null); do
        if docker exec "$c" bash -c "ps -ef 2>/dev/null | grep vllm | grep -v grep | grep -q python" >/dev/null 2>&1; then
            hits+=" $c"
        fi
    done
    local n
    n=$(echo "$hits" | wc -w)
    if [ "$n" -eq 1 ]; then
        TARGET="$(echo "$hits" | xargs)"
        TARGET_HOW="auto"
    else
        return 1
    fi
}

if ! resolve_target; then
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"target container not resolvable (0 or >1 vllm containers; set TARGET_CONTAINER)","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    docker ps --format '{{.Names}}' > results/targets_seen.txt 2>&1 || true
    exit 2
fi
echo "[entrypoint] SKILL_MODE=${SKILL_MODE} target=${TARGET} (${TARGET_HOW})"
TARGET_CONTAINER="${TARGET}" python3 -c 'import json,os;json.dump({"container":os.environ["TARGET_CONTAINER"]},open("results/target.json","w"),indent=2)'
export TARGET_CONTAINER

# ---- 提示词（替换目标容器占位）----
PROMPT_FILE="/workspace/prompts/${SKILL_MODE}-skill.md"
if [ ! -f "${PROMPT_FILE}" ]; then
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"unknown SKILL_MODE","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    exit 2
fi
sed "s|{{VLLM_CONTAINER}}|${TARGET}|g" "${PROMPT_FILE}" > /workspace/prompt.final.md

# ---- agent 权限收敛（意见10）----
# --auto 会自动放行所有工具调用，且 agent 可读到挂载的宿主 auth.json。
# 这里在 /workspace/opencode.json 落显式 deny 规则：--auto 只放行"未被显式
# deny"的动作，deny 规则始终强制生效。除纯 deny 外不做任何 allow 覆盖，
# default 语义保持原样。残余风险见 case.yaml"意见10 治理说明"（bash 嵌套等）。
if [ "${OPENCODE_PERMISSIONS_GUARD:-1}" = "1" ]; then
cat > /workspace/opencode.json <<'JSON'
{
  "$schema": "https://opencode.ai/config.json",
  "permission": {
    "webfetch": "deny",
    "websearch": "deny",
    "read": { "*": "allow", "*auth.json": "deny", "*.env": "ask", "*.env.*": "ask" },
    "bash": { "*": "allow", "curl *": "deny", "wget *": "deny", "ssh *": "deny", "nc *": "deny", "ncat *": "deny", "rsync *": "deny", "scp *": "deny" }
  }
}
JSON
    echo "[entrypoint] opencode permission guard: ON (webfetch/websearch denied, auth.json reads denied)"
fi

# ---- 运行 agent：--format json 落 trace ----
if ! opencode run --auto --format json -m "${MODEL:-deepseek/deepseek-v4-flash}" "$(cat /workspace/prompt.final.md)" \
        > results/trace.jsonl 2> results/opencode.log; then
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"opencode run failed","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    exit 1
fi

if [ ! -f opt/diagnosis.json ]; then
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"opt/diagnosis.json not generated","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    exit 1
fi

# ---- 解析 trace：工具调用序列 + 被调用的 skill ----
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

# ---- 把 skill 调用证据并入 result.json ----
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
