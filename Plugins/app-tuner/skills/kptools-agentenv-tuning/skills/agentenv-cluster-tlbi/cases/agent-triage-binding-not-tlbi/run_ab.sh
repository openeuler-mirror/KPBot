#!/usr/bin/env bash
# =============================================================================
# A/B 对照运行器（本用例自带）
# 以 SKILL_MODE=with / without 各跑一次，收集 result.json，输出客观对照表。
#
# 用法:
#   ./run_ab.sh [--save <目录>] [<image>]
# 环境变量:
#   OPENCODE_BIN     opencode 二进制路径（默认 /usr/local/bin/opencode）
#   OPENCODE_AUTH    auth.json 路径（默认 ~/.local/share/opencode/auth.json）
#                    —— 运行时只读卷挂载进容器（runtime_mount），不经命令行/环境变量传递
#   DOCKER_RUN_ARGS  追加给 docker run 的参数（默认为空；白名单:
#                    --cpus* / --memory* / --pids-limit* / --rm / --name=*
#                    / --network=bridge / --network=none，其余一律拒绝）
#   MODEL            可选模型标识（默认 deepseek/deepseek-v4-flash）
#   AGENTENV_CASE_TIMEOUT  单次 agent 运行的超时秒数（默认 900）
#   OPENCODE_SHA256  可选：opencode 二进制 sha256 指纹；设置即强制比对（fail-closed）
# =============================================================================
set -uo pipefail

DEFAULT_IMAGE="kpbot/app-tuner-agentenv-tlbi-triage:1.0-openeuler24.03"
IMAGE="$DEFAULT_IMAGE"
SAVE_DIR=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --save) SAVE_DIR="${2:-}"; shift 2 ;;
        *) IMAGE="$1"; shift ;;
    esac
done

BIN="${OPENCODE_BIN:-/usr/local/bin/opencode}"
AUTH="${OPENCODE_AUTH:-$HOME/.local/share/opencode/auth.json}"
RUN_ARGS="${DOCKER_RUN_ARGS:-}"

[ -e "${BIN}" ] || { echo "错误: opencode 二进制不存在" >&2; exit 1; }
[ -e "${AUTH}" ] || { echo "错误: opencode auth.json 不存在" >&2; exit 1; }

AUTH_REAL="$(realpath "${AUTH}")"
case "${AUTH_REAL}" in
    $HOME/.local/share/opencode/*|/etc/opencode/*) ;;
    *)
        echo "错误: opencode auth.json 路径不在白名单内" >&2
        exit 1
        ;;
esac
AUTH="${AUTH_REAL}"

BIN_REAL="$(realpath "${BIN}")"
case "${BIN_REAL}" in
    /usr/local/bin/opencode|/usr/local/lib/node_modules/opencode-ai/bin/*|\
$HOME/.local/bin/*|$HOME/.npm-global/*|$HOME/.local/share/opencode/*) ;;
    *)
        echo "错误: opencode 二进制路径不在白名单内" >&2
        exit 1
        ;;
esac
BIN="${BIN_REAL}"

BIN_MAGIC="$(head -c 4 "${BIN}" | od -An -tx1 | tr -d ' \n')"
if [ "${BIN_MAGIC}" != "7f454c46" ]; then
    echo "错误: opencode 不是 ELF 二进制" >&2
    exit 1
fi

if [ -n "${OPENCODE_SHA256:-}" ]; then
    BIN_HASH="$(sha256sum "${BIN}" | awk '{print $1}')"
    if [ "${BIN_HASH}" != "${OPENCODE_SHA256}" ]; then
        echo "错误: opencode sha256 不匹配: got=${BIN_HASH} expect=${OPENCODE_SHA256}" >&2
        exit 1
    fi
fi

RUN_ARGS_FILTERED=()
for tok in ${RUN_ARGS}; do
    case "${tok}" in
        --rm) ;; # 先提取结果，再由本脚本删除容器。
        --cpus*|--memory*|--pids-limit*|--name=*|--network=bridge|--network=none)
            RUN_ARGS_FILTERED+=("${tok}")
            ;;
        *)
            echo "错误: DOCKER_RUN_ARGS 含不允许的参数: ${tok}" >&2
            exit 1
            ;;
    esac
done

TMP="$(mktemp -d)"
cid=""
cleanup() {
    if [ -n "$cid" ]; then
        docker rm -f "$cid" >/dev/null 2>&1 || true
    fi
    rm -rf "$TMP"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
had_failure=0

for mode in with without; do
    echo "==> SKILL_MODE=${mode}"
    cid=$(docker run -d ${RUN_ARGS_FILTERED[@]+"${RUN_ARGS_FILTERED[@]}"} \
        -e SKILL_MODE=${mode} \
        -e "MODEL=${MODEL:-deepseek/deepseek-v4-flash}" \
        -e "AGENTENV_CASE_TIMEOUT=${AGENTENV_CASE_TIMEOUT:-900}" \
        -v "${AUTH}":/run/secrets/opencode-auth.json:ro \
        -v "${BIN}":/usr/local/bin/opencode:ro \
        "${IMAGE}") || { echo "docker run 失败" >&2; exit 1; }

    if ! docker wait "${cid}" > "${TMP}/${mode}.exit"; then
        echo "错误: 无法等待 ${mode} 容器" >&2
        had_failure=1
    elif [ "$(cat "${TMP}/${mode}.exit")" != "0" ]; then
        had_failure=1
    fi

    docker cp "${cid}":/workspace/results/result.json "${TMP}/${mode}.json" 2>/dev/null || true
    if [ ! -s "${TMP}/${mode}.json" ]; then
        echo "  [warn] ${mode}: result.json 缺失或为空（镜像可能过期，请重新 docker build）"
        had_failure=1
    fi

    if [ -n "${SAVE_DIR}" ]; then
        mkdir -p "${SAVE_DIR}"
        docker cp "${cid}":/workspace/results/result.json "${SAVE_DIR}/${mode}.result.json" 2>/dev/null || true
        docker cp "${cid}":/workspace/results/trace.jsonl "${SAVE_DIR}/${mode}.trace.jsonl" 2>/dev/null || true
        docker cp "${cid}":/workspace/results/trace_summary.json "${SAVE_DIR}/${mode}.trace_summary.json" 2>/dev/null || true
        docker cp "${cid}":/workspace/results/opencode.log "${SAVE_DIR}/${mode}.opencode.log" 2>/dev/null || true
        docker cp "${cid}":/workspace/results/report.json "${SAVE_DIR}/${mode}.report.json" 2>/dev/null || true
    fi

    if docker rm "${cid}" >/dev/null; then
        cid=""
    else
        had_failure=1
    fi
done

python3 - "${TMP}" <<'SUM'
import json, os, sys
tmp = sys.argv[1]
keys = ["skill_mode", "exit", "success", "report_present", "checks_score",
        "skills_invoked", "skill_loaded"]
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
SUM
exit "$had_failure"
