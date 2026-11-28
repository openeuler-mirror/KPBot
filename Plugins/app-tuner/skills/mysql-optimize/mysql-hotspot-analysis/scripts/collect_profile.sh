#!/usr/bin/env bash

# MySQL 热点采集（默认 dry-run 打印计划；--execute 才采集）。
#
# 用法：
#   bash scripts/collect_profile.sh                        # dry-run
#   bash scripts/collect_profile.sh --self-check           # 仅校验脚本契约（续行/输出路径）
#   OUT_DIR=/data/profile RUN_SECONDS=20 \
#     MYSQL_DEFAULTS_EXTRA_FILE=${HOME}/.my.cnf.trace \
#     MYSQL_SOCKET=/var/lib/mysql/mysql.sock \
#     bash scripts/collect_profile.sh --execute
#
# 说明：只读采集；不修改 MySQL/系统配置。需要 perf/bcc/iostat 等工具时按缺失降级。
# 身份：PID 必须由所选连接（MYSQL_SOCKET / defaults-extra-file）唯一映射；目标不唯一即停止。
# 样本：认证失败/命令失败写入独立 .err，不计入有效样本；本轮无有效样本写 status.txt 并非零退出。
# 退出码：0=成功/已打印计划/自检通过；1=执行失败或无有效样本；2=参数错误。

set -u

OUT_DIR="${OUT_DIR:-./profile_out}"
RUN_SECONDS="${RUN_SECONDS:-20}"
PERF_FREQ="${PERF_FREQ:-999}"
MYSQL_DEFAULTS_EXTRA_FILE="${MYSQL_DEFAULTS_EXTRA_FILE:-}"
MYSQL_SOCKET="${MYSQL_SOCKET:-}"
RUN_SECONDS_MAX=300
EXECUTE=0
SELF_CHECK=0

for arg in "$@"; do
  case "$arg" in
    --execute) EXECUTE=1 ;;
    --dry-run) EXECUTE=0 ;;
    --self-check) SELF_CHECK=1 ;;
    -h|--help) echo "usage: $0 [--dry-run|--execute|--self-check]"; exit 0 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

# 契约自检：静态核对三条采集命令的续行与输出路径，防止再次出现
# 「换行断裂 → perf 没有 -o/-- sleep 挂死、perf.data 写进 CWD」的回归。
# 不依赖 perf/mysql 是否安装，可在 CI 直接运行。
contract_self_check() {
  local script_dir script rc=0
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  script="${script_dir}/$(basename "${BASH_SOURCE[0]}")"
  check_one() {
    if ! grep -Fq -- "$2" "$script"; then
      echo "[SELF-CHECK FAIL] 缺少契约片段：$1" >&2
      rc=1
    fi
  }
  check_one "perf record 续行到 -o" '-p "${PID}" \'
  check_one "perf record 输出限定在 OUT_DIR 且带 sleep 定时" '-o "${OUT_DIR}/perf.data" -- sleep "${RUN_SECONDS}"'
  check_one "perf record 日志重定向" '> "${OUT_DIR}/perf_record.log" 2>&1'
  check_one "perf report 续行" '-g graph,0.5,callee --stdio \'
  check_one "perf report 输出限定在 OUT_DIR" '> "${OUT_DIR}/perf_report.txt" 2>&1'
  check_one "offcputime 续行" '-p "${PID}" "${RUN_SECONDS}" \'
  check_one "offcputime 输出限定在 OUT_DIR" '> "${OUT_DIR}/offcpu.txt" 2>&1'
  if [ "${rc}" -eq 0 ]; then
    echo "[SELF-CHECK OK] 采集命令续行与输出路径契约完好"
  fi
  return "${rc}"
}

if [ "${SELF_CHECK}" -eq 1 ]; then
  contract_self_check
  exit $?
fi

# RUN_SECONDS 校验：必须在启动任何采集器之前完成，误填大值不得长时间占用现场。
case "${RUN_SECONDS}" in
  ''|*[!0-9]*)
    echo "[FAIL] RUN_SECONDS 必须是正整数，允许范围 1..${RUN_SECONDS_MAX}（当前：'${RUN_SECONDS}'）" >&2
    exit 2
    ;;
esac
# bash 的 [ -lt/-gt ] 对超长整数字符串只报 "integer expression expected" 并按假处理，
# 会让 20 位数字越过范围校验，下游 sleep/perf record 长时间挂住现场：先加长度护栏。
if [ "${#RUN_SECONDS}" -gt 9 ]; then
  echo "[FAIL] RUN_SECONDS 数值过长，允许范围 1..${RUN_SECONDS_MAX}（当前：${RUN_SECONDS}）" >&2
  exit 2
