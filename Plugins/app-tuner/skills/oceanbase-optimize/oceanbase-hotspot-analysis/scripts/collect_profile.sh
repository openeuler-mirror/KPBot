#!/usr/bin/env bash

# OceanBase 只读热点采集。默认 dry-run，仅打印计划；--execute 才真正采集。

#

# 用法：

# OBS_PID=<pid> SAMPLE_SECS=30 bash collect_profile.sh --out ./obprof

# OBS_PID=<pid> SAMPLE_SECS=30 bash collect_profile.sh --out ./obprof --execute

#

# 可选环境变量：

# OBS_PID / PROXY_PID / SYSBENCH_PID

# PERF_SCOPE=pid|cpu（默认 pid：perf -p OBS_PID；cpu：perf -C OBS_CPUS 整核，产物单独标明范围）

# OBS_CPUS=0-31（仅 PERF_SCOPE=cpu 时用于 perf -C；默认不按核采集）

# OB_HOST / OB_PORT / OB_USER / OB_DB / OB_PASSWORD（用于 GVSYSSTAT/线程/审计快照）
# OB_PORT 默认 2881（官方 mysql_port 默认；2882 是 rpc_port）；实测部署以 install.cfg 为准并显式设置

# PMU_SCRIPT=/path/to/ALL_Linux_PMU.sh（Topdown/PMU）

# FLAMEGRAPH_DIR=/path/to/FlameGraph

#

# OUT_DIR 必须为空目录（或不存在）；拒绝复用旧产物，避免把上次未运行的采集器产物算作本轮成功。

# 采集器并发启动，timing.txt 记录各自起止时间；不修改配置、不重启进程；只读取 /proc、perf、sysstat、OB 视图。

set -u

OUT_DIR=""
EXECUTE=0
SAMPLE_SECS="${SAMPLE_SECS:-30}"
OBS_CPUS="${OBS_CPUS:-0-31}"
PERF_SCOPE="${PERF_SCOPE:-pid}"
OB_HOST="${OB_HOST:-127.0.0.1}"
OB_PORT="${OB_PORT:-2881}"
OB_USER="${OB_USER:-root@sys}"
OB_DB="${OB_DB:-oceanbase}"
OB_PASSWORD="${OB_PASSWORD:-}"
PMU_SCRIPT="${PMU_SCRIPT:-}"
FLAMEGRAPH_DIR="${FLAMEGRAPH_DIR:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --out)
      if [ $# -lt 2 ] || [ -z "${2:-}" ]; then
        echo "FAIL --out 缺少目录参数" >&2
        sed -n '7,11p' "$0" >&2
        exit 2
      fi
      OUT_DIR="$2"; shift 2
      ;;
    --execute) EXECUTE=1; shift ;;
    -h|--help)
      sed -n '2,20p' "$0"
      exit 0
    ;;
    *)
      echo "未知参数：$1" >&2
      exit 2
    ;;
  esac
done

if [ -z "${OUT_DIR}" ]; then
  OUT_DIR="./ob_profile_$(date +%Y%m%d-%H%M%S)"
fi

need_cmd() { command -v "$1" >/dev/null 2>&1; }

# 输入校验辅助（先于目标解析与窗口校验定义）
int_ok() { printf '%s' "$1" | grep -qE '^[0-9]+$'; }

if [ -z "${OBS_PID:-}" ]; then
  if need_cmd pgrep; then
    mapfile -t OBS_PIDS < <(pgrep -x observer || true)
    if [ "${#OBS_PIDS[@]}" -gt 1 ]; then
      echo "FAIL 检测到多个 observer 实例（PID: ${OBS_PIDS[*]}）；请用 OBS_PID=<pid> 显式指定目标实例，避免热点归因落到错误实例" >&2
      exit 2
    fi
    if [ "${#OBS_PIDS[@]}" -eq 1 ]; then
      OBS_PID="${OBS_PIDS[0]}"
    fi
  fi
fi

if [ -z "${OBS_PID:-}" ]; then
  echo "FAIL 未找到 observer PID；请设置 OBS_PID" >&2
  exit 2
fi
if ! int_ok "${OBS_PID}"; then
  echo "FAIL OBS_PID 须为数字 PID（当前：${OBS_PID}）" >&2
  exit 2
