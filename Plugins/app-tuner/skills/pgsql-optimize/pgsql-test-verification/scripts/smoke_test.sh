#!/usr/bin/env bash
# smoke_test.sh — PostgreSQL 只读功能冒烟
#
# 分节独立判定：版本 / 扩展 / 数据计数 / 连接与设置 / 活跃等待事件异常。
# 全部只读（SELECT）；口令经 PGPASSWORD 环境变量或 ~/.pgpass，不进 argv。
# 存在 FAIL 时退出码非零（0=全过；1=有 FAIL；2=用法/连接失败）。
#
# 用法:
#   PGHOST=127.0.0.1 PG_PORT=5432 PGUSER=postgres DBNAME=sbtest \
#     EXPECT_VERSION=17.5 bash smoke_test.sh
#
# 可选环境变量:
#   EXPECT_VERSION    期望版本子串（如 17.5；空则跳过版本断言）
#   EXPECT_EXTENSIONS 逗号分隔的期望扩展名（如 pg_stat_statements,pg_buffercache；空则跳过）
#   COUNT_TABLES      逗号分隔的 表:期望行数（如 sbtest1:10000000）；空则跳过对账
#   WAIT_LOCK_MAX     活跃会话中 Lock 类等待的最大样本数，默认 5（仅采样一次）

set -u

PGHOST="${PGHOST:-127.0.0.1}"
PG_PORT="${PG_PORT:-5432}"
PGUSER="${PGUSER:-postgres}"
DBNAME="${DBNAME:-sbtest}"
PG_BIN="${PG_BIN:-/opt/pgsql/bin}"   # 与 SKILL.md 入口变量表的默认一致（否则按默认前缀
                                     # 安装且未改 PATH 时，已安装的 psql 会被报缺失）
EXPECT_VERSION="${EXPECT_VERSION:-}"
EXPECT_EXTENSIONS="${EXPECT_EXTENSIONS:-}"
COUNT_TABLES="${COUNT_TABLES:-}"
WAIT_LOCK_MAX="${WAIT_LOCK_MAX:-5}"

PSQL="${PG_BIN}/psql"
command -v "$PSQL" >/dev/null 2>&1 || PSQL="$(command -v psql || true)"
[ -n "$PSQL" ] || { echo "ERROR: 找不到 psql（设 PG_BIN 指向安装 bin 目录）" >&2; exit 2; }

# ---- 校验参数类型（表:行数 形态）----
for spec in ${COUNT_TABLES//,/ }; do
  case "$spec" in "") ;; *[!A-Za-z0-9_.:]*|*::*|::) echo "ERROR: COUNT_TABLES 条目非法: $spec" >&2; exit 2 ;; esac
done
case "$PG_PORT" in ''|*[!0-9]*) echo "ERROR: PG_PORT 须为数字: ${PG_PORT}" >&2; exit 2 ;; esac
case "$WAIT_LOCK_MAX" in ''|*[!0-9]*) echo "ERROR: WAIT_LOCK_MAX 须为非负整数" >&2; exit 2 ;; esac

PASS_CNT=0; FAIL_CNT=0
pass() { PASS_CNT=$((PASS_CNT+1)); echo "[PASS] $*"; }
fail() { FAIL_CNT=$((FAIL_CNT+1)); echo "[FAIL] $*"; }

# 每次调用都是独立会话：-d 每次都要带（漏了会落到默认库误判）
q() { "$PSQL" -h "$PGHOST" -p "$PG_PORT" -U "$PGUSER" -d "$DBNAME" -X -qAt -v ON_ERROR_STOP=1 -c "$1" 2>&1; }

# ---- 1) 连接与版本 ----
v=$(q 'SELECT version();') || { echo "ERROR: 连接失败：$v" >&2; exit 2; }
pass "psql 连接成功（${PGHOST}:${PG_PORT}/${DBNAME}）"
if [ -n "$EXPECT_VERSION" ]; then
  case "$v" in *"${EXPECT_VERSION}"*) pass "版本匹配：${EXPECT_VERSION}" ;; *) fail "版本不匹配：期望含 ${EXPECT_VERSION}，实际：$(printf '%s' "$v" | head -c 60)" ;; esac
