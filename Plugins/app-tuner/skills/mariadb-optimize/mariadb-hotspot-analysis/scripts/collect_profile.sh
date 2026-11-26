#!/usr/bin/env bash

# MariaDB 只读热点采集。默认 dry-run（只打印计划），--execute 才真正采集。
#
# 用法：
# bash scripts/collect_profile.sh                 # 打印计划
# bash scripts/collect_profile.sh --execute       # 采集到 ./mariadb-profile-<ts>/
# bash scripts/collect_profile.sh --execute --pid 12345 --seconds 30 --out /data/prof --port 3307
#
# 只读：不改配置/不重启/不写数据库、不启用监控计数器；仅写输出目录（umask 077 + chmod 700）。
# 默认不记录 SQL 原文：INNODB_TRX 只取必要字段，死锁快照中的语句行被过滤。
# 每次运行独占一个目录：--out 非空即拒绝，避免把上一轮产物与本轮拼接。

set -u
set -o pipefail
umask 077

EXECUTE=0
PID=""
SECONDS_WIN=20
OUT=""
PORT=""
SOCKET="${SOCKET:-}"
MARIADB_BIN="${MARIADB_BIN:-mariadb}"
MARIADB_DEFAULTS_FILE="${MARIADB_DEFAULTS_FILE:-}"
# perf 栈展开：默认空 = -g（frame pointer）；未保留 fp 时可设 --call-graph dwarf
PERF_CALLGRAPH="${PERF_CALLGRAPH:-}"

usage() {
  cat <<'EOF'
用法: collect_profile.sh [--execute] [--pid PID] [--seconds N] [--out DIR] [--port PORT]

--execute        真正执行采集（默认只打印计划）
--pid PID        mariadbd 进程号（默认自动发现）
--seconds N      采样窗口秒数（默认 20，1-600）
--out DIR        输出目录（必须不存在或为空；默认 ./mariadb-profile-<timestamp>）
--port PORT      MariaDB 客户端端口（多实例区分；默认走 socket/defaults）
-h, --help       显示帮助

环境变量:
SOCKET / MARIADB_BIN / MARIADB_DEFAULTS_FILE  用于执行只读 SQL 采集
FLAMEGRAPH_DIR   FlameGraph 脚本目录（含 stackcollapse-perf.pl/flamegraph.pl）
PERF_CALLGRAPH   perf 展开方式；默认空（-g / frame pointer），可设 --call-graph dwarf

输出目录 700、文件 600；SQL 证据不含语句原文；每轮窗口内各采集器并发启动。
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --execute) EXECUTE=1 ;;
    --pid) shift; PID="${1:-}" ;;
    --seconds) shift; SECONDS_WIN="${1:-20}" ;;
    --out) shift; OUT="${1:-}" ;;
    --port) shift; PORT="${1:-}" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage; exit 2 ;;
  esac
  shift
done

have() { command -v "$1" >/dev/null 2>&1; }

# 输入校验：非法值按用法错误拒绝，不静默跑出无效窗口
int_ok() { printf '%s' "$1" | grep -qE '^[0-9]+$'; }
if ! int_ok "${SECONDS_WIN:-20}" || [ "${SECONDS_WIN:-20}" -lt 1 ] || [ "${SECONDS_WIN:-20}" -gt 600 ]; then
  echo "FAIL: --seconds 须为 1-600 的整数（当前：${SECONDS_WIN}）" >&2
  exit 2
fi
if [ -n "${PID:-}" ] && ! int_ok "${PID}"; then
  echo "FAIL: --pid 须为数字 PID（当前：${PID}）" >&2
  exit 2
fi
if [ -n "${PORT:-}" ] && { ! int_ok "${PORT}" || [ "${PORT}" -lt 1 ] || [ "${PORT}" -gt 65535 ]; }; then
  echo "FAIL: --port 须为 1-65535 的整数（当前：${PORT}）" >&2
  exit 2
fi

