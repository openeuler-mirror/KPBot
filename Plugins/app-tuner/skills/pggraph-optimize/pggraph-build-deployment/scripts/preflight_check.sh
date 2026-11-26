#!/usr/bin/env bash
# Read-only pgGraph deployment preflight.
# Usage:
#   PGSQL_PREFIX=/opt/pgsql/18.3 PGDATA=/data/pg18 PGPORT=6666 \
#     bash preflight_check.sh
# Environment:
#   PGSQL_PREFIX  PostgreSQL install prefix (default: dirname of pg_config)
#   PGDATA        PostgreSQL data directory (default: empty)
#   PGHOST        host (default: localhost)
#   PGPORT        port (default: 5432)
#   PGUSER        user (default: postgres)
#   PGDB          database (default: postgres)
# Auth: all psql calls use -w (never prompt); password auth requires
#   PGPASSWORD env or a .pgpass entry prepared beforehand.
# Exit: 0 all required checks pass; 1 one or more required checks fail.
set -u

PGHOST="${PGHOST:-localhost}"
PGPORT="${PGPORT:-5432}"
PGUSER="${PGUSER:-postgres}"
PGDB="${PGDB:-postgres}"
PGSQL_PREFIX="${PGSQL_PREFIX:-}"
PGDATA="${PGDATA:-}"

fail=0
warn=0

say()  { printf '%s\n' "$*"; }
ok()   { printf '[ OK ] %s\n' "$*"; }
bad()  { printf '[FAIL] %s\n' "$*"; fail=$((fail+1)); }
note() { printf '[WARN] %s\n' "$*"; warn=$((warn+1)); }

need_cmd() {
  # 先查 ${PGSQL_PREFIX}/bin（示例仅设前缀、其 bin 不在 PATH 时也应判可用），再查 PATH
  if [ -n "${PGSQL_PREFIX}" ] && [ -x "${PGSQL_PREFIX}/bin/$1" ]; then
    ok "command: $1 (${PGSQL_PREFIX}/bin/$1)"
  elif command -v "$1" >/dev/null 2>&1; then
    ok "command: $1"
  else
    bad "missing command: $1（${PGSQL_PREFIX}/bin 与 PATH 均未找到）"
  fi
}

say "== pgGraph preflight =="
say "PGHOST=${PGHOST} PGPORT=${PGPORT} PGUSER=${PGUSER} PGDB=${PGDB}"
say "PGSQL_PREFIX=${PGSQL_PREFIX:-<auto>} PGDATA=${PGDATA:-<unset>}"
say ""

if [ -z "${PGSQL_PREFIX}" ]; then
  if command -v pg_config >/dev/null 2>&1; then
    PGSQL_PREFIX="$(dirname "$(dirname "$(command -v pg_config)")")"
  fi
fi

if [ -n "${PGSQL_PREFIX}" ] && [ -x "${PGSQL_PREFIX}/bin/pg_config" ]; then
  ok "pg_config: ${PGSQL_PREFIX}/bin/pg_config"
  PG_VERSION="$("${PGSQL_PREFIX}/bin/pg_config" --version 2>/dev/null || true)"
  ok "version: ${PG_VERSION:-unknown}"
  for f in graph.so; do
    if [ -f "$("${PGSQL_PREFIX}/bin/pg_config" --pkglibdir 2>/dev/null)/${f}" ]; then
      ok "installed: $("${PGSQL_PREFIX}/bin/pg_config" --pkglibdir)/${f}"
    else
      bad "missing installed ${f} under pkglibdir"
    fi
  done
  if ls "$("${PGSQL_PREFIX}/bin/pg_config" --sharedir 2>/dev/null)/extension"/graph.control >/dev/null 2>&1; then
    ok "installed: graph.control"
  else
    bad "missing graph.control under sharedir/extension"
  fi
else
  bad "pg_config not found; set PGSQL_PREFIX"
fi

for c in psql pg_isready sha256sum; do
  need_cmd "${c}"
done

PSQL=""
if [ -n "${PGSQL_PREFIX}" ] && [ -x "${PGSQL_PREFIX}/bin/psql" ]; then
  PSQL="${PGSQL_PREFIX}/bin/psql"