else
  echo "[SKIP] 未设置 EXPECT_VERSION，跳过版本断言"
fi

# ---- 2) 扩展 ----
if [ -n "$EXPECT_EXTENSIONS" ]; then
  got=$(q "SELECT string_agg(extname, ',' ORDER BY extname) FROM pg_extension;") \
    || { fail "pg_extension 查询失败：$got"; got=""; }
  for ext in ${EXPECT_EXTENSIONS//,/ }; do
    case ",${got}," in *",${ext},"*) pass "扩展存在: ${ext}" ;; *) fail "扩展缺失: ${ext}" ;; esac
  done
else
  echo "[SKIP] 未设置 EXPECT_EXTENSIONS，跳过扩展断言"
fi

# ---- 3) 数据计数对账 ----
for spec in ${COUNT_TABLES//,/ }; do
  [ -z "$spec" ] && continue
  tbl="${spec%%:*}"; want="${spec##*:}"
  case "$tbl" in *[!A-Za-z0-9_.]*) fail "表名非法: $tbl"; continue ;; esac
  case "$want" in ''|*[!0-9]*) fail "期望行数非数字: $spec"; continue ;; esac
  got=$(q "SELECT count(*) FROM ${tbl};") || { fail "计数失败 ${tbl}: $got"; continue; }
  if [ "$got" = "$want" ]; then
    pass "计数一致: ${tbl}=${got}"
  else
    fail "计数不一致: ${tbl} 实际=${got} 期望=${want}"
  fi
done
[ -n "$COUNT_TABLES" ] || echo "[SKIP] 未设置 COUNT_TABLES，跳过对账"

# ---- 4) 一致性与设置可读 ----
sb=$(q "SELECT current_setting('shared_buffers');") || fail "设置读取失败：$sb"
[ -n "$sb" ] && pass "设置可读：shared_buffers=${sb}"
one=$(q 'SELECT 1;') || fail "SELECT 1 失败：$one"
[ "$one" = "1" ] && pass "SELECT 1"

# ---- 4b) 写可用性（默认关：只读冒烟跑 4a 即可；写负载/写环境必须显式
# EXPECT_WRITABLE=1——可读热备同样能过版本/设置/SELECT 1，写压测连错备库时在此拦截）----
rec=$(q "SELECT pg_is_in_recovery();")
ro=$(q "SHOW transaction_read_only;")
if [ "${EXPECT_WRITABLE:-0}" = "1" ]; then
  if [ "$rec" = "f" ] && [ "$ro" = "off" ]; then
    pass "目标可写（pg_is_in_recovery=f、transaction_read_only=off）"
  else
    fail "目标是只读/热备（in_recovery=${rec:-?}、read_only=${ro:-?}）——写负载连错了实例"
  fi
else
  [ -n "$rec" ] && pass "pg_is_in_recovery=${rec}（未要求可写，仅记录）"
fi

# ---- 5) 活跃等待事件异常（单次采样）----
we=$(q "SELECT coalesce(wait_event_type,'-'), coalesce(wait_event,'-'), count(*)
        FROM pg_stat_activity WHERE state='active' AND pid <> pg_backend_pid()
        GROUP BY 1,2 ORDER BY 3 DESC;") || fail "pg_stat_activity 查询失败：$we"
if printf '%s' "$we" | awk -F'|' -v max="$WAIT_LOCK_MAX" '
      $1 ~ /^Lock/ { s+=$3 } END { exit (s+0 > max ? 0 : 1) }'; then
  lockn=$(printf '%s' "$we" | awk -F'|' '$1 ~ /^Lock/ { s+=$3 } END { print s+0 }')
  fail "Lock 类活跃等待 ${lockn} 超过阈值 ${WAIT_LOCK_MAX}（采样时刻）：${we}"
else
  pass "活跃等待无 Lock 异常（阈值 ${WAIT_LOCK_MAX}）"
fi

echo "------------------------------------------------------------"
echo "Summary: PASS=${PASS_CNT} FAIL=${FAIL_CNT}"
[ "$FAIL_CNT" -eq 0 ] && exit 0 || exit 1