if [ -z "$PID" ]; then
  PID="$(pgrep -x mariadbd | head -1 || true)"
  [ -z "$PID" ] && PID="$(pgrep -x mysqld | head -1 || true)"
fi

TS="$(date +%Y%m%d_%H%M%S)"
OUT="${OUT:-./mariadb-profile-${TS}}"

SQL_ARGS=()
[ -n "$MARIADB_DEFAULTS_FILE" ] && SQL_ARGS+=(--defaults-extra-file="$MARIADB_DEFAULTS_FILE")
[ -n "$PORT" ] && SQL_ARGS+=(--port="$PORT")
SQL_ARGS+=(--batch --skip-column-names)
if [ -n "$SOCKET" ]; then
  SQL_ARGS+=(--socket="$SOCKET")
fi

print_plan() {
  cat <<EOF
== MariaDB 热点采集计划（dry-run）==
PID            : ${PID:-<未发现 mariadbd>}
采样窗口       : ${SECONDS_WIN}s（perf/offcpu/pidstat/mpstat/iostat/sar 并发启动）
输出目录       : ${OUT}（不存在或空目录，权限 700）
连接端口       : ${PORT:-<默认 socket/defaults>}
perf 展开      : ${PERF_CALLGRAPH:-默认 -g（frame pointer）}
执行模式       : $([ "$EXECUTE" -eq 1 ] && echo execute || echo dry-run)

将执行（只读）：

1. 身份    : ps -L -p PID；mariadbd --version（若可定位）
2. on-CPU  : perf record -F 999 ${PERF_CALLGRAPH:--g} -p PID -- sleep ${SECONDS_WIN}
perf report --stdio；可选 FlameGraph 生成 SVG
3. off-CPU : offcputime -p PID ${SECONDS_WIN}（若安装 bcc）
4. 系统    : pidstat -p PID -t 1 ${SECONDS_WIN}
mpstat -P ALL 1 ${SECONDS_WIN}
iostat -x 1 ${SECONDS_WIN}
sar -n DEV 1 ${SECONDS_WIN}（net.txt，与其他通道一样登记状态）
5. 计数    : 窗口两端各读一次 SHOW GLOBAL STATUS / INNODB_METRICS（只读取值，不启用）
6. InnoDB  : SHOW ENGINE INNODB STATUS（过滤语句行）
INNODB_TRX 只取事务 ID/状态/等待时间等字段；INNODB_LOCK_WAITS
连接实例的 @@pid_file 必须与目标 --pid 一致，否则跳过 SQL 并标注原因

注意：

- 输出目录每轮独占，权限 700；证据包默认不含 SQL 原文。
- perf 需要调试符号；缺符号时先补 debug 包或 -g 编译且不 strip。
- 采样期间不要改配置/数据；先 warmup 再采样。
- 不默认启用 PFS/INNODB_METRICS；开关需在 A/B 两腿一致。
EOF
}

if [ "$EXECUTE" -eq 0 ]; then
  print_plan
  echo
  echo "未执行。确认无误后加 --execute。"
  exit 0
fi

if [ -z "$PID" ]; then
  echo "FAIL: 未发现 mariadbd/mysqld 进程，请用 --pid 指定" >&2
  exit 1
fi

# 每轮独占输出目录：拒绝复用非空目录，避免新旧证据拼在同一份报告里
if [ -e "$OUT" ]; then
  if [ ! -d "$OUT" ] || [ ! -r "$OUT" ] || [ ! -w "$OUT" ]; then
    echo "FAIL: --out 不是可读写的目录: $OUT" >&2
    exit 2
  fi
  if [ -n "$(ls -A "$OUT" 2>/dev/null)" ]; then
    echo "FAIL: --out 非空，拒绝复用（会把上一轮产物与本轮拼接）: $OUT；请换新目录或清空后重试" >&2
    exit 2
  fi
else
  mkdir -p "$OUT" || { echo "FAIL: 无法创建输出目录 $OUT" >&2; exit 1; }
fi
chmod 700 "$OUT" || { echo "FAIL: 无法限制输出目录权限 700: $OUT" >&2; exit 1; }
echo "输出目录（权限 700）: $OUT"

