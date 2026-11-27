#!/usr/bin/env bash

# MariaDB 只读冒烟检查（不写入、不修改任何数据）。

#

# 用法：

# MARIADB_BIN=/opt/mariadb/10.11.16/bin/mariadb \
  # SOCKET=/data/mariadb/run/mysql.sock \
  # DB=bench SAMPLE_TABLE=sbtest1 EXPECT_MIN_ROWS=1000 \
  # bash scripts/smoke_test.sh

#

# 也可以用 TCP：

# HOST=127.0.0.1 PORT=3306 DB_USER=root MARIADB_DEFAULTS_FILE=/tmp/.my.cnf ...

#

# 凭据：优先用 MARIADB_DEFAULTS_FILE（权限 0600）；不要把密码写在命令行。

# 退出码：0 全部通过；1 存在 FAIL。

set -u

MARIADB_BIN="${MARIADB_BIN:-mariadb}"
SOCKET="${SOCKET:-}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-3306}"
if [ -z "${SOCKET:-}" ]; then
  case "${PORT}" in ''|*[!0-9]*) echo "FAIL  PORT 须为数字: ${PORT}" >&2; exit 1 ;; esac
  if [ "${PORT}" -lt 1 ] || [ "${PORT}" -gt 65535 ]; then
    echo "FAIL  PORT 超出合法范围 1-65535: ${PORT}" >&2
    exit 1
  fi
fi
DB_USER="${DB_USER:-}"
DB="${DB:-}"
SAMPLE_TABLE="${SAMPLE_TABLE:-}"
EXPECT_MIN_ROWS="${EXPECT_MIN_ROWS:-0}"
CHECK_REPLICATION="${CHECK_REPLICATION:-auto}"

PASS=0
FAIL=0

pass() { printf 'PASS  %s\n' "$*"; PASS=$((PASS + 1)); }
fail() { printf 'FAIL  %s\n' "$*"; FAIL=$((FAIL + 1)); }
info() { printf 'INFO  %s\n' "$*"; }
section() { printf '\n== %s ==\n' "$*"; }

# SQL 标识符只允许 [A-Za-z_][A-Za-z0-9_]*: 拒绝引号/反引号/空白/数字开头。
# 校验通过后标识符才可安全嵌入 SQL，杜绝双引号内反引号触发 Bash 命令替换。
valid_ident() {
  case "$1" in
    ''|*[!A-Za-z0-9_]*|[0-9]*) return 1 ;;
  esac
  return 0
}
# 字符串字面量转义：用单引号包裹并把内部的单引号翻倍。
sql_str() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/''/g")"; }

for _name in DB SAMPLE_TABLE; do
  _v="${!_name}"
  if [ -n "$_v" ] && ! valid_ident "$_v"; then
    fail "非法 SQL 标识符 ${_name}=${_v}（只允许字母/下划线开头，后接字母数字下划线）"
    echo "结果：FAIL=$FAIL（标识符非法，后续检查跳过）"
    exit 1
  fi
done
unset _name _v

ARGS=()
if [ -n "${MARIADB_DEFAULTS_FILE:-}" ]; then
  ARGS+=(--defaults-extra-file="${MARIADB_DEFAULTS_FILE}")
fi
ARGS+=(--batch --skip-column-names)
if [ -n "$SOCKET" ]; then
  ARGS+=(--socket="$SOCKET")
else
  ARGS+=(--host="$HOST" --port="$PORT" --protocol=tcp)
fi
[ -n "$DB_USER" ] && ARGS+=(--user="$DB_USER")
[ -n "$DB" ] && ARGS+=(--database="$DB")

run_sql() {
  "$MARIADB_BIN" "${ARGS[@]}" -e "$1" 2>&1
}

section "0. 连通性"

if OUT="$(run_sql 'SELECT 1')" && [ "$OUT" = "1" ]; then
  pass "可以连接数据库"
else
  fail "无法连接：$OUT"
  echo "结果：FAIL=$FAIL（连接失败，后续检查跳过）"
  exit 1
fi

section "1. 版本与基础配置"

VER="$(run_sql 'SELECT VERSION()')"
case "$VER" in
  10.11.*) pass "版本 $VER" ;;
  *) info "版本 $VER（非 10.11 基线，注意兼容性）" ;;
