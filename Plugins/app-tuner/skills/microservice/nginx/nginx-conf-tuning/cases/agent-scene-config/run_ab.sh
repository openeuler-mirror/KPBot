#!/usr/bin/env bash
# =============================================================================
# A/B 对照运行器（本用例自带）。以 SKILL_MODE=with / without 各跑一次，输出客观对照表。
#
# 用法:
#   ./run_ab.sh [--save <目录>] [<image>]
#
# 环境变量:
#   OPENCODE_BIN     opencode 二进制路径（默认 /usr/local/bin/opencode），只读挂载
#   OPENCODE_AUTH    auth.json 路径（默认 ~/.local/share/opencode/auth.json）
#                    —— 整文件 base64 经 OPENCODE_AUTH_JSON env 注入容器，不做卷挂载
#   DOCKER_RUN_ARGS  追加给 docker run 的参数（默认 --network=bridge）。白名单:
#                    --cpus* / --memory* / --pids-limit* / --name=*
#                    / --network=bridge / --network=host；其余一律拒绝。
#                    确需 host 网络时显式传 --network=host。
#   OPENCODE_SHA256  可选：opencode 二进制 sha256 指纹；设置即强制比对（fail-closed）
#
# 安全加固（docs/agent-case-security-notes.md §1/§7）：
#   - 宿主路径 fail-closed + realpath 白名单 + ELF 校验，不做「警告后继续」；
#   - DOCKER_RUN_ARGS 逐 token 白名单，拒绝 --privileged / --pid=host / -v 等；
#   - 任一臂 result.json 缺失/为空视为证据不完整，非 0 退出，不按 0 分参与 uplift。
# =============================================================================
set -uo pipefail

DEFAULT_IMAGE="kpbot/app-tuner-agent-nginx-conf-scene-config:1.0-openeuler24.03"
DEFAULT_RUN_ARGS="--network=bridge"
IMAGE="$DEFAULT_IMAGE"
SAVE_DIR=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --save) SAVE_DIR="${2:?}"; shift 2 ;;
        *) IMAGE="$1"; shift ;;
    esac
done

BIN="${OPENCODE_BIN:-/usr/local/bin/opencode}"
AUTH="${OPENCODE_AUTH:-$HOME/.local/share/opencode/auth.json}"
RUN_ARGS="${DOCKER_RUN_ARGS:-${DEFAULT_RUN_ARGS}}"

# ---- 宿主路径 fail-closed + realpath 白名单 + ELF 校验 ----
[ -e "${BIN}" ]  || { echo "错误: opencode 二进制不存在: ${BIN}" >&2; exit 1; }
[ -e "${AUTH}" ] || { echo "错误: opencode auth.json 不存在: ${AUTH}" >&2; exit 1; }

BIN_REAL="$(realpath -e "${BIN}" 2>/dev/null || realpath "${BIN}")" || exit 1
AUTH_REAL="$(realpath -e "${AUTH}" 2>/dev/null || realpath "${AUTH}")" || exit 1