# 通道结果登记：命令退出 0 且产出有效数据才计入 VALID
CH_STATUS_FILE="$OUT/.channel_status"
: > "$CH_STATUS_FILE" || exit 1
record_status() { printf '%s\t%s\n' "$1" "$2" >> "$CH_STATUS_FILE"; }
SQL_CHANNELS=(innodb_status.txt innodb_status_vars.txt innodb_metrics.txt innodb_trx.txt lock_waits.txt pfs_flag.txt)
SQL_OK=0
SQL_SKIP_REASON=""
METRIC_FLAG=""

echo "[1/5] 身份与线程"
{
  echo "== date =="; date
  echo "== uname =="; uname -a
  echo "== pid =="; echo "$PID"
  echo "== threads =="; ps -L -p "$PID" -o tid,comm,psr,pcpu,stat 2>&1 | sort -k4 -nr
  echo "== numa =="; numastat -p "$PID" 2>&1 || true
} > "$OUT/identity.txt" 2>&1
echo "  -> $OUT/identity.txt"

# 连接实例校验：perf 按 --pid 采样，SQL 必须连到同一实例
if ! have "$MARIADB_BIN"; then
  SQL_SKIP_REASON="未找到客户端 $MARIADB_BIN"
else
  CONN_PID_FILE="$("$MARIADB_BIN" "${SQL_ARGS[@]}" -e "SELECT @@pid_file" 2>/dev/null | head -1)"
  if [ -z "$CONN_PID_FILE" ]; then
    SQL_SKIP_REASON="无法从连接实例读取 @@pid_file（连接失败或无权限）"
  elif [ ! -r "$CONN_PID_FILE" ]; then
    SQL_SKIP_REASON="连接实例的 pid_file=$CONN_PID_FILE 不可读"
  else
    CONN_PID="$(head -n 1 "$CONN_PID_FILE" 2>/dev/null | tr -d '[:space:]')"
    if ! int_ok "${CONN_PID:-}"; then
      SQL_SKIP_REASON="pid_file=$CONN_PID_FILE 的内容不是数字 PID（当前：${CONN_PID:-空}）"
    elif [ "$CONN_PID" != "$PID" ]; then
      SQL_SKIP_REASON="连接实例 pid_file=$CONN_PID_FILE 的 PID=$CONN_PID 与目标 PID=$PID 不一致；请用 --socket/--port/--defaults-extra-file 指向目标实例"
    fi
  fi
fi

if [ -n "$SQL_SKIP_REASON" ]; then
  {
    echo "INFO: 本轮跳过全部 SQL 采集（不产出 SQL 证据）"
    echo "原因: $SQL_SKIP_REASON"
  } > "$OUT/sql_skip.txt"
  for f in "${SQL_CHANNELS[@]}"; do record_status "$f" skip; done
  echo "WARN: 跳过 SQL 采集：$SQL_SKIP_REASON" >&2
else
  SQL_OK=1
  # INNODB_METRICS：10.11 用 ENABLED，旧版可能仍是 STATUS，按实际列分支
  METRIC_COLS="$("$MARIADB_BIN" "${SQL_ARGS[@]}" -e "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA='information_schema' AND TABLE_NAME='INNODB_METRICS' AND COLUMN_NAME IN ('ENABLED','STATUS')" 2>/dev/null | tr '\n' ' ')"
  case " $METRIC_COLS " in
    *ENABLED*) METRIC_FLAG=ENABLED ;;
    *STATUS*) METRIC_FLAG=STATUS ;;
    *) echo "WARN: 未识别 INNODB_METRICS 的 ENABLED/STATUS 字段，跳过指标采集" >&2 ;;
  esac
fi

