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

DEFAULT_IMAGE="kpbot/app-tuner-agent-execute-optimization:1.0-openeuler24.03"
IMAGE="$DEFAULT_IMAGE"
SAVE_DIR=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --save) SAVE_DIR="${2:-}"; shift 2 ;;
        *) IMAGE="$1"; shift ;;
    esac
done

# 默认必须是 Linux 真实路径；CANNBot 环境通过 OPENCODE_BIN 显式指定 cannbot 二进制。
BIN="${OPENCODE_BIN:-/usr/local/bin/opencode}"
AUTH="${OPENCODE_AUTH:-$HOME/.local/share/opencode/auth.json}"
RUN_TIMEOUT="${RUN_TIMEOUT:-1200}"

# fail-closed：路径不存在直接退出，不打印警告后继续。
[ -e "${BIN}" ] || { echo "错误: opencode 二进制不存在 ${BIN}" >&2; exit 1; }
[ -e "${AUTH}" ] || { echo "错误: opencode auth.json 不存在 ${AUTH}" >&2; exit 1; }

# AUTH realpath + 白名单（防任意文件被注入容器）。
AUTH_REAL="$(realpath -e "${AUTH}")" || { echo "错误: 无法解析 auth.json 路径" >&2; exit 1; }
case "${AUTH_REAL}" in
    "$HOME"/.local/share/opencode/*|/etc/opencode/*) ;;
    *) echo "错误: auth.json 路径不在白名单内: ${AUTH_REAL}" >&2; exit 1 ;;
esac
AUTH="${AUTH_REAL}"

# BIN realpath + 白名单（含 CANNBot 二进制）。
BIN_REAL="$(realpath -e "${BIN}")" || { echo "错误: 无法解析 opencode 二进制路径" >&2; exit 1; }
case "${BIN_REAL}" in
    /usr/local/bin/opencode|/usr/local/lib/node_modules/opencode-ai/bin/*|\
    /usr/local/lib/node_modules/cannbot/node_modules/cannbot-linux-arm64/bin/*|\
    "$HOME"/.local/bin/*|"$HOME"/.npm-global/*|"$HOME"/.local/share/opencode/*) ;;
    *) echo "错误: opencode 二进制路径不在白名单内: ${BIN_REAL}" >&2; exit 1 ;;
esac
BIN="${BIN_REAL}"

# BIN ELF 魔数校验（fail-closed）。
BIN_MAGIC="$(head -c 4 "${BIN}" | od -An -tx1 | tr -d ' 
')"
[ "${BIN_MAGIC}" = "7f454c46" ] || { echo "错误: ${BIN} 不是 ELF 二进制 (magic=${BIN_MAGIC})" >&2; exit 1; }

# 可选 sha256 指纹（设置即强制比对）。
if [ -n "${OPENCODE_SHA256:-}" ]; then
    BIN_HASH="$(sha256sum "${BIN}" | awk '{print $1}')"
    [ "${BIN_HASH}" = "${OPENCODE_SHA256}" ] || { echo "错误: opencode sha256 不匹配: got=${BIN_HASH}" >&2; exit 1; }
fi

# DOCKER_RUN_ARGS 白名单：拒绝 --privileged / --pid=host / --volume / -v / --network=host 等。
RUN_ARGS_FILTERED=()
for tok in ${DOCKER_RUN_ARGS:-}; do
    case "${tok}" in
        --cpus*|--memory*|--pids-limit*|--rm|--name=*|--network=bridge|--read-only|--tmpfs*)
            RUN_ARGS_FILTERED+=("${tok}") ;;
        *)
            echo "错误: DOCKER_RUN_ARGS 含不允许的参数: ${tok}" >&2; exit 1 ;;
    esac
done

# provider 环境变量透传（非路径输入）。
ENV_ARGS=()
for var in MODEL ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL ANTHROPIC_MODEL \
           ANTHROPIC_DEFAULT_HAIKU_MODEL ANTHROPIC_DEFAULT_OPUS_MODEL ANTHROPIC_DEFAULT_SONNET_MODEL \
           OPENCODE; do
    if [ -n "${!var:-}" ]; then ENV_ARGS+=(-e "$var"); fi
done

# 凭证整文件 base64 经 env 注入，不做卷挂载。
AUTH_JSON_B64="$(base64 -w0 "${AUTH}")"

echo "  cli=${BIN}" >&2
echo "  auth=${AUTH} (env-injected)" >&2

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

for mode in with without; do
    echo "==> SKILL_MODE=${mode}"
    cid=$(docker run -d ${RUN_ARGS_FILTERED[@]+"${RUN_ARGS_FILTERED[@]}"} \
        ${ENV_ARGS[@]+"${ENV_ARGS[@]}"} \
        -e SKILL_MODE=${mode} \
        -e "OPENCODE_AUTH_JSON=${AUTH_JSON_B64}" \
        -v "${BIN}":/usr/local/bin/opencode:ro \
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
        docker cp "${cid}":/workspace/results/report.md "${SAVE_DIR}/${mode}.report.md" 2>/dev/null || true
        docker cp "${cid}":/workspace/results/mock_invocations.log "${SAVE_DIR}/${mode}.mock_invocations.log" 2>/dev/null || true
    fi

    docker rm "${cid}" >/dev/null
done

unset AUTH_JSON_B64

python3 - "${TMP}" <<'PY'
import json
import os
import sys

tmp = sys.argv[1]
keys = ["skill_mode", "exit", "success", "one_variable", "changed_param",
        "invocations", "skills_invoked"]
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
