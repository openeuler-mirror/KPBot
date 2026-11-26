#!/usr/bin/env bash
# 驱动 opencode → 解析 trace → verify → 合并 skill 调用证据进 result.json。
#
# 安全模型（docs/agent-case-security-notes.md §1/§3）：
#   - 凭证整文件 base64 经 OPENCODE_AUTH_JSON env 注入，entrypoint 解码到
#     runner HOME 下 600 文件，agent 启动前 unset，退出时删除；
#   - 评分链 entrypoint/verify/prompts/fixtures 由 root 拥有，.pristine.sha256
#     在 agent 前后校验；
#   - agent 以非特权用户 runner 运行；results/ 为 root 700，agent 只写 work/；
#     verify 以 root 运行并先 rm 旧结果，agent 无法预置/替换 result.json。
set -uo pipefail

cd /workspace
mkdir -p results work
chmod 700 results
chown runner:runner work 2>/dev/null || true
chmod 700 work
umask 077

SKILL_MODE="${SKILL_MODE:-with}" # with | without
MODEL="${MODEL:-deepseek/deepseek-v4-flash}"
export MOCK_LOG="${MOCK_LOG:-/workspace/work/mock_invocations.jsonl}"

fail() {
    local reason=$1
    python3 - "$reason" "${SKILL_MODE}" <<'PY'
import json, sys
json.dump({"success": False, "reason": sys.argv[1], "skill_mode": sys.argv[2]},
          open("results/result.json", "w"), ensure_ascii=False, indent=2)
PY
    echo "[entrypoint] FAILED: ${reason}" >&2
    exit 1
}

[ "$(id -u)" = "0" ] || fail "entrypoint must run as root"
command -v sha256sum >/dev/null 2>&1 || fail "sha256sum is required"
command -v setpriv >/dev/null 2>&1 || fail "setpriv (util-linux) is required"
sha256sum -c .pristine.sha256 >/dev/null 2>&1 || fail "case integrity check failed before agent"

# 清掉上一轮/预置的 agent 证据；result.json 由 verify 在 root 侧生成
rm -f work/mock_invocations.jsonl work/report.md work/base.conf

# ---- 凭证：env(base64) → runner HOME 下 600，agent 无 env 可见 ----
[ -n "${OPENCODE_AUTH_JSON:-}" ] || fail "OPENCODE_AUTH_JSON is required"
AUTH_DIR="/home/runner/.local/share/opencode"
mkdir -p "${AUTH_DIR}" || fail "cannot create runner HOME"
printf '%s' "${OPENCODE_AUTH_JSON}" | base64 -d > "${AUTH_DIR}/auth.json" \
    || fail "invalid OPENCODE_AUTH_JSON (base64)"
chown -R runner:runner /home/runner/.local/share/opencode
chmod 700 "${AUTH_DIR}"
chmod 600 "${AUTH_DIR}/auth.json"
unset OPENCODE_AUTH_JSON
cleanup() { rm -f "${AUTH_DIR}/auth.json"; }
trap cleanup EXIT

# ---- opencode 权限：可执行 mock（bash），禁网络外发与 /workspace 外读取 ----
cat > /workspace/opencode.json <<'JSON'
{"permission":{"bash":"allow","edit":"allow","webfetch":"deny","websearch":"deny","external_directory":"deny","read":{"*":"allow","*/auth.json":"deny"}}}
JSON
chmod 444 /workspace/opencode.json

# ---- with 臂接入被测 skill；只含 SKILL.md + references/，root 只读 ----
if [ "${SKILL_MODE}" = "with" ]; then
    mkdir -p .opencode/skills/nginx-baseline-setup
    cp -r /staging/skill/. .opencode/skills/nginx-baseline-setup/ \
        || fail "cannot install skill into workspace"
    chown -R root:root .opencode
    chmod -R a+rX .opencode
    chmod -R a-w .opencode
fi

PROMPT_FILE="prompts/${SKILL_MODE}-skill.md"
[ -f "${PROMPT_FILE}" ] || fail "unknown SKILL_MODE: ${SKILL_MODE}"

echo "[entrypoint] SKILL_MODE=${SKILL_MODE} model=${MODEL}"

# 重定向 FD 由 root 先建立；setpriv 后 agent 以 runner 身份运行
if ! timeout -k 10 "${AGENT_TIMEOUT:-600}" \
        setpriv --reuid="$(id -u runner)" --regid="$(id -g runner)" --init-groups \
        env HOME=/home/runner USER=runner LOGNAME=runner \
            PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
            MOCK_LOG="${MOCK_LOG}" \
        opencode run --auto --format json -m "${MODEL}" "$(cat "${PROMPT_FILE}")" \
        > results/trace.jsonl 2> results/opencode.log; then
    fail "opencode run failed (see results/opencode.log)"
fi

sha256sum -c .pristine.sha256 >/dev/null 2>&1 || fail "case integrity check failed after agent"

./verify.sh
verify_rc=$?

# 解析 trace：工具使用与 skill 调用证据。
python3 - <<'PY' || fail "trace parse failed"
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

# 合并 skill 调用证据；result.json 缺失/不可解析即 fail-closed（不沿用预置内容）。
# with 臂未真实调用 skill、without 臂反而加载 skill 均判失败。
python3 - <<'PY'
import json, os, sys
path = "results/result.json"
try:
    data = json.load(open(path, encoding="utf-8"))
except Exception:
    sys.exit(2)
if not isinstance(data, dict):
    sys.exit(2)
try:
    ts = json.load(open("results/trace_summary.json", encoding="utf-8"))
except Exception:
    ts = {"skills_invoked": [], "tools_used": []}
skills = ts.get("skills_invoked", []) or []
data["skills_invoked"] = skills
data["tools_used"] = ts.get("tools_used", []) or []
data["skill_mode"] = os.environ.get("SKILL_MODE", "")
if os.environ.get("SKILL_MODE") == "with" and "nginx-baseline-setup" not in skills:
    data["success"] = False
    data["reason"] = "expected skill was not invoked in trace"
elif os.environ.get("SKILL_MODE") == "without" and skills:
    data["success"] = False
    data["reason"] = "skill was loaded in without mode"
json.dump(data, open(path, "w"), ensure_ascii=False, indent=2)
if data.get("reason") in ("expected skill was not invoked in trace", "skill was loaded in without mode"):
    sys.exit(3)
if os.environ.get("SKILL_MODE") == "with" and not data.get("success"):
    sys.exit(4)
PY
merge_rc=$?

if [ "${merge_rc}" -eq 2 ]; then
    fail "verify did not produce a valid result.json"
elif [ "${merge_rc}" -eq 3 ]; then
    echo "[entrypoint] skill invocation gate failed (with must invoke, without must not)" >&2
    exit 1
elif [ "${merge_rc}" -eq 4 ]; then
    echo "[entrypoint] with arm did not satisfy verify assertions" >&2
    exit 1
elif [ "${merge_rc}" -ne 0 ]; then
    fail "result merge failed"
fi

if [ "${verify_rc}" -ne 0 ]; then
    echo "[entrypoint] verify.sh exited ${verify_rc}" >&2
    exit 1
fi
