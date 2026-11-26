#!/usr/bin/env bash
# Isolated pgGraph smoke test.
# Creates a temporary database, installs the extension, builds a tiny graph,
# checks traverse/search, then drops the database. Existing databases are not
# modified. Requires CREATEDB privilege and an installed graph extension.
#
# Usage:
#   PGSQL_PREFIX=/opt/pgsql/18.3 PGHOST=localhost PGPORT=6666 PGUSER=postgres \
#     bash smoke_test.sh
set -u

PGSQL_PREFIX="${PGSQL_PREFIX:-}"
PGHOST="${PGHOST:-localhost}"
PGPORT="${PGPORT:-5432}"
PGUSER="${PGUSER:-postgres}"
DB="pggraph_smoke_${BASHPID}_${RANDOM}"   # 随机后缀降低重名碰撞；只有建库成功才允许清理（CREATED 标志）。须用 ${} 显式分界：$$$_RANDOM 会被解析成 $$ + 未定义变量 _RANDOM（set -u 下启动即中止；$$RANDOM 则拼成 PID+字面量 RANDOM）
CREATED=0

if [ -z "${PGSQL_PREFIX}" ] && command -v pg_config >/dev/null 2>&1; then
  PGSQL_PREFIX="$(dirname "$(dirname "$(command -v pg_config)")")"
fi
for bin in psql createdb dropdb; do
  if [ -x "${PGSQL_PREFIX}/bin/${bin}" ]; then
    eval "${bin^^}_BIN='${PGSQL_PREFIX}/bin/${bin}'"
  elif command -v "${bin}" >/dev/null 2>&1; then
    eval "${bin^^}_BIN='$(command -v "${bin}")'"
  else
    echo "missing command: ${bin}" >&2
    exit 1
  fi
done

# -w 纪律：三类客户端调用（psql/createdb/dropdb）一律不交互提示口令——
# 口令认证场景先备好 PGPASSWORD 或 .pgpass，否则挂起会发生在 EXIT trap 已注册
# 之后（cleanup 也不会执行，agent 完全失联）
psql_db() {
  "${PSQL_BIN}" -w -h "${PGHOST}" -p "${PGPORT}" -U "${PGUSER}" -d "$1" \
    -At -v ON_ERROR_STOP=1 -c "$2"
}

cleanup() {
  # 只删本次创建的库：createdb 失败（同名库已存在）时 NOT dropdb，避免误删他人同名库
  if [ "${CREATED}" = 1 ]; then
    "${DROPDB_BIN}" -w -h "${PGHOST}" -p "${PGPORT}" -U "${PGUSER}" --if-exists "${DB}" \
      || echo "[WARN] dropdb ${DB} 失败（活动连接？），请人工确认清理" >&2
  fi
}
trap cleanup EXIT

echo "== pgGraph smoke test =="
echo "PGHOST=${PGHOST} PGPORT=${PGPORT} PGUSER=${PGUSER} DB=${DB}"

if ! "${CREATEDB_BIN}" -w -h "${PGHOST}" -p "${PGPORT}" -U "${PGUSER}" "${DB}"; then
  echo "FAIL: cannot create temporary database ${DB}（CREATEDB 权限或同名库已存在；不会删除已存在的同名库）" >&2
  exit 1
fi
CREATED=1

if ! psql_db "${DB}" "CREATE EXTENSION graph" >/dev/null; then
  echo "FAIL: CREATE EXTENSION graph failed" >&2
  exit 1
fi
echo "OK: extension installed"

psql_db "${DB}" "
CREATE TABLE node (
  id text PRIMARY KEY,
  name text NOT NULL,
  parent_id text REFERENCES node(id)
);
INSERT INTO node VALUES
  ('root','Root',NULL),
  ('child','Child','root'),
  ('leaf','Leaf','child');
SELECT graph.add_table('node'::regclass, 'id', ARRAY['name']);
SELECT graph.add_edge('node'::regclass, 'parent_id', 'node'::regclass, 'id', 'parent', false);
SELECT * FROM graph.build();
" >/dev/null || { echo "FAIL: register/build failed" >&2; exit 1; }
echo "OK: register + build"

rows="$(psql_db "${DB}" "SELECT count(*) FROM graph.traverse('node'::regclass, 'child', 1, direction := 'out', hydrate := false, max_rows := 100)")"
[ "${rows}" -ge 2 ] 2>/dev/null || { echo "FAIL: traverse returned ${rows}" >&2; exit 1; }
echo "OK: traverse rows=${rows}"

found="$(psql_db "${DB}" "SELECT count(*) FROM graph.search('name','Child', table_filter := 'node'::regclass, mode := 'contains')")"
[ "${found}" -ge 1 ] 2>/dev/null || { echo "FAIL: search returned ${found}" >&2; exit 1; }
echo "OK: search rows=${found}"

echo "PASS: pgGraph smoke test"
