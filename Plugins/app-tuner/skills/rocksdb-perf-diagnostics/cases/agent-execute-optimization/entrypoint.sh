#!/usr/bin/env bash
# 驱动 opencode → 解析 trace → verify → 合并 skill 调用证据。
set -uo pipefail

cd /workspace
mkdir -p results

# ---- 非 root + 权限收敛 ----
AGENT_UID=1000
AGENT_GID=1000
AGENT_HOME=/home/agent
CHROOT_BIN="$(command -v chroot || true)"
[ -n "${CHROOT_BIN}" ] || { echo "错误: chroot 不存在" >&2; exit 1; }
grep -q '^agent:' /etc/passwd 2>/dev/null || echo "agent:x:${AGENT_UID}:${AGENT_GID}::${AGENT_HOME}:/bin/bash" >> /etc/passwd
grep -q '^agent:' /etc/group 2>/dev/null || echo "agent:x:${AGENT_GID}:" >> /etc/group
mkdir -p "${AGENT_HOME}/.local/share/opencode" "${AGENT_HOME}/.config/opencode"
chown -R "${AGENT_UID}:${AGENT_GID}" "${AGENT_HOME}"
chmod 700 "${AGENT_HOME}/.local/share/opencode"
chown "${AGENT_UID}:${AGENT_GID}" /workspace/results
chmod 700 /workspace/results

# 凭证通过 env 注入，落文件后从环境移除；文件在 agent home，600。
if [ -n "${OPENCODE_AUTH_JSON:-}" ]; then
    (
        umask 077
        printf '%s' "${OPENCODE_AUTH_JSON}" | base64 -d > "${AGENT_HOME}/.local/share/opencode/auth.json"
    )
    chown "${AGENT_UID}:${AGENT_GID}" "${AGENT_HOME}/.local/share/opencode/auth.json"
    chmod 600 "${AGENT_HOME}/.local/share/opencode/auth.json"
    unset OPENCODE_AUTH_JSON
fi

cat > /workspace/opencode.json <<'JSON'
{
  "$schema": "https://opencode.ai/config.json",
  "permission": {
    "webfetch": "deny",
    "websearch": "deny",
    "external_directory": "deny",
    "edit": "allow",
    "read": {
      "*": "allow",
      "*auth.json": "deny",
      "/home/agent/.local/share/opencode/*": "deny",
      "*.env": "ask",
      "*.env.*": "ask"
    },
    "bash": {
      "*": "allow",
      "curl *": "deny",
      "wget *": "deny",
      "ssh *": "deny",
      "nc *": "deny",
      "ncat *": "deny",
      "rsync *": "deny",
      "scp *": "deny",
      "cat *auth.json": "deny",
      "cat /home/agent/.local/share/opencode/*": "deny",
      "cat /root/.local/share/opencode/*": "deny",
      "cat /proc/self/environ": "deny",
      "cat /proc/*/environ": "deny",
      "cat /proc/*": "deny",
      "python3 -c *": "deny",
      "env": "deny",
      "printenv": "deny"
    }
  }
}
JSON
chmod 644 /workspace/opencode.json
mkdir -p /workspace/.opencode
cp /workspace/opencode.json /workspace/.opencode/opencode.json
chmod 755 /workspace/.opencode
chmod 644 /workspace/.opencode/opencode.json

SKILL_MODE="${SKILL_MODE:-with}" # with | without
export MOCK_LOG="${MOCK_LOG:-/workspace/results/mock_invocations.log}"

if [ "${SKILL_MODE}" = "with" ]; then
    mkdir -p .opencode/skills
    cp -r /staging/skill .opencode/skills/rocksdb-perf-diagnostics
    # 双保险：即使镜像里误带测试资产，也不让 agent 看到答案。
    rm -rf .opencode/skills/rocksdb-perf-diagnostics/evals \
           .opencode/skills/rocksdb-perf-diagnostics/cases
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