case "${BIN_REAL}" in
    /usr/local/bin/opencode|/usr/local/lib/node_modules/opencode-ai/bin/*|\
    "$HOME"/.local/bin/*|"$HOME"/.npm-global/*|"$HOME"/.local/share/opencode/*) ;;
    *) echo "错误: opencode 二进制路径不在白名单内: ${BIN_REAL}" >&2; exit 1 ;;
esac
case "${AUTH_REAL}" in
    "$HOME"/.local/share/opencode/*|/etc/opencode/*) ;;
    *) echo "错误: opencode auth.json 路径不在白名单内: ${AUTH_REAL}" >&2; exit 1 ;;
esac

BIN_MAGIC="$(head -c 4 "${BIN_REAL}" | od -An -tx1 | tr -d ' \n')"
[ "${BIN_MAGIC}" = "7f454c46" ] || { echo "错误: opencode 不是 ELF 二进制: ${BIN_REAL}" >&2; exit 1; }
if [ -n "${OPENCODE_SHA256:-}" ]; then
    BIN_HASH="$(sha256sum "${BIN_REAL}" | awk '{print $1}')"
    [ "${BIN_HASH}" = "${OPENCODE_SHA256}" ] || {
        echo "错误: opencode sha256 不匹配: got=${BIN_HASH} expect=${OPENCODE_SHA256}" >&2
        exit 1
    }
fi

# ---- DOCKER_RUN_ARGS 白名单 ----
RUN_ARGS_FILTERED=()
for tok in ${RUN_ARGS}; do
    case "${tok}" in
        --cpus*|--memory*|--pids-limit*|--name=*|--network=bridge|--network=host)
            RUN_ARGS_FILTERED+=("${tok}") ;;
        *)
            echo "错误: DOCKER_RUN_ARGS 含不允许的参数: ${tok}" >&2
            exit 1 ;;
    esac
done
for tok in ${DEFAULT_RUN_ARGS}; do
    case "${tok}" in
        --cpus*|--memory*|--pids-limit*|--name=*|--network=bridge|--network=host) ;;
        *) echo "错误: RUN_ARGS 默认值未通过白名单: ${tok}" >&2; exit 1 ;;
    esac
done

# 凭证整文件 base64（避免多行 JSON 破坏 docker -e），不做 -v 挂载
AUTH_JSON_B64="$(base64 < "${AUTH_REAL}" | tr -d '\n')"
[ -n "${AUTH_JSON_B64}" ] || { echo "错误: 无法读取 auth.json" >&2; exit 1; }

TMP="$(mktemp -d)"
cid=""
cleanup() {
    [ -n "${cid}" ] && docker rm -f "${cid}" >/dev/null 2>&1 || true
    rm -rf "${TMP}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
had_failure=0

for mode in with without; do
    echo "==> SKILL_MODE=${mode}"
    cid=$(docker run -d ${RUN_ARGS_FILTERED[@]+"${RUN_ARGS_FILTERED[@]}"} \
        -e "SKILL_MODE=${mode}" \
        -e "MODEL=${MODEL:-deepseek/deepseek-v4-flash}" \
        -e "OPENCODE_AUTH_JSON=${AUTH_JSON_B64}" \
        -v "${BIN_REAL}":/usr/local/bin/opencode:ro \
        "${IMAGE}") || { echo "docker run 失败" >&2; exit 1; }

    if ! docker wait "${cid}" > "${TMP}/${mode}.exit"; then
        echo "错误: 无法等待 ${mode} 容器" >&2
        had_failure=1
    elif [ "$(cat "${TMP}/${mode}.exit")" != "0" ]; then
        # without 臂不满足断言是正常对照结果；证据缺失才阻断（见下方 result.json 检查）。
        echo "  [info] ${mode}: 容器退出码 $(cat "${TMP}/${mode}.exit")" >&2
    fi

    docker cp "${cid}":/workspace/results/result.json "${TMP}/${mode}.json" 2>/dev/null || true
    if [ ! -s "${TMP}/${mode}.json" ]; then
        echo "  [error] ${mode}: result.json 缺失或为空（镜像过期或容器提前退出）" >&2
        docker cp "${cid}":/workspace/results/opencode.log "${TMP}/${mode}.opencode.log" 2>/dev/null || true
        tail -n 12 "${TMP}/${mode}.opencode.log" 2>/dev/null | sed 's/^/    | /' >&2
        had_failure=1
    fi
    docker cp "${cid}":/workspace/work/decisions.md "${TMP}/${mode}.decisions.md" 2>/dev/null || true
    docker cp "${cid}":/workspace/results/trace_summary.json "${TMP}/${mode}.trace.json" 2>/dev/null || true
    docker cp "${cid}":/workspace/results/trace.jsonl "${TMP}/${mode}.trace.jsonl" 2>/dev/null || true
    docker cp "${cid}":/workspace/results/opencode.log "${TMP}/${mode}.opencode.log" 2>/dev/null || true

    if docker rm "${cid}" >/dev/null 2>&1; then
        cid=""
    else
        had_failure=1
    fi
done

# ---- 汇总：证据缺失即失败；uplift 与 case.yaml 统一为差值 ----
python3 - "${TMP}" <<'PY' || had_failure=1
import json, os, sys
tmp = sys.argv[1]

def load(name):
    path = os.path.join(tmp, name)
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return None
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return None

rows = []
evidence_complete = True
invalid = False
for mode in ("with", "without"):
    data = load(mode + ".json")
    exit_path = os.path.join(tmp, mode + ".exit")
    exit_code = open(exit_path).read().strip() if os.path.exists(exit_path) else "NA"
    if not isinstance(data, dict):
        evidence_complete = False
        data = {}
    total = data.get("total")
    if not isinstance(total, int) or total <= 0:
        evidence_complete = False
    skills = data.get("skills_invoked")
    if not isinstance(skills, list):
        trace = load(mode + ".trace.json") or {}
        skills = trace.get("skills_invoked", []) if isinstance(trace, dict) else []
    if mode == "with" and "nginx-conf-tuning" not in skills:
        invalid = True
    if mode == "without" and skills:
        invalid = True
    row = {k: data.get(k) for k in ("success", "passed", "total", "one_variable", "checks", "reason")}
    row.update({
        "mode": mode,
        "exit": exit_code,
        "skills_invoked": skills,
        "evidence_complete": bool(isinstance(total, int) and total > 0),
    })
    rows.append(row)

def rate(row):
    total = row.get("total") or 0
    return ((row.get("passed") or 0) / total) if total else None

with_rate, without_rate = rate(rows[0]), rate(rows[1])
uplift = None
if evidence_complete and not invalid and with_rate is not None and without_rate is not None:
    uplift = round(with_rate - without_rate, 4)
summary = {
    "rows": rows,
    "pass_rate_with": round(with_rate, 4) if with_rate is not None else None,
    "pass_rate_without": round(without_rate, 4) if without_rate is not None else None,
    "uplift": uplift,
    "uplift_metric": "pass_rate_with - pass_rate_without",
    "evidence_complete": evidence_complete,
    "invalid": invalid,
}
print(json.dumps(summary, ensure_ascii=False, indent=2))
json.dump(summary, open(os.path.join(tmp, "summary.json"), "w"), ensure_ascii=False, indent=2)
if not evidence_complete or invalid:
    sys.exit(2)
PY

if [ -n "${SAVE_DIR}" ]; then
    mkdir -p "${SAVE_DIR}"
    cp "${TMP}"/*.json "${SAVE_DIR}/" 2>/dev/null || true
    cp "${TMP}"/*.decisions.md "${SAVE_DIR}/" 2>/dev/null || true
    cp "${TMP}"/*.trace.jsonl "${SAVE_DIR}/" 2>/dev/null || true
    cp "${TMP}"/*.opencode.log "${SAVE_DIR}/" 2>/dev/null || true
    echo "结果已保存到 ${SAVE_DIR}"
fi

if [ "${had_failure}" -ne 0 ]; then
    echo "RESULT: FAILED (see above)" >&2
    exit 1
fi
exit 0