elif command -v psql >/dev/null 2>&1; then
  PSQL="$(command -v psql)"
fi

if [ -n "${PGDATA}" ]; then
  if [ -d "${PGDATA}" ]; then
    ok "PGDATA exists: ${PGDATA}"
    owner="$(stat -c '%U' "${PGDATA}" 2>/dev/null || true)"
    [ -n "${owner}" ] && ok "PGDATA owner: ${owner}"
    if [ "${owner}" = "root" ]; then
      note "PGDATA owned by root; PostgreSQL refuses to run as root"
    fi
  else
    bad "PGDATA missing: ${PGDATA}"
  fi
  if [ -f "${PGDATA}/postmaster.pid" ]; then
    ok "postmaster.pid present"
  else
    note "postmaster.pid absent; instance may be stopped"
  fi
else
  note "PGDATA not set; skipping data-directory checks"
fi

if command -v pg_isready >/dev/null 2>&1 || { [ -n "${PGSQL_PREFIX}" ] && [ -x "${PGSQL_PREFIX}/bin/pg_isready" ]; }; then
  ISREADY="${PGSQL_PREFIX}/bin/pg_isready"
  [ -x "${ISREADY}" ] || ISREADY="$(command -v pg_isready)"
  if "${ISREADY}" -h "${PGHOST}" -p "${PGPORT}" >/dev/null 2>&1; then
    ok "PostgreSQL is ready on ${PGHOST}:${PGPORT}"
  else
    bad "PostgreSQL is not ready on ${PGHOST}:${PGPORT}"
  fi
fi

if [ -n "${PSQL}" ]; then
  # -w：绝不交互提示口令——口令认证场景须先备好 PGPASSWORD 或 .pgpass，
  # 否则脚本会挂在 /dev/tty 等待输入（agent 无人值守时即失联）
  q() {
    "${PSQL}" -w -h "${PGHOST}" -p "${PGPORT}" -U "${PGUSER}" -d "${PGDB}" \
      -At -v ON_ERROR_STOP=1 -c "$1" 2>/dev/null
  }
  if q "SELECT 1" >/dev/null; then
    ok "SQL connection works"
    ext_ver="$(q "SELECT extversion FROM pg_extension WHERE extname='graph'" || true)"
    if [ -n "${ext_ver}" ]; then
      ok "extension graph ${ext_ver}"
      # 分别检查两条 catalog 查询的退出状态：无权读注册表时失败会显式报错，
      # 不能被 || echo 0 静默转成 0 后仍报 OK
      if reg_t="$(q "SELECT count(*) FROM graph._registered_tables")"; then
        reg_e="$(q "SELECT count(*) FROM graph._registered_edges")" || {
          reg_e=""; note "graph._registered_edges 查询失败（权限？）"
        }
        [ -n "${reg_e}" ] && ok "catalog: ${reg_t} tables, ${reg_e} edges"
      else
        bad "graph._registered_tables 查询失败（权限/扩展版本？），catalog 数量不可用"
      fi
      if [ "${reg_t}" -gt 0 ] 2>/dev/null; then
        if q "SELECT count(*) FROM graph.status()" >/dev/null 2>&1; then
          ok "graph.status() callable"
        else
          note "graph.status() not callable"
        fi
      fi
    else
      bad "extension graph is not installed in database ${PGDB}"
    fi
  else
    bad "SQL connection failed"
  fi
fi

if [ -n "${PGDATA}" ] && [ -d "${PGDATA}" ]; then
  artifacts="$(find "${PGDATA}" -name main.pggraph -type f 2>/dev/null | head -20)"
  if [ -n "${artifacts}" ]; then
    while IFS= read -r a; do
      [ -n "${a}" ] && ok "artifact: ${a} ($(du -h "${a}" 2>/dev/null | cut -f1))"
    done <<< "${artifacts}"
  else
    note "no main.pggraph under PGDATA; build may not be persisted"
  fi
fi

say ""
say "== summary: failures=${fail} warnings=${warn} =="
[ "${fail}" -eq 0 ]