fi

# 采集窗口/端口校验（非法值按用法错误拒绝，不静默跑出无效窗口）
if ! int_ok "${SAMPLE_SECS}" || [ "${SAMPLE_SECS}" -lt 1 ] || [ "${SAMPLE_SECS}" -gt 600 ]; then
  echo "FAIL SAMPLE_SECS 须为 1-600 的整数（当前：${SAMPLE_SECS}）" >&2
  exit 2
fi
if ! int_ok "${OB_PORT}" || [ "${OB_PORT}" -lt 1 ] || [ "${OB_PORT}" -gt 65535 ]; then
  echo "FAIL OB_PORT 须为 1-65535（当前：${OB_PORT}）" >&2
  exit 2
fi
case "${PERF_SCOPE}" in
  pid|cpu) ;;
  *) echo "FAIL PERF_SCOPE 须为 pid 或 cpu（当前：${PERF_SCOPE}）" >&2; exit 2 ;;
esac
if [ "${PERF_SCOPE}" = "pid" ]; then
  PERF_LABEL="pid${OBS_PID}"
else
  PERF_LABEL="cpus${OBS_CPUS}"
fi

echo "== OceanBase profile plan =="
echo "out        : ${OUT_DIR}"
echo "execute    : ${EXECUTE}"
echo "sample_secs: ${SAMPLE_SECS}"
echo "obs_pid    : ${OBS_PID}"
echo "perf_scope : ${PERF_SCOPE} (${PERF_LABEL})"
echo "obs_cpus   : ${OBS_CPUS}"
[ -n "${PROXY_PID:-}" ] && echo "proxy_pid  : ${PROXY_PID}"
[ -n "${SYSBENCH_PID:-}" ] && echo "sysbench   : ${SYSBENCH_PID}"
[ -n "${PMU_SCRIPT}" ] && echo "pmu_script : ${PMU_SCRIPT}"

PLAN=()
PLAN+=("meta.txt: 场景/版本/绑核/窗口")
if need_cmd perf; then
  if [ "${PERF_SCOPE}" = "pid" ]; then
    PLAN+=("perf record -p ${OBS_PID} -F 99 -g -- sleep ${SAMPLE_SECS}（默认按 PID，避免混入同核其他进程/漏采）")
  else
    PLAN+=("perf record -C ${OBS_CPUS} -F 99 -g -- sleep ${SAMPLE_SECS}（整核 ${OBS_CPUS}，含核上所有任务；产物标记 ${PERF_LABEL}）")
  fi
fi
[ -x /usr/share/bcc/tools/offcputime ] && PLAN+=("offcputime -p ${OBS_PID} ${SAMPLE_SECS}")
PLAN+=("mpstat -P ALL 1 ${SAMPLE_SECS}")
PLAN+=("pidstat -p ${OBS_PID}[,proxy,sysbench] 1 ${SAMPLE_SECS}")
PLAN+=("iostat -x 1 ${SAMPLE_SECS}（如有）")
PLAN+=('GV$SYSSTAT 窗口起止两份快照（sysstat_start/sysstat_end，供差分）/ GV$OB_THREAD / GV$OB_SQL_AUDIT（如 obclient 可用）')
[ -n "${PMU_SCRIPT}" ] && PLAN+=("PMU/Topdown: ${PMU_SCRIPT}")
PLAN+=("perf/offcpu/mpstat/pidstat/iostat 并发启动，timing.txt 记录各自起止时间")

echo "-- steps --"
for p in "${PLAN[@]}"; do echo "  - $p"; done

if [ "${EXECUTE}" -ne 1 ]; then
  echo "dry-run 结束。加 --execute 执行采集。"
  exit 0
fi

# 输出目录必须为空（或不存在）：拒绝复用旧产物，避免第二次运行缺 perf 时把旧 perf.data 计作本轮成功
if [ -d "${OUT_DIR}" ]; then
  if ! DIR_CONTENTS="$(ls -A "${OUT_DIR}" 2>/dev/null)"; then
    echo "FAIL 无法读取输出目录 ${OUT_DIR}" >&2
    exit 2
  fi
  if [ -n "${DIR_CONTENTS}" ]; then
    echo "FAIL 输出目录 ${OUT_DIR} 非空；请换新目录或先清空，避免把上次产物计作本轮成功" >&2
    exit 2
  fi