# 窗口两端各读一次累计计数（只读快照，不启用任何计数器）
snap_counters() {
  local edge="$1" col
  if [ "$SQL_OK" -ne 1 ]; then
    echo "INFO: SQL 不可用（${SQL_SKIP_REASON}），跳过 ${edge} 累计计数快照" > "$OUT/status.${edge}.skip"
    return 0
  fi
  if ! "$MARIADB_BIN" "${SQL_ARGS[@]}" -e "SHOW GLOBAL STATUS" > "$OUT/status.${edge}" 2>&1; then
    echo "WARN: ${edge} SHOW GLOBAL STATUS 失败（见 status.${edge}）" >&2
  fi
  if [ -n "$METRIC_FLAG" ]; then
    col="$METRIC_FLAG"
    if ! "$MARIADB_BIN" "${SQL_ARGS[@]}" -e "SELECT NAME,SUBSYSTEM,$col,COUNT FROM information_schema.INNODB_METRICS ORDER BY SUBSYSTEM,NAME" > "$OUT/innodb_metrics.${edge}" 2>&1; then
      echo "WARN: ${edge} INNODB_METRICS 快照失败（见 innodb_metrics.${edge}）" >&2
    fi
  else
    echo "INFO: 未识别 INNODB_METRICS 状态字段（10.11=ENABLED，旧版=STATUS），跳过 ${edge} 指标快照" > "$OUT/innodb_metrics.${edge}.skip"
  fi
}

wait_channel() {
  local name="$1" bg="$2"
  [ -n "$bg" ] || { record_status "$name" skip; return 0; }
  if wait "$bg"; then record_status "$name" ok; else record_status "$name" fail; fi
}

COUNTER_BEFORE_TS="$(date '+%Y-%m-%d %H:%M:%S')"
snap_counters before
START_TS="$(date '+%Y-%m-%d %H:%M:%S')"
START_EPOCH="$(date +%s)"
echo "[2/5] 并发采样窗口（${SECONDS_WIN}s，起 ${START_TS}）"

PERF_BG=""; OFFCPU_BG=""; PIDSTAT_BG=""; MPSTAT_BG=""; IOSTAT_BG=""; SAR_BG=""
if have perf; then
  # 默认 -g（frame pointer），与 SKILL.md 纪律及 profiling-playbook.md §2 一致；
  # 未保留 fp 时用 PERF_CALLGRAPH='--call-graph dwarf' 切换，仅接受受控值。
  PERF_ARGS=(-F 999 -p "$PID" -o "$OUT/perf.data")
  case "$PERF_CALLGRAPH" in
    "") PERF_ARGS+=(-g) ;;
    "--call-graph dwarf") PERF_ARGS+=(--call-graph dwarf) ;;
    "--call-graph fp") PERF_ARGS+=(--call-graph fp) ;;
    *)
      echo "WARN: 忽略非法 PERF_CALLGRAPH='${PERF_CALLGRAPH}'，按默认 -g 采集" >&2
      PERF_ARGS+=(-g) ;;
  esac
  perf record "${PERF_ARGS[@]}" -- sleep "$SECONDS_WIN" \
    > "$OUT/perf_record.log" 2>&1 & PERF_BG=$!
else
  echo "WARN: 无 perf，跳过 on-CPU" | tee "$OUT/perf_record.log"
fi

OFFCPU="/usr/share/bcc/tools/offcputime"
if [ -x "$OFFCPU" ]; then
  "$OFFCPU" -p "$PID" "$SECONDS_WIN" > "$OUT/offcpu.txt" 2>&1 & OFFCPU_BG=$!
else
  echo "INFO: 未安装 bcc offcputime，跳过" > "$OUT/offcpu.txt"
fi

if have pidstat; then pidstat -p "$PID" -t 1 "$SECONDS_WIN" > "$OUT/pidstat_t.txt" 2>&1 & PIDSTAT_BG=$!; fi
if have mpstat;  then mpstat -P ALL 1 "$SECONDS_WIN"  > "$OUT/mpstat.txt" 2>&1 & MPSTAT_BG=$!; fi
if have iostat;  then iostat -x 1 "$SECONDS_WIN"      > "$OUT/iostat.txt" 2>&1 & IOSTAT_BG=$!; fi
if have sar; then
  sar -n DEV 1 "$SECONDS_WIN" > "$OUT/net.txt" 2>&1 & SAR_BG=$!
