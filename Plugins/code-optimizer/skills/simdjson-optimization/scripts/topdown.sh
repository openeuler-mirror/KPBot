#!/usr/bin/env bash
# Kunpeng 920 Top-down / PMU 采集（分组 perf stat，避免 multiplexing）。
# 覆盖 simdjson 优化关注的访存/对齐/store/向量寄存器 stall 事件。
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

事件组:
  cycles/ipc      CPU_CYCLES, INST_RETIRED, BR_MIS_PRED, INST_SPEC
  mem-stall       memstall_anyload/store, memstall_L1miss, IF_FETCH_BUBBLE
  load-cancel     LD_CANCEL_MISALIGN, LD_CANCEL_FWD_HZD, LD_CANCEL_LQ_FULL, LD_CANCEL_DTLB_MISS
  store           ST_SCA_FULL, ST_SCD_FULL, ST_ORDER_FAIL
  vec-ptag        Int_ptag_stall, cc_ptag_stall, vfp_ptag_stall, vpd_ptag_stall
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

# 事件组: "组名|事件1,事件2,..."
EVENT_GROUPS=(
  "cycles-ipc|r0011,r0008,r0010,r001b"
  "mem-stall|r7005,r7006,r7007,r1f21"
  "load-cancel|r5091,r5094,r5092,r5090"
  "store|r50a0,r50a1,r50a3,r7000"
  "vec-ptag|r2006,r2007,r2008,r200a"
)

# 探测原始事件是否可用
if ! perf stat -x, -e r0011 -- sleep 0.1 >/dev/null 2>&1; then
  echo "警告: 当前平台不支持 Kunpeng rXXXX 原始事件，降级为通用事件。" | tee -a "${LOG}"
  EVENT_GROUPS=(
    "generic|cycles,instructions,branch-misses,cache-misses"
    "generic-mem|L1-dcache-loads,L1-dcache-load-misses,L1-dcache-stores,stalled-cycles-backend"
  )
fi

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
for g in "${EVENT_GROUPS[@]}"; do
  run_group "${g%%|*}" "${g#*|}"
done

echo "" | tee -a "${LOG}"
echo "=== 关键事件汇总 ===" | tee -a "${LOG}"
awk -F, '
  NF>=3 && $1 ~ /^[0-9]+$/ { v[$3]+=$1 }
  END{
    cyc = (("r0011" in v) ? v["r0011"] : v["cycles"]);
    inst = (("r0008" in v) ? v["r0008"] : v["instructions"]);
    printf "CPU_CYCLES/cycles        : %d\n", cyc;
    printf "INST_RETIRED/instructions: %d\n", inst;
    if (cyc>0) printf "IPC                      : %.3f\n", inst/cyc;
    key["r5091"]="LD_CANCEL_MISALIGN (未对齐 load)";
    key["r5094"]="LD_CANCEL_FWD_HZD  (store->load hazard)";
    key["r5092"]="LD_CANCEL_LQ_FULL  (load queue 满)";
    key["r5090"]="LD_CANCEL_DTLB_MISS";
    key["r50a3"]="ST_ORDER_FAIL      (store 顺序失败)";
    key["r50a0"]="ST_SCA_FULL";
    key["r7005"]="memstall_anyload";
    key["r7006"]="memstall_store";
    key["r7007"]="memstall_L1miss";
    key["r2008"]="vfp_ptag_stall     (向量寄存器)";
    key["r200a"]="vpd_ptag_stall";
    for (k in key) if (k in v) printf "%-26s: %d\n", key[k], v[k];
  }' "${LOG}" | tee -a "${LOG}"

echo "" | tee -a "${LOG}"
echo "提示: 判读见 references/measurement-notes.md；单次采集只作方向，A/B 需同参数多轮。" | tee -a "${LOG}"
