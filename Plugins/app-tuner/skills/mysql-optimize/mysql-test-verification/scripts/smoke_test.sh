#!/usr/bin/env bash

# MySQL 功能冒烟：默认只读；--write-test 在隔离库做写入/回滚验证并自动清理。
#
# 用法：
#   MYSQL_SOCKET=/data/mysql/mysql.sock MYSQL_USER=root \
#     MYSQL_DEFAULTS_EXTRA_FILE="${HOME}/.my.cnf.smoke" \
#     bash scripts/smoke_test.sh            # 只读
#   # 需要写入验证时（隔离库，自动清理）：
#   MYSQL_SOCKET=/data/mysql/mysql.sock MYSQL_USER=root \
#     MYSQL_DEFAULTS_EXTRA_FILE="${HOME}/.my.cnf.smoke" \
#     bash scripts/smoke_test.sh --write-test
#
# 可选：MYSQL_HOST/MYSQL_PORT（不走 socket 时）、MYSQL_BIN（客户端路径）。
# 密码只从 defaults-extra-file 读取，不进 argv。
# 退出码：0=全部通过（含临时库清理）；1=存在失败（含清理失败）；2=参数错误。

set -u

MYSQL_SOCKET="${MYSQL_SOCKET:-}"
MYSQL_HOST="${MYSQL_HOST:-127.0.0.1}"
MYSQL_PORT="${MYSQL_PORT:-3306}"
MYSQL_USER="${MYSQL_USER:-root}"
MYSQL_DEFAULTS_EXTRA_FILE="${MYSQL_DEFAULTS_EXTRA_FILE:-}"
WRITE_TEST=0
for arg in "$@"; do
  case "$arg" in
    --write-test) WRITE_TEST=1 ;;
    -h|--help) echo "usage: $0 [--write-test]"; exit 0 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

fail=0
pass() { printf '[PASS] %s\n' "$*"; }
bad()  { printf '[FAIL] %s\n' "$*" >&2; fail=$((fail + 1)); }

MYSQL_BIN="${MYSQL_BIN:-mysql}"
if ! command -v "${MYSQL_BIN}" >/dev/null 2>&1; then
  echo "[FAIL] 找不到 mysql 客户端：${MYSQL_BIN}" >&2
  exit 1
fi

# MySQL 8.0 要求 --defaults-extra-file 出现在其它客户端选项之前。
MYSQL_ARGS=()
if [ -n "${MYSQL_DEFAULTS_EXTRA_FILE}" ]; then
  MYSQL_ARGS+=(--defaults-extra-file="${MYSQL_DEFAULTS_EXTRA_FILE}")
fi
MYSQL_ARGS+=(-u "${MYSQL_USER}" --connect-timeout=5)
if [ -n "${MYSQL_SOCKET}" ]; then
  MYSQL_ARGS+=(--socket="${MYSQL_SOCKET}")
else
  MYSQL_ARGS+=(-h "${MYSQL_HOST}" -P "${MYSQL_PORT}")
fi

# stderr 与 stdout 分离：诊断文字不得冒充查询结果（如 SELECT 1 的返回值）。
MYSQL_ERR_FILE="$(mktemp "${TMPDIR:-/tmp}/mysql-smoke-err.XXXXXX")" || {
  echo "[FAIL] 无法创建临时诊断文件" >&2
  exit 1
}
sql_err() { tr '\n' ' ' < "${MYSQL_ERR_FILE}" 2>/dev/null || true; }
q() {
  : > "${MYSQL_ERR_FILE}"
  "${MYSQL_BIN}" "${MYSQL_ARGS[@]}" -N -B -e "$1" 2>"${MYSQL_ERR_FILE}"
}

SMOKE_DB=""
DB_CREATED=0

drop_smoke_db() {
  local rc=0 left=""
  q "DROP DATABASE \`${SMOKE_DB}\`;" >/dev/null || rc=$?
  if [ "${rc}" -ne 0 ]; then
    bad "DROP DATABASE 失败（rc=${rc}）：$(sql_err)"
    return
  fi
  left="$(q "SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME='${SMOKE_DB}';")" || left=""
  if [ "${left}" = "0" ]; then
    pass "DROP DATABASE 清理完成"
  else
    bad "临时库未清理：${left}"
  fi
}

