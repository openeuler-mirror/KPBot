#!/usr/bin/env bash
# Kunpeng 920 Top-down / PMU 采集（分组 perf stat，避免 multiplexing）。
# 覆盖 simdjson 优化关注的访存/对齐/store/向量寄存器 stall 事件。
# 事件一律按名称引用，不内置任何平台原始事件编码；
# 平台内核未导出事件名时，可在脚本同目录放置 topdown-events.local 提供
# 「事件名=原始事件编码」映射（不入库，编码以鲲鹏 PMU 事件文档为准）。
# 用法: topdown.sh [-t SECS] [-p PID | -c "CMD"] [-a]
set -euo pipefail

SECS=10
PID=""
CMD=""
RAW=0
TS="$(date +%Y%m%d_%H%M%S)"
LOG="topdown_${TS}.log"

usage() {
  cat <<EOF
用法: $0 [选项]

  -t SECS   采样时长 (进程模式/默认 sleep 模式, 默认 10)
  -p PID    采集指定进程
  -c CMD    采集指定命令 (执行到结束)
  -a        额外打印原始 perf 行
  -h        帮助

事件组 (按事件名引用, 不可用的组自动跳过):
  cycles-ipc      cycles, instructions, branch-misses
  mem-stall       memstall_anyload, memstall_store, memstall_L1miss
  load-cancel     LD_CANCEL_MISALIGN, LD_CANCEL_FWD_HZD, LD_CANCEL_LQ_FULL, LD_CANCEL_DTLB_MISS
  store           ST_SCA_FULL, ST_ORDER_FAIL
  vec-ptag        vfp_ptag_stall, vpd_ptag_stall

环境变量:
  KP_PMU_EVENTS_FILE  事件名→原始事件编码映射文件 (默认: 脚本同目录 topdown-events.local)
EOF
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -t) SECS="$2"; shift 2;;
    -p) PID="$2"; shift 2;;
    -c) CMD="$2"; shift 2;;
    -a) RAW=1; shift;;
    -h) usage;;
    *) echo "未知参数: $1" >&2; usage;;
  esac
done

# 事件组: "组名|事件1,事件2,..."（名称引用；原始编码由可选映射文件补齐）
EVENT_GROUPS=(
  "cycles-ipc|cycles,instructions,branch-misses"
  "mem-stall|memstall_anyload,memstall_store,memstall_L1miss"
  "load-cancel|LD_CANCEL_MISALIGN,LD_CANCEL_FWD_HZD,LD_CANCEL_LQ_FULL,LD_CANCEL_DTLB_MISS"
  "store|ST_SCA_FULL,ST_ORDER_FAIL"
  "vec-ptag|vfp_ptag_stall,vpd_ptag_stall"
)
GENERIC_GROUPS=(
  "generic|cycles,instructions,branch-misses,cache-misses"
  "generic-mem|L1-dcache-loads,L1-dcache-load-misses,L1-dcache-stores"
)

EVENTS_FILE="${KP_PMU_EVENTS_FILE:-$(dirname "$(readlink -f "$0")")/topdown-events.local}"

declare -A NAME2RAW=()

