#!/usr/bin/env bash
# collect_profile.sh — PostgreSQL 只读采集（默认 dry-run，--execute 才落盘）
#
# 同一窗口并行采集：perf record（on-CPU，多 PID 逗号）/ pidstat / mpstat /
# 等待事件快照（psql，可选）。输出落独立时间戳目录；目录已存在即拒绝（exit 3）。
# 不采写、不改 GUC；psql 口令经 PGPASSWORD/.pgpass，不进 argv。
#
# 用法:
#   bash collect_profile.sh --pid 12345 --duration 20                # dry-run 打印计划
#   bash collect_profile.sh --pid 12345 --duration 20 --execute
#   bash collect_profile.sh --pg-pattern 'postgres: postgres sbtest' --duration 30 --execute
#
# 参数:
#   --pid PIDS          目标进程 PID（多实例逗号分隔）
#   --pg-pattern PAT    pgrep -f 模式（与 --pid 二选一；命中后转逗号）
#   --duration SEC      采样窗口 1-600，默认 20
#   --out DIR           输出目录，默认 ./pgsql_profile_<ts>_<pid>
#   --flamegraph-dir D  FlameGraph 脚本目录（可选，有则顺采折叠栈）
#   --psql-sample       压测窗口内做等待事件快照（需 psql 可连；PSQL_* 环境变量）
#   --execute           执行采集（缺省 dry-run）
#
# 退出码: 0=完成; 1=存在失败项; 2=用法错误; 3=输出目录冲突

set -u

PID_ARG=""
PG_PATTERN=""
DURATION=20
OUT=""
FG_DIR=""
PSQL_SAMPLE=0
EXECUTE=0

usage() { cat <<'EOF'
用法: collect_profile.sh --pid <PIDS[,PIDS...]> | --pg-pattern <PAT> \
        [--duration 1-600] [--out DIR] [--flamegraph-dir DIR] [--psql-sample] [--execute]
详见脚本头注释。
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --pid|--pg-pattern|--duration|--out|--flamegraph-dir)
      [ $# -ge 2 ] || { echo "ERROR: $1 需要参数" >&2; usage >&2; exit 2; }
      case "$1" in
        --pid) PID_ARG="$2" ;;
        --pg-pattern) PG_PATTERN="$2" ;;
        --duration) DURATION="$2" ;;
        --out) OUT="$2" ;;
        --flamegraph-dir) FG_DIR="$2" ;;
      esac
      shift 2 ;;
    --psql-sample) PSQL_SAMPLE=1; shift ;;
    --execute) EXECUTE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: 未知参数: $1" >&2; usage >&2; exit 2 ;;
  esac
done

int_ok() { printf '%s' "$1" | grep -qE '^[0-9]+$'; }

# --pid 与 --pg-pattern 二选一：同时传入会在下方合并两组 PID、无差别扩大
# perf/pidstat 的采集范围（与 usage 声明矛盾），按用法错误拒绝
if [ -n "$PID_ARG" ] && [ -n "$PG_PATTERN" ]; then
  echo "ERROR: --pid 与 --pg-pattern 二选一，不可同时指定" >&2
  usage >&2
  exit 2
fi

if [ -n "$PG_PATTERN" ]; then
  if ! command -v pgrep >/dev/null 2>&1; then
    echo "ERROR: 无 pgrep，无法按模式发现进程" >&2; exit 2
  fi
  # pgrep -f 会命中含模式串的调用方 shell 自身：排除 $$ 与祖先进程，
  # 并核验 /proc/<pid>/comm 为 postgres*（非 postgres 的命中一律丢弃）
  self_anc="$$ $PPID"
  found=""
  for p in $(pgrep -f "$PG_PATTERN"); do
    case " $self_anc " in *" $p "*) continue ;; esac
    case "$(cat /proc/$p/comm 2>/dev/null || true)" in postgres*) found="${found:+$found,}$p" ;; esac
  done
  [ -n "$found" ] || { echo "ERROR: 模式未命中 postgres 进程（已排除采集器自身）: ${PG_PATTERN}" >&2; exit 2; }
  PID_ARG="${PID_ARG:+${PID_ARG},}${found}"
fi
[ -n "$PID_ARG" ] || { echo "ERROR: 须指定 --pid 或 --pg-pattern" >&2; usage >&2; exit 2; }
if ! int_ok "$DURATION" || [ "$DURATION" -lt 1 ] || [ "$DURATION" -gt 600 ]; then
  echo "ERROR: --duration 须为 1-600 的整数" >&2; exit 2
fi
# PID 列表形态校验（仅数字与逗号，段非空——防注入拼进命令行）
printf '%s' "$PID_ARG" | grep -qE '^[0-9]+(,[0-9]+)*$' \
  || { echo "ERROR: --pid 须为逗号分隔的数字列表: ${PID_ARG}" >&2; exit 2; }

if [ -z "$OUT" ]; then
  OUT="./pgsql_profile_$(date +%Y%m%d_%H%M%S)_$$_${DURATION}s"
