#!/usr/bin/env bash
# A/B 对照运行器（依 docs/case-spec.md §5）：with/without 各跑一次，输出客观对照表。
set -uo pipefail
DEFAULT_IMAGE="kpbot/app-tuner-agent-agent-os-trigger:1.0-openeuler24.03"
IMAGE="$DEFAULT_IMAGE"; SAVE_DIR=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --save) SAVE_DIR="${2:-}"; shift 2 ;;
        *) IMAGE="$1"; shift ;;
    esac
done
BIN="${OPENCODE_BIN:-/usr/local/lib/node_modules/opencode-ai/bin/opencode.exe}"
AUTH="${OPENCODE_AUTH:-$HOME/.local/share/opencode/auth.json}"
CFG="${OPENCODE_CONFIG:-$HOME/.config/opencode}"
RUN_ARGS="${DOCKER_RUN_ARGS:---network host}"
for path in "${BIN}" "${AUTH}"; do
    [ -e "${path}" ] || echo "警告: 路径不存在 ${path}" >&2
done
[ -e "${CFG}" ] || echo "警告: opencode 配置目录不存在 ${CFG}" >&2
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
for mode in with without; do
    echo "==> SKILL_MODE=${mode}"
    cid=$(docker run -d ${RUN_ARGS} -e SKILL_MODE=${mode} -e MODEL="${MODEL:-opencode/glm-5.3-flash-free}" \
        -v "${BIN}":/usr/local/bin/opencode \
        -v "${AUTH}":/root/.local/share/opencode/auth.json:ro \
        -v "${CFG}":/root/.config/opencode:ro \
        "${IMAGE}") || { echo "docker run 失败" >&2; exit 1; }
    docker wait "${cid}" >/dev/null
    docker inspect -f '{{.State.ExitCode}}' "${cid}" > "${TMP}/${mode}.exit"
    docker cp "${cid}":/workspace/results/result.json "${TMP}/${mode}.json" 2>/dev/null || true
    if [ -n "${SAVE_DIR}" ]; then
        mkdir -p "${SAVE_DIR}"
        for f in result.json trace.jsonl trace_summary.json opencode.log; do
            docker cp "${cid}":/workspace/results/${f} "${SAVE_DIR}/${mode}.${f}" 2>/dev/null || true
        done
    fi
    docker rm "${cid}" >/dev/null
done
python3 - "${TMP}" <<'PY'
import json, os, sys
tmp = sys.argv[1]
keys = ["skill_mode", "exit", "success", "trigger", "skills_invoked", "tools_used"]
rows = []
for m in ("with", "without"):
    d = {}
    p = os.path.join(tmp, m + ".json")
    if os.path.exists(p):
        try: d = json.load(open(p))
        except Exception: d = {}
    d.setdefault("skill_mode", m)
    d.setdefault("exit", open(os.path.join(tmp, m + ".exit")).read().strip())
    rows.append(d)
print("| " + " | ".join(keys) + " |")
print("|-" + "-|-".join("-" * len(k) for k in keys) + "-|")
for d in rows:
    print("| " + " | ".join(str(d.get(k, "")) for k in keys) + " |")
PY
