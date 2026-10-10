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
#                    —— 整文件 base64 经 OPENCODE_AUTH_JSON env 注入容器，不做卷挂载
#   DOCKER_RUN_ARGS  追加给 docker run 的参数（默认为空；白名单:
#                    --cpus* / --memory* / --pids-limit* / --rm / --name=*
#                    / --network=bridge，其余一律拒绝）
#   OPENCODE_SHA256  可选：opencode 二进制 sha256 指纹；设置即强制比对（fail-closed），
#                    未设置则跳过。指纹经部署侧配置注入（CI env/部署清单）
# =============================================================================
set -uo pipefail

DEFAULT_IMAGE="kpbot/code-optimizer-agent-fused-positivity:1.0-openeuler24.03"
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

# fail-closed：opencode 二进制必须存在
[ -e "${BIN}" ] || { echo "错误: opencode 二进制不存在 ${BIN}" >&2; exit 1; }
# fail-closed：auth.json 必须存在（后续整文件 base64 注入，不挂载）
[ -e "${AUTH}" ] || { echo "错误: opencode auth.json 不存在 ${AUTH}" >&2; exit 1; }

# AUTH 源路径 realpath + 白名单（防任意文件读取注入容器）
AUTH_REAL="$(realpath "${AUTH}")"
case "${AUTH_REAL}" in
    $HOME/.local/share/opencode/*|/etc/opencode/*)
        ;;
    *)
        echo "错误: opencode auth.json 路径不在白名单内: ${AUTH_REAL}" >&2
        exit 1
        ;;
esac
AUTH="${AUTH_REAL}"

# BIN realpath + 白名单前缀校验（防任意路径注入）
BIN_REAL="$(realpath "${BIN}")"
case "${BIN_REAL}" in
    /usr/local/bin/opencode|/usr/local/lib/node_modules/opencode-ai/bin/*|\
$HOME/.local/bin/*|$HOME/.npm-global/*|$HOME/.local/share/opencode/*)
        ;;
    *)
        echo "错误: opencode 二进制路径不在白名单内: ${BIN_REAL}" >&2
        exit 1
        ;;
esac
BIN="${BIN_REAL}"

# BIN 完整性：ELF 魔数 fail-closed（防文本/脚本/错误格式顶替；od 属 coreutils 无额外依赖）
BIN_MAGIC="$(head -c 4 "${BIN}" | od -An -tx1 | tr -d ' \n')"
if [ "${BIN_MAGIC}" != "7f454c46" ]; then
    echo "错误: ${BIN} 不是 ELF 二进制 (magic=${BIN_MAGIC})" >&2
    exit 1
fi

# BIN sha256 指纹比对（可选强化）：OPENCODE_SHA256 预置即强制校验，未设置则跳过。
# 信任锚在部署侧配置（CI env/部署清单），仓库不内置指纹（版本×架构矩阵维护成本）。
if [ -n "${OPENCODE_SHA256:-}" ]; then
    BIN_HASH="$(sha256sum "${BIN}" | awk '{print $1}')"
    if [ "${BIN_HASH}" != "${OPENCODE_SHA256}" ]; then
        echo "错误: opencode sha256 不匹配: got=${BIN_HASH} expect=${OPENCODE_SHA256}" >&2
        exit 1
    fi
fi

# RUN_ARGS 白名单过滤：仅允许资源限制与安全开关
RUN_ARGS_FILTERED=()
for tok in ${RUN_ARGS}; do
    case "${tok}" in
        --cpus*|--memory*|--pids-limit*|--rm|--name=*|--network=bridge)
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

# 凭证整文件 base64 经 env 注入（避免多行 JSON 破坏 docker -e，且不做卷挂载）
AUTH_JSON_B64="$(base64 -w0 "${AUTH}")"

for mode in with without; do
    echo "==> SKILL_MODE=${mode}"
    cid=$(docker run -d ${RUN_ARGS_FILTERED[@]+"${RUN_ARGS_FILTERED[@]}"} \
        -e SKILL_MODE=${mode} \
        -e "OPENCODE_AUTH_JSON=${AUTH_JSON_B64}" \
        -v "${BIN}":/usr/local/bin/opencode:ro \
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
        docker cp "${cid}":/workspace/opt/kernel_opt.rs "${SAVE_DIR}/${mode}.kernel_opt.rs" 2>/dev/null || true
    fi

    docker rm "${cid}" >/dev/null
done

unset AUTH_JSON_B64

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