fi
if [ "${RUN_SECONDS}" -lt 1 ] || [ "${RUN_SECONDS}" -gt "${RUN_SECONDS_MAX}" ]; then
  echo "[FAIL] RUN_SECONDS 必须在 1..${RUN_SECONDS_MAX} 之间（当前：${RUN_SECONDS}）" >&2
  exit 2
fi

MYSQL_ARGS=()
# --defaults-extra-file 必须是 mysql 的第一个选项（MySQL 8.0 选项顺序要求）。
[ -n "${MYSQL_DEFAULTS_EXTRA_FILE}" ] && MYSQL_ARGS+=(--defaults-extra-file="${MYSQL_DEFAULTS_EXTRA_FILE}")
[ -n "${MYSQL_SOCKET}" ] && MYSQL_ARGS+=(--socket="${MYSQL_SOCKET}")

# 从所选连接读取实例身份；连接认证失败时为空，绝不据此猜测 PID。
MYSQL_IDENT=""
if command -v mysql >/dev/null 2>&1; then
  MYSQL_IDENT="$(mysql "${MYSQL_ARGS[@]}" -N -B \
    -e "SELECT @@GLOBAL.datadir, @@GLOBAL.socket;" 2>/dev/null)"
fi

MYSQLD_PIDS="$(pgrep -x mysqld 2>/dev/null || true)"
if [ -z "${MYSQLD_PIDS}" ]; then
  MYSQLD_PIDS="$(pidof mysqld 2>/dev/null | tr ' ' '\n' || true)"
fi

PID=""
resolve_pid() {
  local p cmd chosen="" hits=0 total=0
  local datadir socket dd_noslash hit

  if [ -n "${MYSQL_IDENT}" ]; then
    datadir="$(printf '%s\n' "${MYSQL_IDENT}" | awk -F'\t' 'NR==1{print $1}')"
    socket="$(printf '%s\n' "${MYSQL_IDENT}" | awk -F'\t' 'NR==1{print $2}')"
    dd_noslash="${datadir%/}"
    for p in ${MYSQLD_PIDS}; do
      cmd="$(tr '\0' ' ' < "/proc/${p}/cmdline" 2>/dev/null || true)"
      [ -z "${cmd}" ] && continue
      hit=0
      [ -n "${dd_noslash}" ] && case "${cmd}" in *"${dd_noslash}"*) hit=1 ;; esac
      [ -n "${socket}" ] && case "${cmd}" in *"${socket}"*) hit=1 ;; esac
      if [ "${hit}" -eq 1 ]; then
        chosen="${p}"
        hits=$((hits + 1))
      fi
    done
    if [ "${hits}" -ne 1 ]; then
      echo "[FAIL] 无法把所选 MySQL 连接唯一映射到一个 mysqld 进程（匹配数=${hits}；连接身份 datadir=${datadir} socket=${socket}）。匹配数不为 1 即停止采集，避免混合不同实例的调用栈/等待事件。" >&2
      return 1
    fi
  else
    for p in ${MYSQLD_PIDS}; do total=$((total + 1)); done
    if [ "${total}" -eq 0 ]; then
      echo "[FAIL] 未找到 mysqld 进程，且无法通过所选连接确认实例身份。" >&2
      return 1
    fi
    if [ "${total}" -ne 1 ]; then
      echo "[FAIL] 无法通过所选连接确认实例身份（认证失败或客户端缺失），且本机存在 ${total} 个 mysqld 进程；目标不唯一，停止采集。" >&2
      return 1
    fi
    chosen="${MYSQLD_PIDS}"
    note "无法读取所选连接身份，但本机仅有 1 个 mysqld，按唯一进程归属；SQL 证据仍以本轮成功查询为准。"
  fi
  PID="${chosen}"
  return 0
}

NOTE_LOG=""
note() {
  echo "[WARN] $*" >&2
  if [ -n "${NOTE_LOG}" ]; then
    echo "$*" >> "${NOTE_LOG}"
  fi
}

if ! resolve_pid; then
  exit 1
fi

echo "== MySQL profile plan =="
echo "PID=${PID}"
echo "OUT_DIR=${OUT_DIR}"
echo "RUN_SECONDS=${RUN_SECONDS}（校验范围 1..${RUN_SECONDS_MAX}）"
echo "PERF_FREQ=${PERF_FREQ}"
echo "MYSQL_SOCKET=${MYSQL_SOCKET:-未指定}"
echo

if [ "${EXECUTE}" -eq 0 ]; then
  cat <<'PLAN'
将执行（只读）：

1. 按所选连接（--socket / --defaults-extra-file）解析唯一 mysqld PID；无法唯一映射则停止
2. ps -Lo pid,tid,comm,psr,pcpu -p PID                    # 线程分布
3. mpstat -P ALL 1 RUN_SECONDS / pidstat -p PID / iostat / sar
4. perf record -F FREQ --call-graph dwarf -p PID -o OUT_DIR/perf.data -- sleep RUN_SECONDS
   perf report -i OUT_DIR/perf.data ... > OUT_DIR/perf_report.txt
