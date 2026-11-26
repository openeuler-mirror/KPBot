#!/usr/bin/env bash
# =============================================================================
# A/B 对照运行器（本用例自带）
# 以 SKILL_MODE=with / without 各跑一次，收集 result.json，输出客观对照表。
# 不做统计/阈值加工，只展示原始结果。
#
# 用法:
#   ./run_ab.sh [--save <目录>] [<image>]
# 环境变量:
#   OPENCODE_BIN     opencode 二进制路径（默认 /usr/local/lib/node_modules/opencode-ai/bin/opencode.exe）
#   OPENCODE_AUTH    auth.json 路径（默认 ~/.local/share/opencode/auth.json）
#   DOCKER_RUN_ARGS  追加给 docker run 的参数（默认 --network host）
# =============================================================================
set -uo pipefail

DEFAULT_IMAGE="kpbot/code-optimizer-agent-memlayout:1.0-openeuler24.03"
IMAGE="$DEFAULT_IMAGE"
SAVE_DIR=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --save) SAVE_DIR="${2:-}"; shift 2 ;;
        *) IMAGE="$1"; shift ;;
    esac
done

BIN="${OPENCODE_BIN:-/usr/local/lib/node_modules/opencode-ai/bin/opencode.exe}"
AUTH="${OPENCODE_AUTH:-$HOME/.local/share/opencode/auth.json}"
RUN_ARGS="${DOCKER_RUN_ARGS:---network host}"

for path in "${BIN}" "${AUTH}"; do
    if [ ! -e "${path}" ]; then
        echo "警告: 路径不存在 ${path}" >&2
    fi
done

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

for mode in with without; do
    echo "==> SKILL_MODE=${mode}"
    cid=$(docker run -d ${RUN_ARGS} -e SKILL_MODE=${mode} \
        -v "${BIN}":/usr/local/bin/opencode \
        -v "${AUTH}":/root/.local/share/opencode/auth.json:ro \
        "${IMAGE}") || { echo "docker run 失败" >&2; exit 1; }

    docker wait "${cid}" >/dev/null
    docker inspect -f '{{.State.ExitCode}}' "${cid}" > "${TMP}/${mode}.exit"

    docker cp "${cid}":/workspace/results/result.json "${TMP}/${mode}.json" 2>/dev/null || true
    if [ ! -s "${TMP}/${mode}.json" ]; then
        echo "  [warn] ${mode}: result.json 缺失或为空（镜像可能过期，请重新 docker build）"
        docker cp "${cid}":/workspace/results/opencode.log "${TMP}/${mode}.opencode.log" 2>/dev/null || true
        tail -n 12 "${TMP}/${mode}.opencode.log" 2>/dev/null | sed 's/^/    | /'
    fi

    if [ -n "${SAVE_DIR}" ]; then
        mkdir -p "${SAVE_DIR}"
        docker cp "${cid}":/workspace/results/result.json "${SAVE_DIR}/${mode}.result.json" 2>/dev/null || true
        docker cp "${cid}":/workspace/results/trace.jsonl "${SAVE_DIR}/${mode}.trace.jsonl" 2>/dev/null || true
        docker cp "${cid}":/workspace/results/trace_summary.json "${SAVE_DIR}/${mode}.trace_summary.json" 2>/dev/null || true
        docker cp "${cid}":/workspace/results/opencode.log "${SAVE_DIR}/${mode}.opencode.log" 2>/dev/null || true
        docker cp "${cid}":/workspace/opt/kernel_opt.c "${SAVE_DIR}/${mode}.kernel_opt.c" 2>/dev/null || true
    fi

    docker rm "${cid}" >/dev/null
done

python3 - "${TMP}" <<'PY'
import json, os, sys
tmp = sys.argv[1]
keys = ["skill_mode", "exit", "success", "optimized_correct", "speedup",
        "baseline_time_ms", "optimized_time_ms", "skills_invoked"]
rows = []
for m in ("with", "without"):
    d = {}
    p = os.path.join(tmp, m + ".json")
    if os.path.exists(p):
        try:
            d = json.load(open(p))
        except Exception:
            d = {}
    d.setdefault("skill_mode", m)
    d.setdefault("exit", open(os.path.join(tmp, m + ".exit")).read().strip())
    rows.append(d)

print("| " + " | ".join(keys) + " |")
print("|-" + "-|-".join("-" * len(k) for k in keys) + "-|")
for d in rows:
    print("| " + " | ".join(str(d.get(k, "")) for k in keys) + " |")
PY