else
  echo "INFO: 未安装 sar，跳过网络对照" > "$OUT/net.txt"
fi

echo "  等待各采集器同时结束..."
wait_channel perf.data "$PERF_BG"
wait_channel offcpu.txt "$OFFCPU_BG"
wait_channel pidstat_t.txt "$PIDSTAT_BG"
wait_channel mpstat.txt "$MPSTAT_BG"
wait_channel iostat.txt "$IOSTAT_BG"
wait_channel net.txt "$SAR_BG"

END_TS="$(date '+%Y-%m-%d %H:%M:%S')"
END_EPOCH="$(date +%s)"
snap_counters after
COUNTER_AFTER_TS="$(date '+%Y-%m-%d %H:%M:%S')"
{
  echo "采集起: ${START_TS} (epoch ${START_EPOCH})"
  echo "采集止: ${END_TS} (epoch ${END_EPOCH})"
  echo "计数前快照: ${COUNTER_BEFORE_TS}"
  echo "计数后快照: ${COUNTER_AFTER_TS}"
  echo "采样窗口: ${SECONDS_WIN}s；perf/offcpu/pidstat/mpstat/iostat/sar 并发启动"
  echo "累计计数: status.before/status.after；innodb_metrics.before/innodb_metrics.after（SQL 可用时）"
  echo "增量 = after - before；按两个快照之间的实际秒数归一化，勿用时点值当窗口值"
} > "$OUT/window.txt"

echo "[3/5] on-CPU 报告与火焰图"
if [ -s "$OUT/perf.data" ]; then
  perf report -i "$OUT/perf.data" -g graph,0.5,callee --stdio \
    > "$OUT/perf_report.txt" 2>/dev/null || true
  perf script -i "$OUT/perf.data" > "$OUT/perf.script" 2>/dev/null || true
  FG="${FLAMEGRAPH_DIR:-}"
  if [ -n "$FG" ] && [ -x "$FG/stackcollapse-perf.pl" ] && [ -x "$FG/flamegraph.pl" ]; then
    "$FG/stackcollapse-perf.pl" "$OUT/perf.script" > "$OUT/oncpu.folded" 2>/dev/null || true
    [ -s "$OUT/oncpu.folded" ] &&
    "$FG/flamegraph.pl" --title "mariadbd on-CPU" "$OUT/oncpu.folded" > "$OUT/oncpu.svg" 2>/dev/null || true
  else
    echo "INFO: 未设置 FLAMEGRAPH_DIR，跳过 SVG 生成" >> "$OUT/perf_record.log"
  fi
fi

echo "[4/5] InnoDB / SQL 证据"
run_sql() {
  local file="$1" sql="$2" filter="$3"
  if [ "$filter" = "deadlock" ]; then
    # 过滤 LATEST DETECTED DEADLOCK / TRANSACTIONS 段里跟随 "MySQL thread id" 的语句原文
    if "$MARIADB_BIN" "${SQL_ARGS[@]}" -e "$sql" 2>&1 | awk '
        /^[[:space:]]*MySQL thread id / { print; redact=1; next }
        redact && /^[[:space:]]*(\*\*\*|---)/ { redact=0 }
        redact && NF { print "  [SQL 语句已过滤]"; redact=0; next }
        { print }
      ' > "$OUT/$file"; then
      record_status "$file" ok
    else
      record_status "$file" fail
    fi
  else
    if "$MARIADB_BIN" "${SQL_ARGS[@]}" -e "$sql" > "$OUT/$file" 2>&1; then
      record_status "$file" ok
    else
      record_status "$file" fail
    fi
  fi
}