5. offcputime -p PID RUN_SECONDS > OUT_DIR/offcpu.txt      # 等待/离核
6. 等待事件：窗口两端按 EVENT_NAME 快照，按 SUM_TIMER_WAIT / COUNT_STAR 的增量排名
7. mysql SHOW ENGINE INNODB STATUS / GLOBAL STATUS / threads 等

认证失败/命令失败单独写入 *.err，不计入有效样本；本轮无有效样本时写 status.txt 并返回非零。
产物写入 OUT_DIR；不修改任何配置。使用 --execute 真正采集；--self-check 仅校验脚本契约。
PLAN
  exit 0
fi

mkdir -p "${OUT_DIR}" || { echo "[FAIL] 无法创建 ${OUT_DIR}" >&2; exit 1; }
NOTE_LOG="${OUT_DIR}/warnings.txt"

# 复用 OUT_DIR 时先清掉上一轮产物，避免把旧 perf.data / 旧 SQL 结果算作本轮有效样本。
STALE=(perf.data perf_report.txt perf.script perf_record.log offcpu.txt
       threads.txt meminfo.txt interrupts_before.txt interrupts_after.txt
       mpstat.txt pidstat.txt pidstat_io.txt iostat.txt net.txt
       innodb_status.txt global_status.txt global_vars.txt ps_threads.txt
       waits_before.tsv waits_after.tsv waits_top.txt status.txt)
for f in "${STALE[@]}"; do
  rm -f "${OUT_DIR}/${f}" "${OUT_DIR}/${f}.err"
done

VALID_SAMPLES=0
valid_sample() { VALID_SAMPLES=$((VALID_SAMPLES + 1)); }

# 统一采集入口：命令退出码非 0 或结果为空都判失败，错误单独落 .err，不计入有效样本。
capture_ok() {
  local name="$1"; shift
  local out="${OUT_DIR}/${name}" rc
  "$@" > "${out}" 2> "${out}.err"
  rc=$?
  if [ "${rc}" -eq 0 ] && [ -s "${out}" ]; then
    return 0
  fi
  note "${name} 采集失败(rc=${rc})，错误见 ${out}.err"
  return 1
}

echo "[INFO] 开始采集 ${RUN_SECONDS}s（PID=${PID}）..."

if capture_ok threads.txt ps -Lo pid,tid,comm,psr,pcpu -p "${PID}"; then valid_sample; fi
if capture_ok meminfo.txt grep -E 'MemTotal|MemFree|Huge' /proc/meminfo; then valid_sample; fi
if capture_ok interrupts_before.txt cat /proc/interrupts; then valid_sample; fi

# 等待事件窗口左边界：先取累计快照（原始整数值，不做绝对值排名）。
WAITS_SQL='SELECT EVENT_NAME, COUNT_STAR, SUM_TIMER_WAIT FROM performance_schema.events_waits_summary_global_by_event_name'
WAITS_BEFORE_OK=0
if command -v mysql >/dev/null 2>&1; then
  if capture_ok waits_before.tsv mysql "${MYSQL_ARGS[@]}" -N -B -e "${WAITS_SQL}"; then
    WAITS_BEFORE_OK=1
  fi
fi

if command -v mpstat >/dev/null 2>&1; then
  mpstat -P ALL 1 "${RUN_SECONDS}" > "${OUT_DIR}/mpstat.txt" 2> "${OUT_DIR}/mpstat.err" &
fi
if command -v pidstat >/dev/null 2>&1; then
  pidstat -p "${PID}" 1 "${RUN_SECONDS}" > "${OUT_DIR}/pidstat.txt" 2> "${OUT_DIR}/pidstat.err" &
  pidstat -d -p "${PID}" 1 "${RUN_SECONDS}" > "${OUT_DIR}/pidstat_io.txt" 2> "${OUT_DIR}/pidstat_io.err" &
fi
if command -v iostat >/dev/null 2>&1; then
  iostat -x 1 "${RUN_SECONDS}" > "${OUT_DIR}/iostat.txt" 2> "${OUT_DIR}/iostat.err" &
fi
if command -v sar >/dev/null 2>&1; then
  sar -n DEV 1 "${RUN_SECONDS}" > "${OUT_DIR}/net.txt" 2> "${OUT_DIR}/net.err" &
fi