load_events_file() {
  [[ -f "${EVENTS_FILE}" ]] || return 0
  local k v
  while IFS='=' read -r k v || [[ -n "${k}" ]]; do
    v="${v%$'\r'}"
    [[ -z "${k// }" || "${k}" == \#* || -z "${v// }" ]] && continue
    NAME2RAW["${k}"]="${v}"
  done < "${EVENTS_FILE}"
  echo "已加载事件映射: ${EVENTS_FILE} (${#NAME2RAW[@]} 项)" | tee -a "${LOG}"
}

# 把事件名解析为 perf 可用形式: 映射文件有名则替换为原始事件编码, 否则按名称直用
resolve_events() {
  local out=() ev
  local IFS=','
  for ev in $1; do
    if [[ -n "${NAME2RAW[${ev}]:-}" ]]; then
      out+=("${NAME2RAW[${ev}]}")
    else
      out+=("${ev}")
    fi
  done
  echo "${out[*]}"
}

probe_events() {
  perf stat -x, -e "$1" -- sleep 0.1 >/dev/null 2>&1
}

run_group() {
  local name="$1" events="$2"
  echo "" | tee -a "${LOG}"
  echo "=== group: ${name} ===" | tee -a "${LOG}"
  echo "events: ${events}" | tee -a "${LOG}"
  local out
  if [[ -n "$CMD" ]]; then
    out=$(perf stat -x, -e "${events}" bash -c "${CMD}" 2>&1) || true
  elif [[ -n "$PID" ]]; then
    out=$(perf stat -x, -e "${events}" -p "${PID}" -- sleep "${SECS}" 2>&1) || true
  else
    out=$(perf stat -x, -e "${events}" -- sleep "${SECS}" 2>&1) || true
  fi
  if [[ $RAW -eq 1 ]]; then
    echo "$out" | tee -a "${LOG}"
  else
    echo "$out" >> "${LOG}"
  fi
}

echo "=== Top-down 采集 (log: ${LOG}) ===" | tee "${LOG}"
load_events_file

SELECTED=()
RAWMAP=""
for g in "${EVENT_GROUPS[@]}"; do
  gname="${g%%|*}"
  gev="${g#*|}"
  resolved="$(resolve_events "${gev}")"
  if probe_events "${resolved}"; then
    SELECTED+=("${gname}|${resolved}")
    if [[ "${resolved}" != "${gev}" ]]; then
      map_pairs=""
      IFS=',' read -ra _names <<< "${gev}"
      IFS=',' read -ra _codes <<< "${resolved}"
      for i in "${!_names[@]}"; do
        [[ "${_codes[$i]}" != "${_names[$i]}" ]] && map_pairs+="${map_pairs:+,}${_codes[$i]}=${_names[$i]}"
      done
      RAWMAP="${RAWMAP:+${RAWMAP},}${map_pairs}"
    fi
  else
    echo "警告: 事件组 ${gname} 在当前平台不可用 (${gev})，已跳过。" | tee -a "${LOG}"
  fi
done

if [[ ${#SELECTED[@]} -eq 0 ]]; then
  if probe_events "cycles,instructions"; then
    echo "警告: 当前平台不支持上述专用事件，降级为通用事件组。" | tee -a "${LOG}"
    for g in "${GENERIC_GROUPS[@]}"; do
      SELECTED+=("${g%%|*}|${g#*|}")
    done
  else
    echo "错误: perf 在当前环境不可用 (无法采集 cycles/instructions)，请检查 perf_event_paranoid/权限。" | tee -a "${LOG}" >&2
    exit 1
  fi
fi

for g in "${SELECTED[@]}"; do
  run_group "${g%%|*}" "${g#*|}"
done

echo "" | tee -a "${LOG}"
echo "=== 关键事件汇总 ===" | tee -a "${LOG}"
RAWMAP="${RAWMAP}" awk -F, '
  BEGIN {
    if (ENVIRON["RAWMAP"] != "") {
      n = split(ENVIRON["RAWMAP"], a, ",")
      for (i = 1; i <= n; i++) { split(a[i], p, "="); m[p[1]] = p[2] }
    }
  }
  NF>=3 && $1 ~ /^[0-9]+$/ {
    ev = $3
    if (ev in m) ev = m[ev]
    v[ev] += $1
  }
  END{
    cyc = v["cycles"]; inst = v["instructions"];
    printf "cycles                  : %d\n", cyc;
    printf "instructions            : %d\n", inst;
    if (cyc>0) printf "IPC                     : %.3f\n", inst/cyc;
    key["LD_CANCEL_MISALIGN"]="LD_CANCEL_MISALIGN (未对齐 load)";
    key["LD_CANCEL_FWD_HZD"]="LD_CANCEL_FWD_HZD  (store->load hazard)";
    key["LD_CANCEL_LQ_FULL"]="LD_CANCEL_LQ_FULL  (load queue 满)";
    key["LD_CANCEL_DTLB_MISS"]="LD_CANCEL_DTLB_MISS";
    key["ST_ORDER_FAIL"]="ST_ORDER_FAIL      (store 顺序失败)";
    key["ST_SCA_FULL"]="ST_SCA_FULL";
    key["memstall_anyload"]="memstall_anyload";
    key["memstall_store"]="memstall_store";
    key["memstall_L1miss"]="memstall_L1miss";
    key["vfp_ptag_stall"]="vfp_ptag_stall     (向量寄存器)";
    key["vpd_ptag_stall"]="vpd_ptag_stall";
    for (k in key) if (k in v) printf "%-24s: %d\n", key[k], v[k];
  }' "${LOG}" | tee -a "${LOG}"

echo "" | tee -a "${LOG}"
echo "提示: 判读见 references/measurement-notes.md；单次采集只作方向，A/B 需同参数多轮。" | tee -a "${LOG}"