# 任何失败路径（含中途退出）都先清理临时库，再按 fail 计数决定退出码。
finish() {
  trap - EXIT
  if [ "${WRITE_TEST}" -eq 1 ] && [ "${DB_CREATED}" -eq 1 ]; then
    drop_smoke_db
  fi
  rm -f "${MYSQL_ERR_FILE}"
  if [ "${fail}" -eq 0 ]; then
    echo "== smoke test passed =="
    exit 0
  fi
  echo "== smoke test failed: ${fail} =="
  exit 1
}
trap finish EXIT

echo "== MySQL smoke test (read-only=$([ "${WRITE_TEST}" -eq 0 ] && echo yes || echo no)) =="

# 1. 连接与版本：以客户端退出码为准，不用 stderr 文本判定成功。
ver_rc=0
ver="$(q 'SELECT VERSION();')" || ver_rc=$?
if [ "${ver_rc}" -eq 0 ] && [ -n "${ver}" ]; then
  pass "连接成功，版本 ${ver}"
else
  bad "连接失败（rc=${ver_rc}）：$(sql_err) ${ver}"
  exit 1
fi

# 2. 基础视图
db_rc=0
db_count="$(q 'SELECT COUNT(*) FROM information_schema.SCHEMATA;')" || db_rc=$?
if [ "${db_rc}" -ne 0 ]; then
  bad "SHOW DATABASES 失败（rc=${db_rc}）：$(sql_err)"
else
  case "${db_count}" in
    ''|*[!0-9]*) bad "schema 数量异常：${db_count}" ;;
    *) pass "schema 数量 ${db_count}" ;;
  esac
fi

# 3. 关键参数与状态
bp="$(q "SHOW VARIABLES LIKE 'innodb_buffer_pool_size';")"
echo "[INFO] ${bp}"
psize="$(q "SHOW VARIABLES LIKE 'innodb_page_size';")"
echo "[INFO] ${psize}"
sqlmode="$(q 'SELECT @@sql_mode;')"
echo "[INFO] sql_mode=${sqlmode}"
charset="$(q 'SELECT @@character_set_server;')"
echo "[INFO] character_set_server=${charset}"
readonly="$(q 'SELECT @@read_only;')"
echo "[INFO] read_only=${readonly}"

