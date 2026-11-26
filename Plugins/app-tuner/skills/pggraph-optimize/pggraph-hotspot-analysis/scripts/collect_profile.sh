#!/usr/bin/env bash
# pgGraph profile collector.
# Default: dry-run (prints the plan, writes nothing). Use --run to collect.
# Read-only with respect to PostgreSQL data.
#
# Usage:
#   PGSQL_PREFIX=/opt/pgsql/18.3 PGPORT=6666 PGUSER=postgres PGDB=postgres \
#   BACKEND_PID=12345 CPULIST=0-7 DURATION=30 \
#     bash collect_profile.sh            # dry-run
#     bash collect_profile.sh --run      # collect
#
# Output: ${RESULTS_DIR}/profile-<run_id>/
#   env.txt / pg_stat_activity_*.txt / pg_stat_statements_*.txt / window.txt /
#   perf-stat.txt / perf-record.txt / perf.data / smaps-rollup.txt / summary.txt
# 凭据经 PGPASSWORD 环境变量或 ~/.pgpass 提供（所有 psql 已加 -w：无口令时快速
# 失败、绝不交互挂起；口令不进 argv/日志）。
# Exit: 0 all requested items ok; 1 a requested item failed; 2 usage error;
#       3 output directory conflict.
set -u

PGSQL_PREFIX="${PGSQL_PREFIX:-}"
PGHOST="${PGHOST:-localhost}"
PGPORT="${PGPORT:-5432}"
PGUSER="${PGUSER:-postgres}"
PGDB="${PGDB:-postgres}"
BACKEND_PID="${BACKEND_PID:-}"
CPULIST="${CPULIST:-}"
DURATION="${DURATION:-30}"
RESULTS_DIR="${RESULTS_DIR:-./profiles}"

# --duration 校验：须为 1-600 的正整数（sleep 会接受 infinity 等非法值，禁止透传）
int_ok() { printf '%s' "$1" | grep -qE '^[0-9]+$'; }
if ! int_ok "${DURATION}" || [ "${DURATION}" -lt 1 ] || [ "${DURATION}" -gt 600 ]; then
  echo "ERROR: DURATION 须为 1-600 的整数（当前：${DURATION}）" >&2
  exit 2
fi
if [ -n "${BACKEND_PID}" ] && { ! int_ok "${BACKEND_PID}" || [ "${BACKEND_PID}" -lt 1 ]; }; then
  echo "ERROR: BACKEND_PID 须为正整数（当前：${BACKEND_PID}）" >&2
  exit 2
fi

# 参数解析：仅接受 --run / -h / --help，其余一律用法错误
case "${1:-}" in
  "") RUN=0 ;;
  --run) RUN=1 ;;
  -h|--help)
    sed -n '2,20p' "$0"; exit 0 ;;
  *) echo "ERROR: 未知参数: $1（仅支持 --run）" >&2; exit 2 ;;
esac

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)_$$"
OUT="${RESULTS_DIR}/profile-${RUN_ID}"

if [ -z "${PGSQL_PREFIX}" ] && command -v pg_config >/dev/null 2>&1; then
  PGSQL_PREFIX="$(dirname "$(dirname "$(command -v pg_config)")")"
fi
PSQL="${PGSQL_PREFIX}/bin/psql"
[ -x "${PSQL}" ] || PSQL="$(command -v psql || true)"

say() { printf '%s\n' "$*"; }

say "== pgGraph profile plan =="
say "output: ${OUT}"
say "PG: ${PGHOST}:${PGPORT} user=${PGUSER} db=${PGDB}"
say "backend pid: ${BACKEND_PID:-<unset>}"
say "cpu list: ${CPULIST:-<unset>}"
say "duration: ${DURATION}s"
say ""

if [ "${RUN}" -ne 1 ]; then
  say "Dry-run. Planned steps (perf stat 与 perf record 同一窗口并行):"
  say "  1. write env.txt (uname/lscpu/numactl/pg_config/graph.so sha256)"
  say "  2. snapshot pg_stat_activity and pg_stat_statements (start/end of window)"
  say "  3. perf stat + perf record -F 99 --call-graph dwarf (same window, parallel)"
  say "  4. smaps_rollup for backend pid (if set)"
  say "  5. window.txt records start/end timestamps"
  say "  6. print next commands for flamegraph"
  say ""
  say "Re-run with --run to collect."
  exit 0