fi
echo "pgsql collect_profile"
echo "  pid=${PID_ARG} duration=${DURATION}s out=${OUT} execute=${EXECUTE} psql_sample=${PSQL_SAMPLE}"
echo "  纪律: 先预热再采样; on-CPU 火焰图看不到锁等待(等待事件/off-CPU 互补); LTO 构建注意归因伪影"
echo "------------------------------------------------------------"

if [ "$EXECUTE" -ne 1 ]; then
  echo "[DRY-RUN] 计划执行: perf record -F 999 -g -p ${PID_ARG} -- sleep ${DURATION}"
  command -v pidstat >/dev/null 2>&1 && echo "[DRY-RUN] 计划执行: pidstat -p ${PID_ARG} 1 ${DURATION}" || echo "[DRY-RUN] pidstat 不可用,将跳过"
  command -v mpstat >/dev/null 2>&1 && echo "[DRY-RUN] 计划执行: mpstat -P ALL 1 ${DURATION}" || echo "[DRY-RUN] mpstat 不可用,将跳过"
  [ "$PSQL_SAMPLE" -eq 1 ] && echo "[DRY-RUN] 计划执行: psql 等待事件快照 ×3"
  exit 0
fi

# --execute 才落盘：排他创建输出目录（已存在/并发竞态即拒绝），dry-run 无副作用
if ! mkdir "$OUT" 2>/dev/null; then
  echo "ERROR: 输出目录已存在或无法创建: ${OUT}——每轮独立目录，复用会截断上轮产物" >&2
  exit 3
fi

{
  date '+start=%F %T'
  echo "pid=${PID_ARG} duration=${DURATION}"
  if command -v "${PG_BIN:-/opt/pgsql/bin}/pg_config" >/dev/null 2>&1; then
    "${PG_BIN:-/opt/pgsql/bin}/pg_config" --version 2>/dev/null
    "${PG_BIN:-/opt/pgsql/bin}/pg_config" --configure 2>/dev/null
  fi
} > "${OUT}/env.txt"

PERF_STATUS=skip; PIDSTAT_STATUS=skip; MPSTAT_STATUS=skip; PSQL_STATUS=skip
COLLECT_PIDS=()

if command -v perf >/dev/null 2>&1; then
  PERF_STATUS=running
  ( perf record -F 999 -g -p "$PID_ARG" -o "${OUT}/perf.data" -- sleep "$DURATION" \
      > "${OUT}/perf_record.log" 2>&1 ) &
  COLLECT_PIDS+=($!)
else
  echo "[SKIP] 未找到 perf" >&2
fi

if command -v pidstat >/dev/null 2>&1; then
  PIDSTAT_STATUS=running
  ( pidstat -p "$PID_ARG" 1 "$DURATION" > "${OUT}/pidstat.txt" 2>&1 ) &
  COLLECT_PIDS+=($!)
else
  echo "[SKIP] 未找到 pidstat" >&2
fi

if command -v mpstat >/dev/null 2>&1; then
  MPSTAT_STATUS=running
  ( mpstat -P ALL 1 "$DURATION" > "${OUT}/mpstat.txt" 2>&1 ) &
  COLLECT_PIDS+=($!)
else
  echo "[SKIP] 未找到 mpstat" >&2
fi

if [ "$PSQL_SAMPLE" -eq 1 ]; then
  PSQL="${PG_BIN:-/opt/pgsql/bin}/psql"
  command -v "$PSQL" >/dev/null 2>&1 || PSQL="$(command -v psql || true)"
  if [ -n "$PSQL" ]; then
    PSQL_STATUS=running
    (
      # 按同一截止时间调度三轮快照（与 perf/pidstat 窗口对齐）：duration 很小
      # （如 1s）时后续轮越过截止即跳过并记录，不让采样器跑出窗口
      end=$(( $(date +%s) + DURATION ))
      for i in 1 2 3; do
        if [ "$i" -gt 1 ]; then
          now=$(date +%s)
          if [ "$now" -ge "$end" ]; then
            echo "psql_sample_$i: past window deadline, skipped" >> "${OUT}/.psql_note"
            break
          fi
          left=$(( end - now ))
          gap=$(( DURATION / 4 + 1 ))
          # 间隔取「均分间隔」与「剩余时间」的较小者，末轮不越过截止
          [ "$left" -lt "$gap" ] && gap="$left"
          sleep "$gap"
        fi
        remain=$(( end - $(date +%s) )); [ "$remain" -lt 1 ] && remain=1
        # 连接与查询双重约束（-w 只禁口令提示，不防连接/查询停滞）：
        # 外层 timeout 到期回收停滞子进程；PGCONNECT_TIMEOUT 约束建连；
        # statement_timeout 约束查询本身——都按剩余窗口取值
        # now() 为语句级固定值、可作分组外列；与 --pid/--pg-pattern 目标核属同一实例（同连接）
        timeout "$remain" env PGCONNECT_TIMEOUT="$remain" \
          "$PSQL" -w -h "${PGHOST:-127.0.0.1}" -p "${PG_PORT:-5432}" -U "${PGUSER:-postgres}" \
                -d "${DBNAME:-sbtest}" -X -qAt -c \
          "SET statement_timeout TO '${remain}s';
           SELECT now(), coalesce(wait_event_type,'-'), coalesce(wait_event,'-'), count(*)
           FROM pg_stat_activity
           WHERE state='active' AND pid <> pg_backend_pid()
             AND datname = current_database()
           GROUP BY 2,3;" >> "${OUT}/wait_snapshot.txt" 2> "${OUT}/wait_stderr_${i}.txt"
        rc=$?
        [ "$rc" -eq 0 ] || { echo "psql_sample_$i rc=$rc" >> "${OUT}/.psql_fail"; }
      done
    ) &
    COLLECT_PIDS+=($!)
  else
    echo "[SKIP] 未找到 psql，跳过等待事件快照" >&2
  fi
