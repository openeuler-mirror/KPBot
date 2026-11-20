#!/usr/bin/env bash
# A/B 对照运行器（本用例自带）。以 SKILL_MODE=with / without 各跑一次，输出客观对照表。
#
# 用法:
#   ./run_ab.sh [--save <目录>] [<image>]
# 环境变量:
#   OPENCODE_BIN     opencode 二进制路径
#   OPENCODE_AUTH    auth.json 路径
#   DOCKER_RUN_ARGS  追加给 docker run 的参数（默认 --network host）
set -uo pipefail

DEFAULT_IMAGE="kpbot/app-tuner-agent-nginx-tuning-loop:1.0-openeuler24.03"
IMAGE="$DEFAULT_IMAGE"
SAVE_DIR=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --save) SAVE_DIR="${2:?}"; shift 2 ;;
        *) IMAGE="$1"; shift ;;
    esac
done

BIN="${OPENCODE_BIN:-/usr/local/lib/node_modules/opencode-ai/bin/opencode.exe}"
AUTH="${OPENCODE_AUTH:-$HOME/.local/share/opencode/auth.json}"
RUN_ARGS="${DOCKER_RUN_ARGS:---network host}"

for path in "${BIN}" "${AUTH}"; do
    [ -e "${path}" ] || echo "警告: 路径不存在 ${path}" >&2
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
    docker cp "${cid}":/workspace/results/report.md "${TMP}/${mode}.report.md" 2>/dev/null || true
    docker cp "${cid}":/workspace/results/trace_summary.json "${TMP}/${mode}.trace.json" 2>/dev/null || true
    docker rm -f "${cid}" >/dev/null 2>&1 || true
done

python3 - "$TMP" <<'PY'
import json, sys, os
tmp = sys.argv[1]
def load(p, default=None):
    fp = os.path.join(tmp, p)
    if os.path.exists(fp) and os.path.getsize(fp) > 0:
        try:
            return json.load(open(fp))
        except Exception:
            return default
    return default

rows = []
for mode in ("with", "without"):
    data = load(f"{mode}.json", {}) or {}
    trace = load(f"{mode}.trace.json", {}) or {}
    rows.append({
        "mode": mode,
        "exit": open(os.path.join(tmp, f"{mode}.exit")).read().strip() if os.path.exists(os.path.join(tmp, f"{mode}.exit")) else "NA",
        "success": data.get("success"),
        "passed": data.get("passed"),
        "total": data.get("total"),
        "skills_invoked": trace.get("skills_invoked", []),
    })

def rate(r):
    t = r.get("total") or 0
    return (r.get("passed") or 0) / t if t else 0.0

with_rate, without_rate = rate(rows[0]), rate(rows[1])
summary = {
    "rows": rows,
    "pass_rate_with": round(with_rate, 4),
    "pass_rate_without": round(without_rate, 4),
    "uplift": round(with_rate - without_rate, 4) if (with_rate or without_rate) else None,
}
print(json.dumps(summary, ensure_ascii=False, indent=2))
out = os.path.join(tmp, "summary.json")
json.dump(summary, open(out, "w"), ensure_ascii=False, indent=2)
PY

if [ -n "${SAVE_DIR}" ]; then
    mkdir -p "${SAVE_DIR}"
    cp "${TMP}"/*.json "${SAVE_DIR}/" 2>/dev/null || true
    cp "${TMP}"/*.report.md "${SAVE_DIR}/" 2>/dev/null || true
    echo "结果已保存到 ${SAVE_DIR}"
fi
