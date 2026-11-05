#!/usr/bin/env bash
# =============================================================================
# A/B 对照运行器（本用例自带）
# 以 SKILL_MODE=with / without 各跑一次，收集 result.json，输出客观对照表。
# 不做统计/阈值加工，只展示原始结果。
#
# 用法:
#   ./run_ab.sh [--save <目录>] [<image>]
# 环境变量:
#   OPENCODE_BIN      opencode 二进制路径（默认宿主机上的 /usr/local/lib/node_modules/opencode-ai/bin/opencode.exe）
#   OPENCODE_AUTH     auth.json 路径（默认 ~/.local/share/opencode/auth.json）
#   DOCKER_RUN_ARGS   追加给 docker run 的参数（默认 --network bridge，见意见10；docker.sock 固定挂载）
#   TARGET_CONTAINER  目标 vllm 容器名（缺省进容器内按进程自探）
#   EXPECTED_FAILED_CHECKS / EXPECTED_VLLM_ASCEND_OK —— 地面真值覆盖（见 case.yaml）
# 建议宿主机预置故障（如进入目标容器 unset PYTHONMALLOC 重启 vllm）后运行，
# 否则真值为空集时 A/B 无区分度。
# =============================================================================
set -uo pipefail

DEFAULT_IMAGE="kpbot/app-tuner-agent-stage1:1.0-openeuler24.03"
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
# 意见10：默认不再共享宿主网络命名空间（--network host 下容器内任意代码可直
# 连宿主并外发敏感文件）。默认 bridge（NAT 出网，模型 API 可达）；需要宿主
# 网络时显式传 DOCKER_RUN_ARGS='--network host' 并自担风险。
RUN_ARGS="${DOCKER_RUN_ARGS:---network bridge}"

for path in "${BIN}" "${AUTH}" /var/run/docker.sock; do
    if [ ! -e "${path}" ]; then
        echo "警告: 路径不存在 ${path}" >&2
    fi
done

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

for mode in with without; do
    echo "==> SKILL_MODE=${mode}"
    # 意见3.0-2：宿主资产挂载交代——BIN 与 AUTH 均只读（BIN 仅需执行权限；
    # :ro 由挂载层保证、与容器内 root/CAP_DAC_OVERRIDE 无关，杜绝容器内代码
    # 覆盖宿主 opencode 二进制形成宿主持久化通道）。docker.sock 维持 rw 挂载：
    # socket 为双向通道，加 :ro 并无实际约束力，属既有独立 finding
    # security-docker-socket-mounted-into-auto-approved-agent，残余风险见
    # case.yaml 意见10 治理说明
    cid=$(docker run -d ${RUN_ARGS} \
        -e SKILL_MODE=${mode} \
        $( [ -n "${TARGET_CONTAINER:-}" ] && echo "-e TARGET_CONTAINER=${TARGET_CONTAINER}" ) \
        $( [ -n "${EXPECTED_FAILED_CHECKS:-}" ] && echo "-e EXPECTED_FAILED_CHECKS=${EXPECTED_FAILED_CHECKS}" ) \
        $( [ -n "${EXPECTED_VLLM_ASCEND_OK:-}" ] && echo "-e EXPECTED_VLLM_ASCEND_OK=${EXPECTED_VLLM_ASCEND_OK}" ) \
        -v "${BIN}":/usr/local/bin/opencode:ro \
        -v "${AUTH}":/root/.local/share/opencode/auth.json:ro \
        -v /var/run/docker.sock:/var/run/docker.sock \
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
        docker cp "${cid}":/workspace/results/result.json           "${SAVE_DIR}/${mode}.result.json" 2>/dev/null || true
        docker cp "${cid}":/workspace/results/trace.jsonl           "${SAVE_DIR}/${mode}.trace.jsonl" 2>/dev/null || true
        docker cp "${cid}":/workspace/results/trace_summary.json    "${SAVE_DIR}/${mode}.trace_summary.json" 2>/dev/null || true
        docker cp "${cid}":/workspace/results/opencode.log          "${SAVE_DIR}/${mode}.opencode.log" 2>/dev/null || true
        docker cp "${cid}":/workspace/opt/diagnosis.json            "${SAVE_DIR}/${mode}.diagnosis.json" 2>/dev/null || true
        docker cp "${cid}":/workspace/prompt.final.md               "${SAVE_DIR}/${mode}.prompt.md" 2>/dev/null || true
    fi

    docker rm "${cid}" >/dev/null
done

python3 - "${TMP}" <<'PY'
import json, os, sys
tmp = sys.argv[1]
keys = ["skill_mode", "exit", "success", "diagnosis_correct",
        "expected_failed_checks", "reported_failed_checks", "skills_invoked"]
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