if command -v perf >/dev/null 2>&1; then
  if perf record -F "${PERF_FREQ}" --call-graph dwarf -p "${PID}" \
      -o "${OUT_DIR}/perf.data" -- sleep "${RUN_SECONDS}" \
      > "${OUT_DIR}/perf_record.log" 2>&1; then
    if [ -s "${OUT_DIR}/perf.data" ]; then
      valid_sample
      perf report -i "${OUT_DIR}/perf.data" -g graph,0.5,callee --stdio \
        > "${OUT_DIR}/perf_report.txt" 2>&1 || note "perf report 失败，见 ${OUT_DIR}/perf_report.txt"
      perf script -i "${OUT_DIR}/perf.data" \
        > "${OUT_DIR}/perf.script" 2>&1 || note "perf script 失败，见 ${OUT_DIR}/perf.script"
    else
      note "perf record 成功但未生成非空 perf.data"
    fi
  else
    note "perf record 失败，见 ${OUT_DIR}/perf_record.log"
  fi
else
  note "缺少 perf，跳过 on-CPU 采样"
fi

if [ -x /usr/share/bcc/tools/offcputime ]; then
  if /usr/share/bcc/tools/offcputime -p "${PID}" "${RUN_SECONDS}" \
      > "${OUT_DIR}/offcpu.txt" 2>&1; then
    if [ -s "${OUT_DIR}/offcpu.txt" ]; then
      valid_sample
    else
      note "offcputime 输出为空"
    fi
  else
    note "offcputime 失败，见 ${OUT_DIR}/offcpu.txt"
  fi
else
  note "缺少 bcc offcputime，跳过 off-CPU"
fi

wait

if capture_ok interrupts_after.txt cat /proc/interrupts; then valid_sample; fi

if command -v mysql >/dev/null 2>&1; then
  if capture_ok innodb_status.txt mysql "${MYSQL_ARGS[@]}" -e "SHOW ENGINE INNODB STATUS\\G"; then valid_sample; fi
  if capture_ok global_status.txt mysql "${MYSQL_ARGS[@]}" -e "SHOW GLOBAL STATUS"; then valid_sample; fi
  if capture_ok global_vars.txt mysql "${MYSQL_ARGS[@]}" -e "SHOW GLOBAL VARIABLES"; then valid_sample; fi
  if capture_ok ps_threads.txt mysql "${MYSQL_ARGS[@]}" -N -B -e "SELECT THREAD_ID,NAME,TYPE,PROCESSLIST_ID FROM performance_schema.threads"; then valid_sample; fi
else
  note "缺少 mysql 客户端，跳过 DB 状态"
fi

# 等待事件窗口右边界快照 + 按 EVENT_NAME 求增量后排名（绝不按绝对累计值归因）。
if [ "${WAITS_BEFORE_OK}" -eq 1 ] && \
   capture_ok waits_after.tsv mysql "${MYSQL_ARGS[@]}" -N -B -e "${WAITS_SQL}"; then
  awk -F'\t' '
    NR==FNR { c[$1]=$2; t[$1]=$3; next }
    ($1 in c) {
      dc=$2-c[$1]; dt=$3-t[$1]
      if (dc>0 || dt>0) printf "%s\t%d\t%.6f\n", $1, dc, dt/1e12
    }
  ' "${OUT_DIR}/waits_before.tsv" "${OUT_DIR}/waits_after.tsv" \
    | sort -t $'\t' -k3,3nr -k2,2nr | head -30 \
    > "${OUT_DIR}/waits_top.txt" 2> "${OUT_DIR}/waits_top.err"
  if [ -s "${OUT_DIR}/waits_top.txt" ]; then
    valid_sample
  else
    note "等待事件窗口增量为零或无法计算，waits_top.txt 为空"
  fi
else
  note "等待事件快照未成对成功，跳过增量排名（不可用绝对累计值替代）"
fi

STATUS_FILE="${OUT_DIR}/status.txt"
if [ "${VALID_SAMPLES}" -eq 0 ]; then
  {
    echo "status=no_data"
    echo "valid_samples=0"
    echo "run_seconds=${RUN_SECONDS}"
    echo "mysqld_pid=${PID}"
    echo "note=本轮无有效样本（认证失败/采集器缺失/命令失败），产物不可判读"
  } > "${STATUS_FILE}"
  echo "[FAIL] 本轮无任何有效样本，无数据可判读——产物目录 ${OUT_DIR}，状态 ${STATUS_FILE}" >&2
  exit 1
fi

{
  echo "status=ok"
  echo "valid_samples=${VALID_SAMPLES}"
  echo "run_seconds=${RUN_SECONDS}"
  echo "mysqld_pid=${PID}"
  echo "mysql_socket=${MYSQL_SOCKET:-none}"
} > "${STATUS_FILE}"

echo "[INFO] 完成，本轮有效样本 ${VALID_SAMPLES} 项，产物：${OUT_DIR}"
echo "[INFO] 建议：perf_report.txt + offcpu.txt + waits_top.txt（窗口增量排名）+ innodb_status.txt 联合归因"
exit 0
