#!/usr/bin/env bash
# Shared helpers for the pgvector-deploy scripts. Source only.

if [[ -t 1 ]]; then
  C_RED=$'\033[31m'
  C_GRN=$'\033[32m'
  C_YEL=$'\033[33m'
  C_BLU=$'\033[34m'
  C_RST=$'\033[0m'
else
  C_RED=""
  C_GRN=""
  C_YEL=""
  C_BLU=""
  C_RST=""
fi

log_info()  { printf '%s[INFO]%s %s\n' "$C_BLU" "$C_RST" "$*"; }
log_ok()    { printf '%s[ OK ]%s %s\n' "$C_GRN" "$C_RST" "$*"; }
log_warn()  { printf '%s[WARN]%s %s\n' "$C_YEL" "$C_RST" "$*" >&2; }
log_error() { printf '%s[FAIL]%s %s\n' "$C_RED" "$C_RST" "$*" >&2; }

die() {
  log_error "$*"
  exit 2
}

require_root() {
  [[ "$(id -u)" -eq 0 ]] || die "需要 root 权限执行（当前 uid=$(id -u)）"
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "缺少命令: $1"
}

resolve_pg_config() {
  local candidates=()
  local c owner mode
  if [[ -n "${PG_CONFIG:-}" ]]; then
    [[ -x "$PG_CONFIG" ]] || die "pg_config 不存在或不可执行: $PG_CONFIG"
  elif command -v pg_config >/dev/null 2>&1; then
    PG_CONFIG=$(command -v pg_config)
  else
    for c in /usr/pgsql-*/bin/pg_config /usr/lib/postgresql/*/bin/pg_config /usr/local/pgsql/bin/pg_config; do
      [[ -x "$c" ]] && candidates+=("$c")
    done
    if ((${#candidates[@]} == 1)); then
      PG_CONFIG=${candidates[0]}
    elif ((${#candidates[@]} > 1)); then
      die "找到多个 pg_config（${candidates[*]}），请用 --pg-config 指定与运行实例匹配的版本"
    else
      die "未找到 pg_config；请安装 PostgreSQL 服务端及 devel 包，或用 --pg-config 指定"
    fi
  fi

  owner=$(stat -c '%u' "$PG_CONFIG" 2>/dev/null || echo '')
  mode=$(stat -c '%a' "$PG_CONFIG" 2>/dev/null || echo '')
  [[ "$owner" == "0" ]] || die "pg_config 必须属于 root（当前 uid=${owner:-unknown}）: $PG_CONFIG"
  [[ "$mode" =~ ^[0-9]+$ ]] || die "无法读取 pg_config 权限: $PG_CONFIG"
  (( (8#$mode & 0022) == 0 )) || die "pg_config 不得组/其他可写（当前 $mode）: $PG_CONFIG"
}

check_pg_headers() {
  local inc
  [[ -n "${PG_CONFIG:-}" ]] || die "PG_CONFIG 未设置"
  inc=$("$PG_CONFIG" --includedir-server 2>/dev/null) \
    || die "无法读取 pg_config --includedir-server"
  [[ -f "$inc/postgres.h" ]] \
    || die "缺少 PostgreSQL 服务端头文件（$inc/postgres.h）；请安装匹配版本的 devel 包"
}