fi

for cpid in "${COLLECT_PIDS[@]}"; do
  wait "$cpid" 2>/dev/null
done
date '+end=%F %T' >> "${OUT}/window.txt"

RC=0
if [ "$PERF_STATUS" = "running" ]; then
  if [ -s "${OUT}/perf.data" ]; then
    if perf script -i "${OUT}/perf.data" > "${OUT}/perf.unfold" 2> "${OUT}/perf_script.log"; then
      PERF_STATUS=ok
      if [ -n "$FG_DIR" ] && [ -x "${FG_DIR}/stackcollapse-perf.pl" ]; then
        "${FG_DIR}/stackcollapse-perf.pl" "${OUT}/perf.unfold" > "${OUT}/perf.folded" 2>/dev/null || true
        if [ -x "${FG_DIR}/flamegraph.pl" ] && [ -s "${OUT}/perf.folded" ]; then
          "${FG_DIR}/flamegraph.pl" "${OUT}/perf.folded" > "${OUT}/flamegraph.svg" 2>/dev/null || true
        fi
      fi
    else
      echo "[FAIL] perf script 失败，见 ${OUT}/perf_script.log" >&2
      PERF_STATUS=fail
    fi
  else
    echo "[FAIL] perf record 未产生样本（权限/paranoid），见 ${OUT}/perf_record.log" >&2
    PERF_STATUS=fail
  fi
fi
if [ "$PIDSTAT_STATUS" = "running" ]; then
  [ -s "${OUT}/pidstat.txt" ] && PIDSTAT_STATUS=ok || { PIDSTAT_STATUS=fail; echo "[FAIL] pidstat 无样本" >&2; }
fi
if [ "$MPSTAT_STATUS" = "running" ]; then
  [ -s "${OUT}/mpstat.txt" ] && MPSTAT_STATUS=ok || { MPSTAT_STATUS=fail; echo "[FAIL] mpstat 无样本" >&2; }
fi
if [ "$PSQL_STATUS" = "running" ]; then
  if [ -e "${OUT}/.psql_fail" ]; then
    rm -f "${OUT}/.psql_fail"
    PSQL_STATUS=fail
    echo "[FAIL] 等待事件快照 psql 调用失败（见 wait_stderr_*.txt；认证/连接类错误不算无样本）" >&2
  elif [ -s "${OUT}/wait_snapshot.txt" ]; then
    PSQL_STATUS=ok
  else
    PSQL_STATUS=fail
    echo "[FAIL] 等待事件快照无样本（连接成功但零输出？核对采样时机与可见性前提）" >&2
  fi
fi

{
  echo "perf=${PERF_STATUS}"
  echo "pidstat=${PIDSTAT_STATUS}"
  echo "mpstat=${MPSTAT_STATUS}"
  echo "psql_wait=${PSQL_STATUS}"
} > "${OUT}/summary.txt"

# 至少一个采集器产生有效样本：全 skip（未装 perf/sysstat 且未启用 SQL 采样）
# 的空跑不能以 0 结束——本轮没有任何数据可供判读
if [ "$PERF_STATUS" != "ok" ] && [ "$PIDSTAT_STATUS" != "ok" ] \
   && [ "$MPSTAT_STATUS" != "ok" ] && [ "$PSQL_STATUS" != "ok" ]; then
  echo "no_data=1 (no collector produced samples; install perf/sysstat or enable --psql-sample)" >> "${OUT}/summary.txt"
  echo "[FAIL] 本轮无任何有效样本（全 skip），无数据可判读" >&2
  RC=1
fi

for st in "$PERF_STATUS" "$PIDSTAT_STATUS" "$MPSTAT_STATUS" "$PSQL_STATUS"; do
  [ "$st" = "fail" ] && RC=1
done

echo "------------------------------------------------------------"
if [ "$RC" -eq 0 ]; then
  echo "完成：${OUT}"
else
  echo "完成（存在失败项，见 ${OUT}/summary.txt）：${OUT}"
fi
ls -lh "$OUT" 2>/dev/null | sed 's/^/  /'
exit "$RC"