fi
mkdir -p "${OUT_DIR}"
DEFAULTS_FILE=""
cleanup() { [ -n "${DEFAULTS_FILE}" ] && rm -f "${DEFAULTS_FILE}"; }
trap cleanup EXIT INT TERM

# timing.txt 与采集器清单（OB 快照要在窗口起点记录时间，故提前初始化）
TIMING_FILE="${OUT_DIR}/timing.txt"
: > "${TIMING_FILE}"
COLLECTOR_PIDS=()
COLLECTOR_NAMES=()

# obclient 查询封装：口令只经 mktemp defaults 文件，不进 argv；SQL 串用单引号保护 GV$ 视图名
obq() {
  if [ -n "${DEFAULTS_FILE}" ]; then
    # --defaults-extra-file 必须首位，放在 -h/-P/-u/-D 之后客户端会忽略，口令读不到
    obclient --defaults-extra-file="${DEFAULTS_FILE}" -h "${OB_HOST}" -P "${OB_PORT}" \
      -u "${OB_USER}" -D "${OB_DB}" -N -B -e "$1" 2>/dev/null
  else
    obclient -h "${OB_HOST}" -P "${OB_PORT}" -u "${OB_USER}" -D "${OB_DB}" \
      -N -B -e "$1" 2>/dev/null
  fi
}

OB_AVAIL=0
SYSSTAT_START_TS=""
SYSSTAT_END_TS=""
if need_cmd obclient; then
  OB_AVAIL=1
  if [ -n "${OB_PASSWORD}" ]; then
    umask 077
    DEFAULTS_FILE="$(mktemp)"
    printf '[client]\npassword=%s\n' "${OB_PASSWORD}" > "${DEFAULTS_FILE}"
  fi
  # 窗口起点快照：必须在采集器启动前采，不能推迟到 perf 后处理之后
  SYSSTAT_START_TS="$(date -Is)"
  printf 'sysstat_start=%s\n' "${SYSSTAT_START_TS}" >> "${TIMING_FILE}"
  obq 'SELECT /*+read_consistency(weak)*/ NAME,VALUE FROM GV$SYSSTAT WHERE STAT_ID IN (10000,10001,10002,10003,10005,10006,140002,140003,40006,40007,40008,40009,40010,40011,40012,50000,50001,50008,50009,60000,60001,60002,60003,60004,60005,60019,60020,60021,60022,60023,60024,80057) AND CON_ID > 1000;' > "${OUT_DIR}/sysstat_start.txt" || true
else
  echo "obclient not found; skip OB views" > "${OUT_DIR}/obclient_missing.txt"
fi

{
  echo "time=$(date -Is)"
  echo "host=$(hostname)"
  echo "kernel=$(uname -r)"
  echo "arch=$(uname -m)"
  echo "obs_pid=${OBS_PID}"
  echo "obs_cpus=${OBS_CPUS}"
  echo "perf_scope=${PERF_SCOPE}"
  echo "perf_label=${PERF_LABEL}"
  echo "sample_secs=${SAMPLE_SECS}"
  echo "proxy_pid=${PROXY_PID:-}"
  echo "sysbench_pid=${SYSBENCH_PID:-}"
  ps -o pid,ppid,comm,psr,pcpu,pmem -p "${OBS_PID}" 2>/dev/null
} > "${OUT_DIR}/meta.txt"

# 线程快照

ps -Lo pid,tid,comm,psr,pcpu -p "${OBS_PID}" > "${OUT_DIR}/threads_ps.txt" 2>/dev/null || true

# 并发采集：perf/offcpu/mpstat/pidstat/iostat 同一窗口启动，timing.txt 记录各自起止时间

launch_collector() {
  # $1=通道名 $2=stdout 文件 $3=stderr 文件（"/dev/null" 表示丢弃）其余为命令与参数
  name="$1"; stdout_file="$2"; stderr_file="$3"; shift 3
  printf '%s start=%s\n' "${name}" "$(date -Is)" >> "${TIMING_FILE}"
  "$@" > "${stdout_file}" 2> "${stderr_file}" &
  COLLECTOR_PIDS+=("$!")
  COLLECTOR_NAMES+=("${name}")
}