fi

# 每轮原子创建独立目录：目录已存在一律拒绝（复用会截断上轮产物），退出码 3
mkdir -p "${RESULTS_DIR}" || { echo "cannot create ${RESULTS_DIR}" >&2; exit 1; }
mkdir "${OUT}" 2>/dev/null || { echo "ERROR: 输出目录已存在或无法创建: ${OUT}" >&2; exit 3; }
chmod 700 "${OUT}"

STATUS_PSQL=skip; STATUS_PERF=skip; STATUS_SMAPS=skip
RC=0

{
  echo "# generated ${RUN_ID}"
  echo "## uname"; uname -a
  echo "## lscpu"; lscpu
  echo "## numactl"; numactl -H 2>/dev/null || true
  echo "## pg_config"; "${PGSQL_PREFIX}/bin/pg_config" --version 2>/dev/null || true
  echo "## graph.so sha256"
  libdir="$("${PGSQL_PREFIX}/bin/pg_config" --pkglibdir 2>/dev/null || true)"
  [ -n "${libdir}" ] && sha256sum "${libdir}/graph.so" 2>/dev/null || true
  echo "## env"
  echo "PGHOST=${PGHOST} PGPORT=${PGPORT} PGUSER=${PGUSER} PGDB=${PGDB}"
  echo "BACKEND_PID=${BACKEND_PID} CPULIST=${CPULIST} DURATION=${DURATION}"
} > "${OUT}/env.txt" 2>&1 || true

snap_sql() {  # $1: suffix (start/end)；返回真实退出状态（失败不再静默转成功）
  [ -n "${PSQL}" ] && [ -x "${PSQL}" ] || {
    echo "psql not found; skipped SQL snapshots (${1})" > "${OUT}/pg_stat_activity_${1}.txt"
    return 1
  }
  local rc=0
  # -w 禁止交互式口令提示：agent 调起时无 PGPASSWORD/.pgpass 会挂满窗口，宁快失败走 fail/skip
  "${PSQL}" -w -h "${PGHOST}" -p "${PGPORT}" -U "${PGUSER}" -d "${PGDB}" -At -v ON_ERROR_STOP=1 \
    -c "SELECT state, wait_event_type, wait_event, count(*) FROM pg_stat_activity WHERE datname=current_database() GROUP BY 1,2,3 ORDER BY 4 Desc" \
    > "${OUT}/pg_stat_activity_${1}.txt" 2>&1 || rc=1
  # dbid 过滤到当前数据库：pg_stat_statements 汇总整个实例，跨库读取会把其他库的 SQL
  # 文本带进证据包、全实例 Top20 也会挤掉目标库的慢查询
  if [ -n "${HAVE_PGSS:-}" ]; then
    "${PSQL}" -w -h "${PGHOST}" -p "${PGPORT}" -U "${PGUSER}" -d "${PGDB}" -At -v ON_ERROR_STOP=1 \
      -c "SELECT queryid, calls, total_exec_time, mean_exec_time, rows FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database()) ORDER BY total_exec_time DESC LIMIT 20" \
      > "${OUT}/pg_stat_statements_${1}.txt" 2>&1 || { rc=1; touch "${OUT}/.pgss_fail"; }
  else
    echo "pg_stat_statements unavailable（未安装或预检连接失败）; skipped (本层记 skip 而非 fail)" \
      > "${OUT}/pg_stat_statements_${1}.txt"
  fi
  return $rc
}

# ---- 同一稳态窗口：SQL 起点快照 + perf stat/record 并行 + SQL 终点快照 ----
date -u '+start=%FT%TZ' > "${OUT}/window.txt"
# pg_stat_statements 扩展预检：按输出值判定（psql 对「查询成功但空结果」同样退出 0，
# 只看退出码会把扩展缺失误判为已安装）；预检自身连接/认证失败单独记录，不冒充扩展缺失
HAVE_PGSS=""
PGSS_PRECHECK_FAIL=0
if [ -n "${PSQL}" ] && [ -x "${PSQL}" ]; then
  pgss_out=$("${PSQL}" -w -h "${PGHOST}" -p "${PGPORT}" -U "${PGUSER}" -d "${PGDB}" -At -c \
    "SELECT 1 FROM pg_extension WHERE extname='pg_stat_statements'" 2>/dev/null)
  if [ $? -eq 0 ]; then
    [ "${pgss_out:-}" = "1" ] && HAVE_PGSS=1
  else
    PGSS_PRECHECK_FAIL=1
    echo "[FAIL] pg_stat_statements 预检连接/认证失败——与扩展缺失区分，计入 psql/pgss 失败" >&2
  fi
