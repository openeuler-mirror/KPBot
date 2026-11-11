#!/usr/bin/env bash
set -uo pipefail

cd /workspace

# ---- P1-d 身份检查：本入口须以 root 运行（agent 段内部降权 optimizer，verify 段须 root 落盘判定文件）----
[ "$(id -u)" = "0" ] || { echo "错误: entrypoint 须以 root 运行（镜像 USER root；agent 段内部 setpriv 降权 optimizer）" >&2; exit 2; }

mkdir -p results opt
# ---- P1-a 第二重置：入口层也清掉 agent 可能预置的判定文件（与 verify.sh 内重置双保险）----
rm -f results/result.json

SKILL_MODE="${SKILL_MODE:-with}"          # with | without

# ---- R1 凭证重组：OPENCODE_AUTH_JSON 整文件 base64 → 隔离 HOME 下 auth.json ----
# P1-d：隔离 HOME 收敛为变量 AGENT_HOME（仅 agent 段经 env 注入 opencode 进程，
# root 段不再改 HOME）；umask 077 只护本段，auth 落盘 600；
# 段末 umask 022 复位——root 后续写文件 644，防 docker cp 出容器后宿主不可读。
umask 077
AUTH_TMP="$(mktemp /tmp/opencode_auth.XXXXXX)"
printf '%s' "${OPENCODE_AUTH_JSON}" | base64 -d > "${AUTH_TMP}"
AGENT_HOME=/tmp/opencode-home
mkdir -p "${AGENT_HOME}/.local/share/opencode"
cp "${AUTH_TMP}" "${AGENT_HOME}/.local/share/opencode/auth.json"
chown -R optimizer:optimizer "${AGENT_HOME}"
umask 022

# ---- R2 项目级权限配置：deny bash/webfetch/websearch（显式 deny 在 --auto 下依然生效）----
# P1-d：root 写入、root 拥有（/workspace 不再整体交 agent），agent 不可篡改
printf '%s\n' '{"permission":{"bash":"deny","webfetch":"deny","websearch":"deny","edit":"allow"}}' > /workspace/opencode.json

# ---- R13 运行结束后清理凭证 ----
cleanup() {
    rm -rf "${AUTH_TMP}" "${AGENT_HOME}"
}
trap cleanup EXIT

# ---- P1-d skill 装配：两臂都预建 .opencode/skills，统一交 optimizer ----
# （收敛 opencode 项目内写需求；with 臂拷入技能，without 臂留空目录作对照）
# ---- R17 round-4 P2 199228675：装配失败必须 fail-closed——
# 否则 with 臂静默退化为 without 臂（--read-only 未给 .opencode 配 tmpfs 时
# mkdir/cp/chown 三连失败被静默吞掉），A/B 消融变量被无声破坏；
# 注：只读卷下判定文件写入本身也可能失败，此时以退出码 1 为准。
assembly_fail() {
    R17_REASON="$1" SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"skill assembly failed: "+os.environ["R17_REASON"],"skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    exit 1
}
mkdir -p .opencode/skills || assembly_fail "mkdir .opencode/skills"
if [ "${SKILL_MODE}" = "with" ]; then
    cp -r /staging/skill .opencode/skills/llvm-mca-analysis || assembly_fail "cp /staging/skill -> .opencode/skills"
fi
chown -R optimizer:optimizer .opencode || assembly_fail "chown -R optimizer .opencode"

PROMPT_FILE="prompts/${SKILL_MODE}-skill.md"
if [ ! -f "${PROMPT_FILE}" ]; then
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"unknown SKILL_MODE","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    exit 2
fi

echo "[entrypoint] SKILL_MODE=${SKILL_MODE} model=${MODEL:-deepseek/deepseek-v4-flash}"

# ---- P1-d agent 段降权运行：opencode 以 optimizer 身份执行（HOME=AGENT_HOME 注入）----
# 重定向由 root shell 先行建立 FD：optimizer 进程写已打开的 FD，不受 results
# 目录（root 持有）权限影响；失败仍 fail-closed 落盘判负。
if ! setpriv --reuid=optimizer --regid=optimizer --init-groups \
        env HOME="${AGENT_HOME}" \
        opencode run --auto --format json -m "${MODEL:-deepseek/deepseek-v4-flash}" "$(cat "${PROMPT_FILE}")" \
        > results/trace.jsonl 2> results/opencode.log; then
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"opencode run failed","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
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
    SKILL_MODE="${SKILL_MODE}" python3 - <<'PY'
import json, os
r = json.load(open("results/result.json"))
t = json.load(open("results/trace_summary.json"))
r["skills_invoked"] = t.get("skills_invoked", [])
r["tools_used"] = t.get("tools_used", [])
if os.environ.get("SKILL_MODE") == "with":
    r["skill_assembled"] = True
json.dump(r, open("results/result.json", "w"), ensure_ascii=False, indent=2)
PY
fi

# ---- P1-d 收尾：判定产物全局可读（root 落盘 644 之上再兜底，保 docker cp 后宿主可读）----
chmod -R a+rX results
