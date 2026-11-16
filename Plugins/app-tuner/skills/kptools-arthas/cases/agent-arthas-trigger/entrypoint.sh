#!/usr/bin/env bash
set -uo pipefail
cd /workspace
mkdir -p results
SKILL_MODE="${SKILL_MODE:-with}"
SKILL_NAME="kptools-arthas"
export ARTHAS_BOOT_JAR="${ARTHAS_BOOT_JAR:-/opt/arthas/arthas-boot.jar}"
AGENT_USER="arthas-agent"
AGENT_HOME="/home/${AGENT_USER}"

# ---- 凭证重组：OPENCODE_AUTH_JSON 整文件 base64 → agent 隔离 HOME 下 auth.json ----
# docs/agent-case-security-notes.md 规则 1：不做 -v 卷挂载、600 权限、运行后即删；
# 凭证文件位于 /workspace 之外，读取禁令见两臂 prompts 与 SKILL 安全约束节。
umask 077
AUTH_DIR="${AGENT_HOME}/.local/share/opencode"
mkdir -p "${AUTH_DIR}"
AUTH_TMP="$(mktemp /tmp/opencode_auth.XXXXXX)"
printf '%s' "${OPENCODE_AUTH_JSON:-}" | base64 -d > "${AUTH_TMP}"
install -m 600 -o "${AGENT_USER}" -g "${AGENT_USER}" "${AUTH_TMP}" "${AUTH_DIR}/auth.json"
rm -f "${AUTH_TMP}"

# ---- 项目级权限配置：deny webfetch/websearch（外发通道；显式 deny 在 --auto 下依然生效）----
# bash/edit 保留：本用例工作流需要执行 collect.sh 与落盘证据。root 持有、agent 不可改
# （/workspace 根对 agent 不可写）。
printf '%s\n' '{"permission":{"webfetch":"deny","websearch":"deny"}}' > /workspace/opencode.json

if [ "${SKILL_MODE}" = "with" ]; then
    mkdir -p .opencode/skills
    cp -r /staging/skill ".opencode/skills/${SKILL_NAME}"
fi
# agent 可写面收敛（规则 3）：skill 装配目录与证据目录归 agent；
# /workspace 根、results/、opencode.json 保持 root 拥有
chown -R "${AGENT_USER}:${AGENT_USER}" .opencode
mkdir -p /workspace/arthas-evidence
chown "${AGENT_USER}:${AGENT_USER}" /workspace/arthas-evidence

# 启动 workload（with/without 同一前提；与 agent 同 uid 运行以允许同 uid attach）并注入 PID 到 prompt
cat > /workspace/DemoApp.java <<'JAVA'
public class DemoApp {
    static final Object LOCK = new Object();

    static void sleep(long ms) {
        try { Thread.sleep(ms); } catch (InterruptedException ignored) {}
    }

    static void parse(int id)   { sleep(40); }   // 40ms
    static void io(int id)      { sleep(120); }  // 120ms：最耗时段
    static void compute(int id) {
        long s = 0;
        for (int i = 0; i < 20000; i++) { s += i * id; }
    }

    static int handle(int id) {
        parse(id);
        io(id);
        compute(id);
        return id;
    }

    static void cpuBurn() {
        long s = 0;
        while (true) { s += 1; }
    }

    public static void main(String[] args) {
        Thread apiWorker = new Thread(() -> {
            while (true) {
                handle((int) (System.nanoTime() % 100));
                sleep(50);
            }
        }, "api-worker");
        Thread lockHolder = new Thread(() -> {
            synchronized (LOCK) { sleep(600000L); }
        }, "lock-holder");
        Thread lockWaiter = new Thread(() -> {
            synchronized (LOCK) { }
        }, "lock-waiter");
        for (int i = 0; i < 2; i++) {
            new Thread(DemoApp::cpuBurn, "cpu-burner").start();
        }
        apiWorker.start();
        lockHolder.start();
        sleep(100);
        lockWaiter.start();
        sleep(600000L);
    }
}
JAVA
javac -d /workspace /workspace/DemoApp.java
setpriv --reuid="${AGENT_USER}" --regid="${AGENT_USER}" --init-groups \
    env HOME="${AGENT_HOME}" java -cp /workspace DemoApp & JPID=$!
sleep 3
echo "[entrypoint] DemoApp PID=$JPID"
trap 'kill $JPID 2>/dev/null; rm -f "${AUTH_DIR}/auth.json"' EXIT

sed "s/__TARGET_PID__/${JPID}/g" "prompts/${SKILL_MODE}-skill.md" > "prompts/${SKILL_MODE}-skill.eff.md"
PROMPT_FILE="prompts/${SKILL_MODE}-skill.eff.md"
write_fail() { SKILL_MODE="${SKILL_MODE}" python3 -c "import json,os;json.dump({'success':False,'reason':'$1','skill_mode':os.environ['SKILL_MODE']},open('results/result.json','w'),indent=2)"; exit 1; }
[ -f "${PROMPT_FILE}" ] || { write_fail "missing prompt"; }

# agent 段以非 root 用户运行（规则 3）；trace/log FD 由 root shell 先行建立
if ! setpriv --reuid="${AGENT_USER}" --regid="${AGENT_USER}" --init-groups \
    env HOME="${AGENT_HOME}" opencode run --auto --format json -m "${MODEL:-opencode/glm-5.3-flash-free}" "$(cat "${PROMPT_FILE}")" \
        > results/trace.jsonl 2> results/opencode.log; then
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