fi
# 起点快照失败不得被终点成功掩盖（报错文本混进证据包会被当正常快照）
START_FAILED=0
snap_sql start || START_FAILED=1

PERF_PIDS=()
if [ -n "${BACKEND_PID}" ]; then
  # PGHOST 远程时 pg_stat_activity 的 PID 属于远端实例，本机 /proc 同号 PID 可能是无关
  # 进程：核实本机 PID 确为 postgres 后端才采 perf/proc，否则只采 SQL 并记录原因
  PID_OWNER_OK=0
  if [ "${PGHOST}" = "localhost" ] || [ "${PGHOST}" = "127.0.0.1" ]; then
    comm=$(cat "/proc/${BACKEND_PID}/comm" 2>/dev/null || true)
    case "${comm}" in
      postgres*) PID_OWNER_OK=1 ;;
      *) echo "[SKIP] /proc/${BACKEND_PID}（comm=${comm:-不存在}）不是 postgres 进程，跳过 perf/proc 采集，仅采 SQL" >&2 ;;
    esac
    # 第三重核对（实例归属）：两套本机 PostgreSQL 并存时，仅凭进程名仍可能是另一集群
    # 的 backend——postgres 进程 cmdline 含 -D <数据目录>，与本次 PGDATA（若提供）核对
    if [ "${PID_OWNER_OK}" = 1 ] && [ -n "${PGDATA:-}" ]; then
      pid_d=$(tr '\0' ' ' < "/proc/${BACKEND_PID}/cmdline" 2>/dev/null || true)
      case " ${pid_d} " in
        *" -D ${PGDATA} "*|*" -D ${PGDATA} "*)
          : ;;
        *)
          echo "[SKIP] /proc/${BACKEND_PID} 是 postgres 但数据目录不是本次 PGDATA=${PGDATA}（cmdline: ${pid_d}），疑似另一实例，跳过 perf/proc 采集，仅采 SQL" >&2
          PID_OWNER_OK=0
          ;;
      esac
    fi
  else
    echo "[SKIP] PGHOST=${PGHOST} 为远程实例，本机 /proc/${BACKEND_PID} 归属无法核实，跳过 perf/proc 采集，仅采 SQL" >&2
  fi
  if [ "${PID_OWNER_OK}" = 1 ] && command -v perf >/dev/null 2>&1; then
    ( perf stat -p "${BACKEND_PID}" -e cycles,instructions,cache-references,cache-misses \
        -- sleep "${DURATION}" > "${OUT}/perf-stat.txt" 2>&1; echo $? > "${OUT}/.perf_stat_rc" ) &
    PERF_PIDS+=($!)
    ( perf record -F 99 --call-graph dwarf -p "${BACKEND_PID}" -o "${OUT}/perf.data" \
        -- sleep "${DURATION}" > "${OUT}/perf-record.txt" 2>&1; echo $? > "${OUT}/.perf_record_rc" ) &
    PERF_PIDS+=($!)
    STATUS_PERF=running
  elif [ "${PID_OWNER_OK}" = 1 ]; then
    echo "perf not found" > "${OUT}/perf-record.txt"
    STATUS_PERF=skip
  fi
elif [ -n "${CPULIST}" ]; then
  if command -v perf >/dev/null 2>&1; then
    ( perf stat -C "${CPULIST}" -e cycles,instructions,cache-references,cache-misses \
        -- sleep "${DURATION}" > "${OUT}/perf-stat.txt" 2>&1; echo $? > "${OUT}/.perf_stat_rc" ) &
    PERF_PIDS+=($!)
    ( perf record -F 99 -C "${CPULIST}" --call-graph dwarf \
        -o "${OUT}/perf.data" -- sleep "${DURATION}" > "${OUT}/perf-record.txt" 2>&1; echo $? > "${OUT}/.perf_record_rc" ) &
    PERF_PIDS+=($!)
    STATUS_PERF=running
  else
    echo "perf not found" > "${OUT}/perf-record.txt"
    STATUS_PERF=skip
  fi
