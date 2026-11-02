#!/usr/bin/env bash
# =============================================================================
# A/B 对照运行器（本用例自带）
# 以 SKILL_MODE=with / without 各跑一次，收集 result.json，输出客观对照表。
# 不做统计/阈值加工，只展示原始结果。
#
# 用法:
#   ./run_ab.sh [--save <目录>] [<image>]
# 环境变量:
#   OPENCODE_BIN     opencode 二进制路径（默认 /usr/local/bin/opencode），只读挂载
#   OPENCODE_AUTH    auth.json 路径（默认 ~/.local/share/opencode/auth.json）
#                    —— 整文件 base64 经 OPENCODE_AUTH_JSON env 注入容器，不做卷挂载
#   DOCKER_RUN_ARGS  追加给 docker run 的参数（默认为空；白名单:
#                    --cpus* / --memory* / --pids-limit* / --rm / --name=*
#                    / --network=bridge，其余一律拒绝）
# =============================================================================
set -uo pipefail

DEFAULT_IMAGE="kpbot/code-optimizer-agent-conformance:1.0-openeuler24.03"
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

# fail-closed：opencode 二进制与 auth.json 必须存在（后续整文件 base64 注入，不挂载）
[ -e "${BIN}" ]  || { echo "错误: opencode 二进制不存在 ${BIN}" >&2; exit 1; }
[ -e "${AUTH}" ] || { echo "错误: opencode auth.json 不存在 ${AUTH}" >&2; exit 1; }

# RUN_ARGS 白名单过滤：仅允许资源限制与安全开关（默认不带 --network host）
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
        docker cp "${cid}":/workspace/results/fixture_check.out "${SAVE_DIR}/${mode}.fixture_check.out" 2>/dev/null || true
        docker cp "${cid}":/workspace/opt/conformance "${SAVE_DIR}/${mode}.conformance" 2>/dev/null || true
    fi

    docker rm "${cid}" >/dev/null
done

# --save 回拷产物做凭证剔除：避免凭证经日志/结果落盘到宿主（更不可进仓库）
if [ -n "${SAVE_DIR}" ]; then
    python3 - "${SAVE_DIR}" "${AUTH_JSON_B64:-}" <<'PY'
import os, re, sys
save = sys.argv[1]
blob = sys.argv[2] if len(sys.argv) > 2 else ""
pat = re.compile(r'("(?:access_token|api[_-]?key|secret|token|password|key)"\s*:\s*")[^"]*(")', re.I)
skey = re.compile(r'sk-[A-Za-z0-9_\-]{8,}')
for name in sorted(os.listdir(save)):
    p = os.path.join(save, name)
    if not os.path.isfile(p):
        continue
    try:
        s = open(p, encoding="utf-8", errors="ignore").read()
    except Exception:
        continue
    t = s
    if blob:
        t = t.replace(blob, "***REDACTED***")
    t = pat.sub(r'\1***REDACTED***\2', t)
    t = skey.sub("***REDACTED***", t)
    if t != s:
        open(p, "w", encoding="utf-8").write(t)
        print("[redact] %s: 已剔除凭证串" % name)
PY
fi

unset AUTH_JSON_B64

python3 - "${TMP}" <<'PY'
import json, os, sys
tmp = sys.argv[1]
keys = ["skill_mode", "exit", "success", "fixture_ok", "framework_present",
        "clause_bound", "run_rc", "conformance_ready", "skills_invoked"]
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
