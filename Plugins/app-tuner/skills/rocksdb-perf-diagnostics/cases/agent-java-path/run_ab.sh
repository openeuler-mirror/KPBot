#!/usr/bin/env bash
# A/B 对照运行器（本用例自带）。以 SKILL_MODE=with / without 各跑一次，输出客观对照表。
#
# 用法:
#   ./run_ab.sh [--save <目录>] [<image>]
# 环境变量:
#   OPENCODE_BIN     opencode 二进制路径
#   OPENCODE_AUTH    auth.json 路径
#   DOCKER_RUN_ARGS  追加给 docker run 的参数（默认 --network host）
#   RUN_TIMEOUT      单臂超时秒数（默认 1200）
set -uo pipefail

DEFAULT_IMAGE="kpbot/app-tuner-agent-java-path:1.0-openeuler24.03"
IMAGE="$DEFAULT_IMAGE"
SAVE_DIR=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --save) SAVE_DIR="${2:-}"; shift 2 ;;
        *) IMAGE="$1"; shift ;;
    esac
done

# 优先使用 cannbot 二进制（CANNBot 环境的 provider 在它里面），否则退回 opencode。
CANNBOT_BIN=/usr/local/lib/node_modules/cannbot/node_modules/cannbot-linux-arm64/bin/cannbot
if [ -n "${OPENCODE_BIN:-}" ]; then
    BIN="$OPENCODE_BIN"
elif [ -x "$CANNBOT_BIN" ]; then
    BIN="$CANNBOT_BIN"
else
    BIN="/usr/local/lib/node_modules/opencode-ai/bin/opencode.exe"
fi
AUTH="${OPENCODE_AUTH:-$HOME/.local/share/opencode/auth.json}"
RUN_ARGS="${DOCKER_RUN_ARGS:---network host}"
RUN_TIMEOUT="${RUN_TIMEOUT:-1200}"

# 透传 CANNBot / Anthropic provider 环境变量。`-e VAR` 从宿主环境取值，
# 不把 secret 写进命令行。
ENV_ARGS=()
for var in \
    MODEL \
    ANTHROPIC_AUTH_TOKEN \
    ANTHROPIC_BASE_URL \
    ANTHROPIC_MODEL \
    ANTHROPIC_DEFAULT_HAIKU_MODEL \
    ANTHROPIC_DEFAULT_OPUS_MODEL \
    ANTHROPIC_DEFAULT_SONNET_MODEL \
    OPENCODE \
    OPENCODE_CONFIG_CONTENT; do
    if [ -n "${!var:-}" ]; then
        ENV_ARGS+=(-e "$var")
    fi
done

# 可选：把宿主的 opencode/cannbot 配置目录挂进容器。
CONFIG_DIR="${OPENCODE_CONFIG_DIR:-$HOME/.config/opencode}"
MOUNT_CONFIG=()
if [ -d "$CONFIG_DIR" ]; then
    MOUNT_CONFIG=(-v "$CONFIG_DIR":/root/.config/opencode:ro)
fi

for path in "${BIN}" "${AUTH}"; do
    [ -e "${path}" ] || echo "警告: 路径不存在 ${path}" >&2
done

echo "  cli=${BIN}" >&2
echo "  auth=${AUTH}" >&2
echo "  config=${CONFIG_DIR}" >&2

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

for mode in with without; do
    echo "==> SKILL_MODE=${mode}"
    cid=$(docker run -d ${RUN_ARGS} ${ENV_ARGS[@]+"${ENV_ARGS[@]}"} ${MOUNT_CONFIG[@]+"${MOUNT_CONFIG[@]}"} \
        -e SKILL_MODE=${mode} \
        -v "${BIN}":/usr/local/bin/opencode \
        -v "${AUTH}":/root/.local/share/opencode/auth.json:ro \
        "${IMAGE}") || { echo "docker run 失败" >&2; exit 1; }

    if timeout "${RUN_TIMEOUT}" docker wait "${cid}" >/dev/null; then
        docker inspect -f '{{.State.ExitCode}}' "${cid}" > "${TMP}/${mode}.exit"
    else
        echo "  [warn] ${mode}: timeout after ${RUN_TIMEOUT}s; killing container" >&2
        docker kill "${cid}" >/dev/null 2>&1 || true
        echo 124 > "${TMP}/${mode}.exit"
    fi

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
        docker cp "${cid}":/workspace/results/answer.md "${SAVE_DIR}/${mode}.answer.md" 2>/dev/null || true
    fi

    docker rm "${cid}" >/dev/null
done

python3 - "${TMP}" <<'PY'
import json
import os
import sys

tmp = sys.argv[1]
keys = ["skill_mode", "exit", "success", "assertion_pass_rate", "skills_invoked"]
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
