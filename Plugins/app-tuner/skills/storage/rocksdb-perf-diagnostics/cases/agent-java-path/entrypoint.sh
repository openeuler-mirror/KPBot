#!/usr/bin/env bash
# 驱动 opencode → 解析 trace → verify → 合并 skill 调用证据。
set -uo pipefail

cd /workspace
mkdir -p results || exit 1

# ---- 非 root + 权限收敛 ----
AGENT_UID=1000
AGENT_GID=1000
AGENT_HOME=/home/agent
CHROOT_BIN="$(command -v chroot || true)"
[ -n "${CHROOT_BIN}" ] || { echo "错误: chroot 不存在" >&2; exit 1; }
if ! grep -q '^agent:' /etc/passwd 2>/dev/null; then
    echo "agent:x:${AGENT_UID}:${AGENT_GID}::${AGENT_HOME}:/bin/bash" >> /etc/passwd || { echo "错误: 无法写入 /etc/passwd" >&2; exit 1; }
fi
if ! grep -q '^agent:' /etc/group 2>/dev/null; then
    echo "agent:x:${AGENT_GID}:" >> /etc/group || { echo "错误: 无法写入 /etc/group" >&2; exit 1; }
fi
mkdir -p "${AGENT_HOME}/.local/share/opencode" "${AGENT_HOME}/.config/opencode" || { echo "错误: 无法创建 agent home" >&2; exit 1; }
chown -R "${AGENT_UID}:${AGENT_GID}" "${AGENT_HOME}" || { echo "错误: chown agent home 失败" >&2; exit 1; }
chmod 700 "${AGENT_HOME}/.local/share/opencode" || { echo "错误: chmod agent home 失败" >&2; exit 1; }
chown "${AGENT_UID}:${AGENT_GID}" /workspace/results || { echo "错误: chown results 失败" >&2; exit 1; }
chmod 700 /workspace/results || { echo "错误: chmod results 失败" >&2; exit 1; }

# 凭证通过 env 注入，落文件后从环境移除；文件在 agent home，600。
if [ -n "${OPENCODE_AUTH_JSON:-}" ]; then
    (
        umask 077
        printf '%s' "${OPENCODE_AUTH_JSON}" | base64 -d > "${AGENT_HOME}/.local/share/opencode/auth.json"
    )
    chown "${AGENT_UID}:${AGENT_GID}" "${AGENT_HOME}/.local/share/opencode/auth.json" || { echo "错误: chown auth 失败" >&2; exit 1; }
    chmod 600 "${AGENT_HOME}/.local/share/opencode/auth.json" || { echo "错误: chmod auth 失败" >&2; exit 1; }
    unset OPENCODE_AUTH_JSON
    [ -s "${AGENT_HOME}/.local/share/opencode/auth.json" ] || { echo "错误: 凭证写入失败（只读根？）" >&2; exit 1; }
fi

# 权限配置预置在镜像中；这里只做 fail-closed 校验，不做运行时写入。
[ -s /workspace/opencode.json ] || { echo "错误: 权限配置缺失 /workspace/opencode.json" >&2; exit 1; }
[ -s /workspace/.opencode/opencode.json ] || { echo "错误: 权限配置缺失 /workspace/.opencode/opencode.json" >&2; exit 1; }

SKILL_MODE="${SKILL_MODE:-with}" # with | without

if [ "${SKILL_MODE}" = "with" ]; then
    mkdir -p .opencode/skills || { echo "错误: 无法创建 .opencode/skills" >&2; exit 1; }
    cp -r /staging/skill .opencode/skills/rocksdb-perf-diagnostics || { echo "错误: 复制 skill 失败" >&2; exit 1; }
    # 双保险：即使镜像里误带测试资产，也不让 agent 看到答案。
    rm -rf .opencode/skills/rocksdb-perf-diagnostics/evals \
           .opencode/skills/rocksdb-perf-diagnostics/cases || { echo "错误: 清理测试资产失败" >&2; exit 1; }
fi

PROMPT_FILE="/opt/prompts/${SKILL_MODE}-skill.md"
if [ ! -f "${PROMPT_FILE}" ]; then
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"unknown SKILL_MODE","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    exit 2
fi

echo "[entrypoint] SKILL_MODE=${SKILL_MODE} model=${MODEL:-deepseek/deepseek-v4-flash}"

AUTO_FLAG="--auto"
if opencode run --help 2>&1 | grep -q -- "--dangerously-skip-permissions"; then
    AUTO_FLAG="--dangerously-skip-permissions"
fi

if ! "${CHROOT_BIN}" --skip-chdir --userspec="${AGENT_UID}:${AGENT_GID}" / \
        env HOME="${AGENT_HOME}" PATH=/usr/local/bin:/usr/bin:/bin \
        opencode run "${AUTO_FLAG}" --format json -m "${MODEL:-deepseek/deepseek-v4-flash}" "$(cat "${PROMPT_FILE}")" \
        > results/trace.jsonl 2> results/opencode.log; then
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"opencode run failed","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
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

/opt/verify.sh
verify_rc=$?

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

exit "${verify_rc}"
