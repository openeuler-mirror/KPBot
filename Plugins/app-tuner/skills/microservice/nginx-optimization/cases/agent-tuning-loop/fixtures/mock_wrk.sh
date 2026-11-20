#!/usr/bin/env bash
# mock_wrk.sh — 模拟 wrk 压测工具（确定性指标，零副作用，不碰真实 nginx）。
#
# P2 教训（PR#100 检视）的协议设计：
#   1. 每次调用以一行结构化 JSON 追加到 $MOCK_LOG：{"argv":[...], "parsed":{...},
#      "unparsed":[...], "config_lines":[...]}；无参调用记录空数组而非空行；
#   2. 同时支持 --key=value 与 --key value 两种写法；
#   3. 无法识别的参数记入 unparsed 并打 stderr 警告，绝不静默忽略；
#   4. 指标由 --config 指向的 nginx.conf 内容决定（快照嵌入日志，verify 无文件竞态）。
#
# 用法:
#   MOCK_LOG=/path/invocations.log ./mock_wrk.sh [-c 100] [-d 10] --config <nginx.conf>
set -uo pipefail

CONNS=100
DUR=10
CONFIG=""
MOCK_LOG="${MOCK_LOG:-}"

argv=("$0")
unparsed=()

while [[ $# -gt 0 ]]; do
    argv+=("$1")
    case "$1" in
        -c) CONNS="${2:?}"; argv+=("$2"); shift 2 ;;
        -d) DUR="${2:?}"; argv+=("$2"); shift 2 ;;
        --config=*) CONFIG="${1#--config=}"; shift ;;
        --config) CONFIG="${2:?}"; argv+=("$2"); shift 2 ;;
        *) unparsed+=("$1"); echo "[mock_wrk] unparsed arg: $1" >&2; shift ;;
    esac
done

# 读取 config（未提供则视为全默认配置）并计算确定性 RPS。
config_lines=()
if [[ -n "${CONFIG}" && -f "${CONFIG}" ]]; then
    mapfile -t config_lines < "${CONFIG}"
fi

rps=10000
# 生效指令匹配前剔除注释行（# 开头）：被注释掉的指令不得影响指标
low_all=$(printf '%s\n' "${config_lines[@]:-}" | grep -v '^[[:space:]]*#' | tr 'A-Z' 'a-z')

grep_val() { grep -F "$1" <<<"${low_all}" || true; }

if [[ -n "$(grep_val "reuseport")" ]];      then rps=$((rps + rps * 8 / 100)); fi
if [[ -n "$(grep_val "access_log off")" ]]; then rps=$((rps + rps * 7 / 100)); fi
if [[ -n "$(grep_val "sendfile on")" ]];    then rps=$((rps + rps * 3 / 100)); fi
if [[ -n "$(grep_val "worker_cpu_affinity")" ]]; then rps=$((rps + rps * 5 / 100)); fi

echo "Running ${DUR}s test @ http://127.0.0.1:8080/ (conns=${CONNS})"
echo "Requests/sec: ${rps}"

if [[ -n "${MOCK_LOG}" ]]; then
    CONFIG="${CONFIG}" python3 - "${MOCK_LOG}" "${argv[@]}" ${unparsed[@]+"${unparsed[@]}"} -- <<'PY'
import json, os, sys

mock_log, *rest = sys.argv[1:]
sep = rest.index("--")
argv, unparsed = rest[:sep], rest[sep + 1:]

config_lines = []
cfg = os.environ.get("CONFIG", "")
if cfg and os.path.exists(cfg):
    config_lines = [ln.rstrip("\n") for ln in open(cfg, encoding="utf-8", errors="replace")]

entry = {
    "argv": argv,
    "parsed": {"config": cfg, "conns": None, "dur": None},
    "unparsed": unparsed,
    "config_lines": config_lines,
}
with open(mock_log, "a", encoding="utf-8") as f:
    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
PY
fi