if need_cmd perf; then
  if [ "${PERF_SCOPE}" = "pid" ]; then
    launch_collector perf /dev/null "${OUT_DIR}/perf.err" \
      perf record -p "${OBS_PID}" -F 99 -g -o "${OUT_DIR}/perf.data" -- sleep "${SAMPLE_SECS}"
  else
    launch_collector perf /dev/null "${OUT_DIR}/perf.err" \
      perf record -C "${OBS_CPUS}" -F 99 -g -o "${OUT_DIR}/perf.data" -- sleep "${SAMPLE_SECS}"
  fi
fi
if [ -x /usr/share/bcc/tools/offcputime ]; then
  launch_collector offcpu "${OUT_DIR}/offcpu.folded" "${OUT_DIR}/offcpu.err" \
    /usr/share/bcc/tools/offcputime -df -p "${OBS_PID}" "${SAMPLE_SECS}"
fi

PIDSTAT_PIDS="${OBS_PID}"
[ -n "${PROXY_PID:-}" ] && PIDSTAT_PIDS="${PIDSTAT_PIDS},${PROXY_PID}"
[ -n "${SYSBENCH_PID:-}" ] && PIDSTAT_PIDS="${PIDSTAT_PIDS},${SYSBENCH_PID}"

if need_cmd mpstat; then
  launch_collector mpstat "${OUT_DIR}/mpstat.txt" "${OUT_DIR}/mpstat.err" mpstat -P ALL 1 "${SAMPLE_SECS}"
fi
if need_cmd pidstat; then
  launch_collector pidstat "${OUT_DIR}/pidstat.txt" "${OUT_DIR}/pidstat.err" pidstat -p "${PIDSTAT_PIDS}" 1 "${SAMPLE_SECS}"
fi
if need_cmd iostat; then
  launch_collector iostat "${OUT_DIR}/iostat.txt" "${OUT_DIR}/iostat.err" iostat -x 1 "${SAMPLE_SECS}"
fi

if [ "${#COLLECTOR_PIDS[@]}" -gt 0 ]; then
  for i in "${!COLLECTOR_PIDS[@]}"; do
    wait "${COLLECTOR_PIDS[$i]}"; rc=$?
    printf '%s end=%s rc=%s\n' "${COLLECTOR_NAMES[$i]}" "$(date -Is)" "${rc}" >> "${TIMING_FILE}"
  done
fi

# 窗口终点快照：wait 结束后立即采，先于 perf 后处理（折叠/渲染可达数十秒）
if [ "${OB_AVAIL}" -eq 1 ]; then
  SYSSTAT_END_TS="$(date -Is)"
  printf 'sysstat_end=%s\n' "${SYSSTAT_END_TS}" >> "${TIMING_FILE}"
  obq 'SELECT /*+read_consistency(weak)*/ NAME,VALUE FROM GV$SYSSTAT WHERE STAT_ID IN (10000,10001,10002,10003,10005,10006,140002,140003,40006,40007,40008,40009,40010,40011,40012,50000,50001,50008,50009,60000,60001,60002,60003,60004,60005,60019,60020,60021,60022,60023,60024,80057) AND CON_ID > 1000;' > "${OUT_DIR}/sysstat_end.txt" || true
fi

# perf 后处理（报告/火焰图）在窗口结束后进行；产物名带采集范围标签
if [ -s "${OUT_DIR}/perf.data" ]; then
  perf report -i "${OUT_DIR}/perf.data" --stdio --sort comm,dso,sym > "${OUT_DIR}/perf_report.txt" 2>/dev/null || true
  if [ -n "${FLAMEGRAPH_DIR}" ] && [ -x "${FLAMEGRAPH_DIR}/stackcollapse-perf.pl" ]; then
    perf script -i "${OUT_DIR}/perf.data" > "${OUT_DIR}/perf.script" 2>/dev/null || true
    "${FLAMEGRAPH_DIR}/stackcollapse-perf.pl" "${OUT_DIR}/perf.script" > "${OUT_DIR}/perf.folded" 2>/dev/null || true
    "${FLAMEGRAPH_DIR}/flamegraph.pl" "${OUT_DIR}/perf.folded" > "${OUT_DIR}/observer_${PERF_LABEL}.svg" 2>/dev/null || true
  fi
