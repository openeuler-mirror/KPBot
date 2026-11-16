#!/usr/bin/env bash
# =============================================================================
# A/B 对照运行器（依 docs/case-spec.md §5）：with/without 各跑一次，输出客观对照表。
# 环境变量处理依 docs/agent-case-security-notes.md 规则 7：
# fail-closed + realpath 白名单 + BIN 文件类型校验 + DOCKER_RUN_ARGS 白名单。
#
# 用法:
#   ./run_ab.sh [--save <目录>] [<image>]
# 环境变量:
#   OPENCODE_BIN      opencode 二进制路径（默认 /usr/local/bin/opencode）
#   OPENCODE_AUTH     auth.json 路径（默认 ~/.local/share/opencode/auth.json）
#   OPENCODE_CONFIG   opencode 配置目录（默认 ~/.config/opencode）
#   DOCKER_RUN_ARGS   追加给 docker run 的参数（默认为空；白名单:
#                     --cpus* / --memory* / --pids-limit* / --rm / --name=*
#                     / --network=bridge / --read-only / --tmpfs=*，
#                     --privileged / --pid=host / --volume / --network host
#                     等一律拒绝）
# =============================================================================
set -uo pipefail

DEFAULT_IMAGE="kpbot/app-tuner-agent-agent-arthas-trigger:1.0-openeuler24.03"
IMAGE="$DEFAULT_IMAGE"
SAVE_DIR=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --save) SAVE_DIR="${2:-}"; shift 2 ;;
        *) IMAGE="$1"; shift ;;
    esac
done

# (5) 默认值必须是 Linux 真实路径（不得为 Windows 风格 .exe）
BIN="${OPENCODE_BIN:-/usr/local/bin/opencode}"
AUTH="${OPENCODE_AUTH:-$HOME/.local/share/opencode/auth.json}"
CFG="${OPENCODE_CONFIG:-$HOME/.config/opencode}"
RUN_ARGS="${DOCKER_RUN_ARGS:-}"

# (1) fail-closed：路径不存在直接退出，而非警告后继续
for path in "${BIN}" "${AUTH}"; do
    [ -e "${path}" ] || { echo "错误: 路径不存在 ${path}" >&2; exit 1; }
done
[ -e "${CFG}" ] || { echo "错误: opencode 配置目录不存在 ${CFG}" >&2; exit 1; }

# (2) realpath 解析 + 白名单前缀（拒绝符号链接绕过与任意路径注入 docker -v）
AUTH="$(realpath -e "${AUTH}")" || exit 1
case "${AUTH}" in
    "$HOME"/.local/share/opencode/*|/etc/opencode/*)
        ;;
    *)
        echo "错误: opencode auth.json 路径不在白名单内: ${AUTH}" >&2
        exit 1
        ;;
esac

CFG="$(realpath -e "${CFG}")" || exit 1
case "${CFG}" in
    "$HOME"/.config/opencode|/etc/opencode)
        ;;
    *)
        echo "错误: opencode 配置目录不在白名单内: ${CFG}" >&2
        exit 1
        ;;
esac

BIN="$(realpath -e "${BIN}")" || exit 1
case "${BIN}" in
    /usr/local/bin/opencode|/usr/local/lib/node_modules/opencode-ai/bin/*|\
    "$HOME"/.local/bin/*|"$HOME"/.bun/*|"$HOME"/.npm-global/*|"$HOME"/.local/share/opencode/*)
        ;;
    *)
        echo "错误: opencode 二进制路径不在白名单内: ${BIN}" >&2
        exit 1
        ;;
esac

# (3) BIN 文件类型校验：ELF 魔数 fail-closed（防任意文件顶替二进制挂入容器执行）
BIN_MAGIC="$(head -c 4 "${BIN}" | od -An -tx1 | tr -d ' \n')"
if [ "${BIN_MAGIC}" != "7f454c46" ]; then
    echo "错误: ${BIN} 不是 ELF 二进制 (magic=${BIN_MAGIC})" >&2
    exit 1
fi

# (4) DOCKER_RUN_ARGS 白名单过滤：仅允许资源限制与安全开关，其余一律拒绝
RUN_ARGS_FILTERED=()
for tok in ${RUN_ARGS}; do
    case "${tok}" in
        --cpus*|--memory*|--pids-limit*|--rm|--name=*|--network=bridge|--read-only|--tmpfs=*)
            RUN_ARGS_FILTERED+=("${tok}")
            ;;
        *)
            echo "错误: DOCKER_RUN_ARGS 含不允许的参数: ${tok}" >&2
            exit 1
            ;;
    esac
done

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

for mode in with without; do
    echo "==> SKILL_MODE=${mode}"
    cid=$(docker run -d ${RUN_ARGS_FILTERED[@]+"${RUN_ARGS_FILTERED[@]}"} \
        -e SKILL_MODE=${mode} -e MODEL="${MODEL:-opencode/glm-5.3-flash-free}" \
        -v "${BIN}":/usr/local/bin/opencode:ro \
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