if [ "$SQL_OK" -eq 1 ]; then
  run_sql innodb_status.txt "SHOW ENGINE INNODB STATUS\G" deadlock
  run_sql innodb_status_vars.txt "SHOW GLOBAL STATUS LIKE 'Innodb_%'" plain
  # 不采集 TRX_QUERY / SELECT *：只保留事务 ID、状态、等待时间等必要字段
  run_sql innodb_trx.txt "SELECT TRX_ID,TRX_STATE,TRX_STARTED,TRX_WAIT_STARTED,TRX_MYSQL_THREAD_ID,TRX_WEIGHT FROM information_schema.INNODB_TRX ORDER BY TRX_STARTED" plain
  run_sql lock_waits.txt "SELECT * FROM information_schema.INNODB_LOCK_WAITS" plain
  run_sql pfs_flag.txt "SHOW VARIABLES LIKE 'performance_schema'" plain

  if [ -n "$METRIC_FLAG" ]; then
    col="$METRIC_FLAG"
    if "$MARIADB_BIN" "${SQL_ARGS[@]}" -e "SELECT NAME,SUBSYSTEM,$col,COUNT FROM information_schema.INNODB_METRICS ORDER BY SUBSYSTEM,NAME" > "$OUT/.metrics_all" 2>&1 &&
       ! grep -qE '^ERROR' "$OUT/.metrics_all"; then
      # 只记录已启用的计数器；未启用/缺项单列，默认不修改全局监控状态
      awk -F'\t' -v on="$OUT/innodb_metrics.txt" -v off="$OUT/innodb_metrics_missing.txt" '
        BEGIN { OFS="\t"; print "NAME","SUBSYSTEM","FLAG","COUNT" > on; print "NAME","SUBSYSTEM","FLAG" > off }
        tolower($3) ~ /^(yes|enabled|on|1)$/ { print > on; next }
        { print > off }
      ' "$OUT/.metrics_all"
      record_status innodb_metrics.txt ok
    else
      cp "$OUT/.metrics_all" "$OUT/innodb_metrics.txt" 2>/dev/null ||
        echo "WARN: INNODB_METRICS 查询失败" > "$OUT/innodb_metrics.txt"
      echo "INFO: 未读到已启用计数器；默认不启用模块，缺项不在此处补齐" >> "$OUT/innodb_metrics_missing.txt"
      record_status innodb_metrics.txt fail
    fi
    rm -f "$OUT/.metrics_all"
  else
    echo "INFO: 未识别 INNODB_METRICS 状态字段（10.11=ENABLED，旧版=STATUS），跳过" > "$OUT/innodb_metrics.txt"
    record_status innodb_metrics.txt skip
  fi
fi

echo "[5/5] 汇总"
# 每条通道都必须命令成功 + 有非占位数据；ERROR 行不算有效样本
VALID=0
while IFS=$'\t' read -r name st; do
  [ -n "$name" ] || continue
  [ "$st" = "ok" ] || continue
  [ -s "$OUT/$name" ] || continue
  if [ "$name" = "perf.data" ]; then
    VALID=$((VALID + 1))
  elif grep -qvE '^[[:space:]]*(INFO|WARN|FAIL|ERROR)([[:space:]:(]|$)|^(NAME|Variable_name)[[:space:]]' "$OUT/$name"; then
    VALID=$((VALID + 1))
  fi
done < "$CH_STATUS_FILE"

{
  echo "== 采集完成 =="
  date
  echo "窗口: ${SECONDS_WIN}s  PID: ${PID}"
  cat "$OUT/window.txt"
  echo "valid_channels=${VALID}"
  [ -f "$OUT/sql_skip.txt" ] && cat "$OUT/sql_skip.txt"
  ls -lh "$OUT"
} > "$OUT/README.txt" 2>&1

if [ "${VALID}" -eq 0 ]; then
  echo "no_data" > "$OUT/result.status"
  echo "FAIL: 本轮无任何有效样本（采集器缺失、命令失败或全部为占位/错误行），无数据可判读——证据包 $OUT" >&2
  exit 1
fi

echo "ok valid_channels=${VALID}" > "$OUT/result.status"
echo
echo "完成（有效样本通道 ${VALID} 项）。证据包：$OUT"
echo "下一步：结合 references/profiling-playbook.md 解读，并记录场景/并发/数据/md5。"
exit 0