else
  echo "set BACKEND_PID or CPULIST to collect perf data" > "${OUT}/perf-record.txt"
fi

for p in "${PERF_PIDS[@]:-}"; do
  [ -n "$p" ] && wait "$p" 2>/dev/null
done
date -u '+end=%FT%TZ' >> "${OUT}/window.txt"

if snap_sql end && [ "${START_FAILED}" = 0 ]; then
  STATUS_PSQL=ok
else
  STATUS_PSQL=fail
fi

# smaps 与 perf 同用 PID 归属守卫：未核实归属（远程实例/本机同号非 postgres 进程）时
# 一律 skip——本机可读的同号无关进程其 Pss/Rss 会被误当数据库证据（这正是守卫要防的）
if [ -n "${BACKEND_PID}" ]; then
  if [ "${PID_OWNER_OK:-0}" = 1 ]; then
    if grep -E '^(Pss|Private_Clean|Private_Dirty|Rss):' "/proc/${BACKEND_PID}/smaps_rollup" \
        > "${OUT}/smaps-rollup.txt" 2>&1; then
      STATUS_SMAPS=ok
    else
      STATUS_SMAPS=fail
    fi
  else
    echo "PID 归属未核实（远程实例或非 postgres 进程），跳过 smaps 采集（仅采 SQL）" \
      > "${OUT}/smaps-rollup.txt"
    STATUS_SMAPS=skip
  fi
fi

if [ "${STATUS_PERF}" = "running" ]; then
  stat_rc=$(cat "${OUT}/.perf_stat_rc" 2>/dev/null || echo 1)
  rec_rc=$(cat "${OUT}/.perf_record_rc" 2>/dev/null || echo 1)
  # 删除仅限自建标记文件：子 shell 内相对化执行（rm 的实参不含 / 与变量路径）
  (cd "${OUT}" && rm -f .perf_stat_rc .perf_record_rc)
  if [ "${rec_rc}" = 0 ] && [ -s "${OUT}/perf.data" ]; then
    if [ "${stat_rc}" = 0 ]; then
      STATUS_PERF=ok
    else
      STATUS_PERF=fail   # record 成功但 stat 失败（如 PMU 事件不受支持）也要入摘要
      echo "[FAIL] perf stat 失败（rc=${stat_rc}，事件不受支持/权限？），见 perf-stat.txt" >&2
    fi
    if perf report -i "${OUT}/perf.data" --stdio --no-children -g none --percent-limit 0.5 \
        > "${OUT}/perf-report.txt" 2>&1; then
      :   # report 可解析：perf.data 有效性二次确认
    else
      STATUS_PERF=fail   # record 产生了数据但 report 无法解析（损坏/版本不匹配）也按失败传播
      echo "[FAIL] perf report 解析失败（perf.data 损坏/版本不匹配），见 perf-report.txt" >&2
    fi
  else
    STATUS_PERF=fail
  fi
fi

# 逐项状态入 summary.txt；被请求的采集项失败 → 退出码 1
if [ "${PGSS_PRECHECK_FAIL}" = 1 ] || [ -e "${OUT}/.pgss_fail" ]; then
  STATUS_PGSS=fail
  (cd "${OUT}" && rm -f .pgss_fail)   # 同上：仅自建标记文件，相对化删除
elif [ -n "${HAVE_PGSS}" ]; then
  STATUS_PGSS=ok
else
  STATUS_PGSS=skip
fi
{
  echo "psql=${STATUS_PSQL}"
  echo "pgss=${STATUS_PGSS}"
  echo "perf=${STATUS_PERF}"
  echo "smaps=${STATUS_SMAPS}"
} > "${OUT}/summary.txt"
for st in "${STATUS_PSQL}" "${STATUS_PGSS}" "${STATUS_PERF}" "${STATUS_SMAPS}"; do
  [ "$st" = "fail" ] && RC=1
done

say "collected: ${OUT}"
say "summary: psql=${STATUS_PSQL} pgss=${STATUS_PGSS} perf=${STATUS_PERF} smaps=${STATUS_SMAPS}"
say "next:"
say "  perf script -i ${OUT}/perf.data | stackcollapse-perf.pl | flamegraph.pl > ${OUT}/flamegraph.svg"
exit "${RC}"