fi

# OB 视图：sysstat 起止快照已在窗口边界完成，这里补线程/审计
if [ "${OB_AVAIL}" -eq 1 ]; then
  obq 'SELECT SVR_IP,SVR_PORT,TENANT_ID,TID,TNAME,STATUS,LATCH_WAIT,LATCH_HOLD,CGROUP_PATH FROM GV$OB_THREAD;' > "${OUT_DIR}/gv_ob_thread.txt" || true
  obq 'SELECT /*+read_consistency(weak)*/ SVR_IP,TENANT_ID,PLAN_ID,ELAPSED_TIME,QUEUE_TIME,EXECUTE_TIME,RETURN_ROWS,RET_CODE FROM GV$OB_SQL_AUDIT ORDER BY ELAPSED_TIME DESC LIMIT 20;' > "${OUT_DIR}/sql_audit_top.txt" || true
fi

# PMU/Topdown

if [ -n "${PMU_SCRIPT}" ] && [ -f "${PMU_SCRIPT}" ]; then
  bash "${PMU_SCRIPT}" "${OUT_DIR}/pmu" > "${OUT_DIR}/pmu.log" 2>&1 || true
fi

# 至少一项采集器产生有效样本：perf/offcpu/mpstat/pidstat/iostat/OB sysstat 窗口快照全缺时
# 按无数据失败（空跑不能以 0 结束——没有证据可判读）
VALID=0
[ -s "${OUT_DIR}/perf.data" ] && VALID=$((VALID + 1))
[ -s "${OUT_DIR}/offcpu.folded" ] && VALID=$((VALID + 1))
[ -s "${OUT_DIR}/mpstat.txt" ] && VALID=$((VALID + 1))
[ -s "${OUT_DIR}/pidstat.txt" ] && VALID=$((VALID + 1))
[ -s "${OUT_DIR}/iostat.txt" ] && VALID=$((VALID + 1))
[ -s "${OUT_DIR}/sysstat_start.txt" ] && [ -s "${OUT_DIR}/sysstat_end.txt" ] && VALID=$((VALID + 1))
{
  echo "perf_scope=${PERF_SCOPE} (${PERF_LABEL})"
  echo "timing=timing.txt（各采集器起止时间；sysstat_start/sysstat_end 为窗口边界快照时刻）"
  echo "sysstat_window_start=${SYSSTAT_START_TS:-none}"
  echo "sysstat_window_end=${SYSSTAT_END_TS:-none}"
  echo "perf_data=$([ -s "${OUT_DIR}/perf.data" ] && echo ok || echo missing)"
  echo "offcpu=$([ -s "${OUT_DIR}/offcpu.folded" ] && echo ok || echo missing)"
  echo "mpstat=$([ -s "${OUT_DIR}/mpstat.txt" ] && echo ok || echo missing)"
  echo "pidstat=$([ -s "${OUT_DIR}/pidstat.txt" ] && echo ok || echo missing)"
  echo "iostat=$([ -s "${OUT_DIR}/iostat.txt" ] && echo ok || echo missing)"
  echo "ob_sysstat_start=$([ -s "${OUT_DIR}/sysstat_start.txt" ] && echo ok || echo missing)"
  echo "ob_sysstat_end=$([ -s "${OUT_DIR}/sysstat_end.txt" ] && echo ok || echo missing)"
  echo "gv_ob_thread=$([ -s "${OUT_DIR}/gv_ob_thread.txt" ] && echo ok || echo missing)"
  echo "sql_audit=$([ -s "${OUT_DIR}/sql_audit_top.txt" ] && echo ok || echo missing)"
} > "${OUT_DIR}/status.txt"
if [ "${VALID}" -eq 0 ]; then
  echo "FAIL 本轮无任何有效样本（采集器缺失或全部失败），无数据可判读——见 ${OUT_DIR}/status.txt" >&2
  exit 1
fi

echo "采集完成：${OUT_DIR}（有效样本通道 ${VALID} 项，状态见 status.txt）"
ls -1 "${OUT_DIR}" 2>/dev/null | sed 's/^/  /'
exit 0