esac

ENGINE="$(run_sql 'SELECT @@default_storage_engine')"
[ "$ENGINE" = "InnoDB" ] && pass "默认引擎 InnoDB" || fail "默认引擎为 $ENGINE（预期 InnoDB）"

CHARSET="$(run_sql 'SELECT @@character_set_server')"
info "server charset=$CHARSET"
# 时区只做 info 展示，不作门禁判定；消除「冒烟覆盖时区」与脚本实际检查项的漂移。
info "time_zone=$(run_sql 'SELECT @@system_time_zone, @@time_zone' | tr '\t' '/')"

info "buffer_pool=$(run_sql 'SELECT @@innodb_buffer_pool_size')"
info "redo_size=$(run_sql 'SELECT @@innodb_log_file_size')"
info "flush_method=$(run_sql 'SELECT @@innodb_flush_method')"
info "flush_log_at_trx_commit=$(run_sql 'SELECT @@innodb_flush_log_at_trx_commit')"
info "sync_binlog=$(run_sql 'SELECT @@sync_binlog')"
info "performance_schema=$(run_sql 'SELECT @@performance_schema')"

section "2. 引擎与库表"

if run_sql 'SHOW ENGINES' | awk -F'\t' '$1=="InnoDB" && $2=="DEFAULT"{found=1} END{exit !found}'; then
  pass "SHOW ENGINES 中 InnoDB=DEFAULT"
else
  fail "SHOW ENGINES 未见 InnoDB=DEFAULT"
fi

if [ -n "$DB" ]; then
  if run_sql "SHOW DATABASES LIKE $(sql_str "$DB")" | grep -qx "$DB"; then
    pass "数据库存在：$DB"
  else
    fail "数据库不存在：$DB"
  fi
fi

