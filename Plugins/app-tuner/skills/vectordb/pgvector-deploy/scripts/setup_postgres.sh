#!/usr/bin/env bash
#===============================================================
# setup_postgres.sh — 确保本机 PostgreSQL 实例已初始化并运行
#
# 用法:
#   bash scripts/setup_postgres.sh [--pg-config PATH] [--db-port PORT]
#                                  [--check-only] [--dry-run]
#
# 说明:
#   - RHEL/openEuler: 缺 PGDATA 时执行 postgresql-setup --initdb，再启 systemd 服务
#   - Debian/Ubuntu: 使用 pg_lsclusters / pg_ctlcluster 启动已有集群
#   - --check-only 只检查，不安装、不初始化、不启动
#===============================================================
set -euo pipefail

SKILL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/common.sh
source "$SKILL_ROOT/scripts/lib/common.sh"

PG_CONFIG=""
DB_PORT=5432
CHECK_ONLY=0
DRY_RUN=0

usage() {
  cat <<'EOF'
用法: setup_postgres.sh [--pg-config PATH] [--db-port PORT] [--check-only] [--dry-run]

  --pg-config PATH  指定 pg_config（默认自动查找）
  --db-port PORT    PostgreSQL 端口（默认 5432）
  --check-only      只检查，不改动系统
  --dry-run         打印计划，不执行
  -h, --help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pg-config) PG_CONFIG=${2:?--pg-config 缺少参数}; shift 2 ;;
    --db-port) DB_PORT=${2:?--db-port 缺少参数}; shift 2 ;;
    --check-only) CHECK_ONLY=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数: $1（-h 查看用法）" ;;
  esac
done

[[ "$DB_PORT" =~ ^[0-9]+$ ]] && ((DB_PORT >= 1 && DB_PORT <= 65535)) \
  || die "端口非法: $DB_PORT"

if [[ "$CHECK_ONLY" != "1" && "$DRY_RUN" != "1" ]]; then
  require_root
fi

resolve_pg_config
check_pg_headers
PG_BINDIR=$("$PG_CONFIG" --bindir)
PG_ISREADY="$PG_BINDIR/pg_isready"
PSQL="$PG_BINDIR/psql"
if [[ ! -x "$PSQL" ]]; then
  PSQL=$(command -v psql 2>/dev/null || true)
fi
[[ -n "$PSQL" ]] || die "未找到 psql（应与 $PG_CONFIG 同版本）"
command -v runuser >/dev/null 2>&1 || die "缺少 runuser（util-linux）"

check_ready() {
  if [[ -x "$PG_ISREADY" ]]; then
    "$PG_ISREADY" -q -p "$DB_PORT" >/dev/null 2>&1
  else
    runuser -u postgres -- "$PSQL" -X -p "$DB_PORT" -tAc "SELECT 1" >/dev/null 2>&1
  fi
}

if check_ready; then
  log_ok "PostgreSQL 已在端口 $DB_PORT 运行（pg_config: $PG_CONFIG）"
  exit 0
fi

if [[ "$CHECK_ONLY" == "1" ]]; then
  die "PostgreSQL 未在端口 $DB_PORT 运行；请先启动实例或去掉 --skip-postgres-setup"
fi

if [[ "$DRY_RUN" == "1" ]]; then
  cat <<EOF
[dry-run] PostgreSQL 启动计划
  pg_config : $PG_CONFIG
  端口      : $DB_PORT
  Debian    : pg_lsclusters 为空则 pg_createcluster <major> main --start，否则 pg_ctlcluster 启动
  RHEL 系   : PGDATA 未初始化则 postgresql-setup --initdb，再 systemctl enable --now postgresql
EOF
  exit 0
fi

pg_major=$("$PG_CONFIG" --version | awk '{print $2}' | cut -d. -f1)

if command -v apt-get >/dev/null 2>&1; then
  if command -v pg_lsclusters >/dev/null 2>&1; then
    clusters=$(pg_lsclusters --no-header 2>/dev/null || true)
    if [[ -z "$clusters" ]]; then
      log_info "未发现 PostgreSQL 集群，创建 cluster main（版本 $pg_major）"
      pg_createcluster "$pg_major" main --start
    else
      while read -r ver name _port status _rest; do
        [[ -z "${ver:-}" ]] && continue
        if [[ "$status" != "online" ]]; then
          log_info "启动 PostgreSQL 集群 $ver/$name"
          pg_ctlcluster "$ver" "$name" start
        fi
      done <<<"$clusters"
    fi
  fi
  systemctl enable --now postgresql >/dev/null 2>&1 || true
else
  if [[ -z "${PGDATA:-}" ]]; then
    if [[ -f "/var/lib/pgsql/$pg_major/data/PG_VERSION" ]]; then
      PGDATA="/var/lib/pgsql/$pg_major/data"
    else
      PGDATA=/var/lib/pgsql/data
    fi
  fi
  if [[ ! -f "$PGDATA/PG_VERSION" ]]; then
    command -v postgresql-setup >/dev/null 2>&1 \
      || die "PGDATA 未初始化（$PGDATA）且缺少 postgresql-setup；请先手工初始化 PostgreSQL"
    log_info "初始化 PostgreSQL 数据目录: $PGDATA"
    postgresql-setup --initdb
  fi
  command -v systemctl >/dev/null 2>&1 \
    || die "无 systemd，无法自动启动 PostgreSQL；请手工启动后重试"
  started=0
  for unit in "postgresql-$pg_major" postgresql; do
    if systemctl list-unit-files "$unit.service" --no-legend 2>/dev/null | grep -q .; then
      log_info "启动 PostgreSQL 服务: $unit.service"
      systemctl enable --now "$unit"
      started=1
      break
    fi
  done
  ((started == 1)) || die "未找到 PostgreSQL systemd 单元（postgresql 或 postgresql-$pg_major）"
fi

for _ in $(seq 1 30); do
  if check_ready; then
    log_ok "PostgreSQL 已启动（端口 $DB_PORT）"
    exit 0
  fi
  sleep 1
done

journalctl -u postgresql -n 50 --no-pager 2>/dev/null || true
die "PostgreSQL 启动后仍无法连接（端口 $DB_PORT）；请检查服务日志与 pg_hba.conf"