hit="$(q "SELECT ROUND(100*(1-IFNULL(
(SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_buffer_pool_reads')/
NULLIF((SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Innodb_buffer_pool_read_requests'),0),1)),2);")"
case "${hit}" in
  ''|*[!0-9.]*) echo "[INFO] buffer pool 命中率不可用（performance_schema 可能关闭）" ;;
  *) pass "Buffer Pool 命中率 ${hit}%" ;;
esac

# mysql 客户端的 -N 会抑制 \G 纵向输出的列名标签（"Status:"/"Info:"），且
# "INNODB MONITOR OUTPUT" 位于输出第 6 行左右：head -n 5 + grep STATUS 在健康
# MySQL 8.0 上也恒假红。此查询单独不加 -N，以客户端退出码 + 输出内容判定。
: > "${MYSQL_ERR_FILE}"
status_rc=0
status="$("${MYSQL_BIN}" "${MYSQL_ARGS[@]}" -B -e 'SHOW ENGINE INNODB STATUS\G' 2>"${MYSQL_ERR_FILE}")" || status_rc=$?
if [ "${status_rc}" -eq 0 ] && printf '%s' "${status}" | grep -q 'INNODB MONITOR OUTPUT'; then
  pass "SHOW ENGINE INNODB STATUS 可读"
else
  bad "SHOW ENGINE INNODB STATUS 不可读（rc=${status_rc}）：$(sql_err)"
fi

# 4. 只读查询：同时校验客户端退出码和实际返回值，错误信息单独保留。
probe_rc=0
probe="$(q 'SELECT 1;')" || probe_rc=$?
if [ "${probe_rc}" -eq 0 ] && [ "${probe}" = "1" ]; then
  pass "SELECT 1"
else
  bad "SELECT 1 失败（rc=${probe_rc}，返回值=${probe}，stderr=$(sql_err)）"
fi

# 5. 写入验证（隔离库；DROP DATABASE 由 EXIT trap 统一执行）。
if [ "${WRITE_TEST}" -eq 1 ]; then
  SMOKE_DB="mtr_smoke_$$"
  DBQ="\`${SMOKE_DB}\`"
  T1="${DBQ}.t1"
  echo "[INFO] write-test db=${SMOKE_DB}"

  create_rc=0
  create_out="$(q "CREATE DATABASE ${DBQ} CHARACTER SET utf8mb4;")" || create_rc=$?
  if [ "${create_rc}" -ne 0 ]; then
    bad "CREATE DATABASE 失败（rc=${create_rc}）：$(sql_err)"
  else
    DB_CREATED=1
    pass "CREATE DATABASE ${SMOKE_DB}"

    ddl_rc=0
    q "CREATE TABLE ${T1} (id INT PRIMARY KEY AUTO_INCREMENT, v VARCHAR(32) NOT NULL, KEY k_v(v)) ENGINE=InnoDB CHARACTER SET utf8mb4;" >/dev/null || ddl_rc=$?
    if [ "${ddl_rc}" -eq 0 ]; then
      pass "CREATE TABLE"
    else
      bad "CREATE TABLE 失败（rc=${ddl_rc}）：$(sql_err)"
    fi

    ins_rc=0
    q "INSERT INTO ${T1}(v) VALUES ('a'),('b'),('c');" >/dev/null || ins_rc=$?
    if [ "${ins_rc}" -eq 0 ]; then
      pass "INSERT 3 行"
    else
      bad "INSERT 失败（rc=${ins_rc}）：$(sql_err)"
    fi

    cnt_rc=0
    cnt="$(q "SELECT COUNT(*) FROM ${T1};")" || cnt_rc=$?
    if [ "${cnt_rc}" -eq 0 ] && [ "${cnt}" = "3" ]; then
      pass "INSERT/SELECT 计数 3"
    else
      bad "计数异常（rc=${cnt_rc}）：${cnt}"
    fi

    # 三条语句必须在同一连接内执行，ROLLBACK 才能撤销该 UPDATE。
    tx_rc=0
    q "START TRANSACTION; UPDATE ${T1} SET v='z' WHERE id=1; ROLLBACK;" >/dev/null || tx_rc=$?
    v_rc=0
    v="$(q "SELECT v FROM ${T1} WHERE id=1;")" || v_rc=$?
    if [ "${tx_rc}" -eq 0 ] && [ "${v_rc}" -eq 0 ] && [ "${v}" = "a" ]; then
      pass "事务 ROLLBACK 生效"
    else
      bad "ROLLBACK 验证失败（tx_rc=${tx_rc}, read_rc=${v_rc}, v=${v}）：$(sql_err)"
    fi

    # 预期错误：客户端必须失败，且诊断须含 Duplicate entry；成功插入即判定缺陷。
    dup_rc=0
    dup_out="$(q "INSERT INTO ${T1}(id,v) VALUES (1,'dup');")" || dup_rc=$?
    if [ "${dup_rc}" -ne 0 ] && grep -qi 'duplicate entry' "${MYSQL_ERR_FILE}"; then
      pass "唯一键冲突符合预期（rc=${dup_rc}）"
    else
      bad "唯一键冲突未检出（rc=${dup_rc}，stdout=${dup_out}，stderr=$(sql_err)）"
    fi

    srt_rc=0
    srt="$(q "SELECT GROUP_CONCAT(v ORDER BY v) FROM ${T1};")" || srt_rc=$?
    if [ "${srt_rc}" -eq 0 ] && [ "${srt}" = "a,b,c" ]; then
      pass "ORDER BY/collation 可执行：${srt}"
    else
      bad "ORDER BY 失败（rc=${srt_rc}）：${srt}"
    fi
  fi
fi