if [ -n "$DB" ] && [ -n "$SAMPLE_TABLE" ]; then
  # 标识符已校验；反引号在双引号内写成 \` 才是字面量，不会触发命令替换。
  if run_sql "SHOW TABLES FROM \`${DB}\` LIKE $(sql_str "$SAMPLE_TABLE")" | grep -qx "$SAMPLE_TABLE"; then
    pass "表存在：$DB.$SAMPLE_TABLE"
    CNT="$(run_sql "SELECT COUNT(*) FROM \`${DB}\`.\`${SAMPLE_TABLE}\`")"
    if [ "${CNT:-0}" -ge "$EXPECT_MIN_ROWS" ] 2>/dev/null; then
      pass "行数 $CNT ≥ 期望 $EXPECT_MIN_ROWS"
    else
      fail "行数 $CNT < 期望 $EXPECT_MIN_ROWS"
    fi
    info "表结构："
    run_sql "SHOW CREATE TABLE \`${DB}\`.\`${SAMPLE_TABLE}\`" | head -n 40 || true
  else
    fail "表不存在：$DB.$SAMPLE_TABLE"
  fi
fi

section "3. 访问路径（只读）"

if [ -n "$DB" ] && [ -n "$SAMPLE_TABLE" ]; then
  ID_COL="$(run_sql "SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE
  WHERE TABLE_SCHEMA=$(sql_str "$DB") AND TABLE_NAME=$(sql_str "$SAMPLE_TABLE") AND CONSTRAINT_NAME='PRIMARY'
  ORDER BY ORDINAL_POSITION LIMIT 1")"
  if [ -n "$ID_COL" ] && ! valid_ident "$ID_COL"; then
    fail "主键列名非法：$ID_COL（跳过 EXPLAIN 检查）"
    ID_COL=""
  fi
  if [ -n "$ID_COL" ]; then
    pass "主键列：$ID_COL"
    # 先判退出码：EXPLAIN 失败时 PLAN 是错误信息，全文 grep 会把失败误判为通过。
    if PLAN="$(run_sql "EXPLAIN SELECT * FROM \`${DB}\`.\`${SAMPLE_TABLE}\` WHERE \`${ID_COL}\` = 1")"; then
      info "EXPLAIN 主键查询："
      printf '%s\n' "$PLAN"
      # 只解析 EXPLAIN 的 type 列（第 4 列）：表名/Extra 含 all 不再误报全表扫描。
      if printf '%s\n' "$PLAN" | awk -F'\t' '$4=="ALL"{found=1} END{exit !found}'; then
        fail "主键查询 type 列出现 ALL（全表扫描）"
      else
        pass "主键查询 type 列未出现 ALL"
      fi
    else
      fail "EXPLAIN 主键查询执行失败（退出码非 0，不判通过）"
    fi
    info "索引："
    run_sql "SHOW INDEX FROM \`${DB}\`.\`${SAMPLE_TABLE}\`" | head -n 20 || true
  else
    info "表无主键，跳过主键查询检查"
  fi
else
  info "未指定 DB/SAMPLE_TABLE，跳过访问路径检查"
fi

section "4. 事务隔离与只读校验"

TX_ISO="$(run_sql 'SELECT @@tx_isolation')"
[ -n "$TX_ISO" ] && pass "隔离级别 $TX_ISO" || info "无法读取隔离级别"

READ_ONLY="$(run_sql 'SELECT @@read_only')"
info "read_only=$READ_ONLY"
# 写可用性验收（默认关：只读冒烟到 info 即可；需写验收显式 EXPECT_WRITABLE=1——
# SELECT 1 类只读验证不证明可写，写压测连错只读实例/从库时在此拦截）
if [ "${EXPECT_WRITABLE:-0}" = "1" ]; then
  if [ "$READ_ONLY" = "0" ]; then
    pass "写可用性：read_only=OFF"
  else
    fail "写可用性：read_only=${READ_ONLY}（目标为只读？写验收须指向可写实例）"
  fi
fi

section "5. 复制状态"

if [ "$CHECK_REPLICATION" != "off" ]; then
  # SHOW SLAVE STATUS 在 --batch --skip-column-names 下无列名，原按列名匹配的 awk 恒空，
  # 健康从库会误报 FAIL。改用 \G 垂直输出（不复用 run_sql 的 --skip-column-names）按行解析。
  SLAVE_ARGS=()
  [ -n "${MARIADB_DEFAULTS_FILE:-}" ] && SLAVE_ARGS+=(--defaults-extra-file="${MARIADB_DEFAULTS_FILE}")
  if [ -n "$SOCKET" ]; then
    SLAVE_ARGS+=(--socket="$SOCKET")
  else
    SLAVE_ARGS+=(--host="$HOST" --port="$PORT" --protocol=tcp)
  fi
  [ -n "$DB_USER" ] && SLAVE_ARGS+=(--user="$DB_USER")
  HAS_SLAVE="$("$MARIADB_BIN" "${SLAVE_ARGS[@]}" -e 'SHOW SLAVE STATUS\G' 2>&1)"
  if printf '%s\n' "$HAS_SLAVE" | grep -q 'Slave_IO_Running:'; then
    IO="$(printf '%s\n' "$HAS_SLAVE" | awk '/Slave_IO_Running:/{print $2; exit}')"
    SQLT="$(printf '%s\n' "$HAS_SLAVE" | awk '/Slave_SQL_Running:/{print $2; exit}')"
    [ "$IO" = "Yes" ] && pass "Slave_IO_Running=Yes" || fail "Slave_IO_Running=$IO"
    [ "$SQLT" = "Yes" ] && pass "Slave_SQL_Running=Yes" || fail "Slave_SQL_Running=$SQLT"
  else
    info "未配置从库（或未启用复制）"
  fi

  WSREP="$(run_sql "SHOW STATUS LIKE 'wsrep_ready'")"
  if [ -n "$WSREP" ]; then
    READY="$(printf '%s\n' "$WSREP" | awk -F'\t' '{print $2}')"
    [ "$READY" = "ON" ] && pass "wsrep_ready=ON" || fail "wsrep_ready=$READY"
  fi
fi

section "6. 错误日志提示"

info "请人工确认错误日志无 ERROR/InnoDB: Cannot/Aborting（脚本不读取日志路径）"

section "汇总"

printf 'PASS=%d FAIL=%d\n' "$PASS" "$FAIL"
if [ "$FAIL" -gt 0 ]; then
  echo "结果：不通过"
  exit 1
fi
echo "结果：通过"
exit 0
