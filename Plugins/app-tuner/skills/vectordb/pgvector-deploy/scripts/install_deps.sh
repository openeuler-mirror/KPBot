#!/usr/bin/env bash
#===============================================================
# install_deps.sh — 安装 pgvector 构建与 PostgreSQL 运行依赖（幂等）
#
# 用法:
#   bash scripts/install_deps.sh [--dry-run] [--no-postgres]
#
# 说明:
#   - 仅在缺少 pg_config 或服务端头文件时安装 PostgreSQL 包
#   - --no-postgres 只装构建工具，不安装 PostgreSQL
#===============================================================
set -euo pipefail

SKILL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/common.sh
source "$SKILL_ROOT/scripts/lib/common.sh"

DRY_RUN=${DRY_RUN:-0}
NO_POSTGRES=${NO_POSTGRES:-0}

usage() {
  cat <<'EOF'
用法: install_deps.sh [--dry-run] [--no-postgres]

  --dry-run       只打印将执行的安装命令
  --no-postgres   不安装 PostgreSQL 包（要求系统已有 pg_config 与服务端头文件）
  -h, --help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --no-postgres) NO_POSTGRES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数: $1（-h 查看用法）" ;;
  esac
done

if [[ "$DRY_RUN" != "1" ]]; then
  require_root
fi

if command -v dnf >/dev/null 2>&1; then
  PKG_MGR=dnf
elif command -v yum >/dev/null 2>&1; then
  PKG_MGR=yum
elif command -v apt-get >/dev/null 2>&1; then
  PKG_MGR=apt-get
elif command -v zypper >/dev/null 2>&1; then
  PKG_MGR=zypper
else
  die "未识别的包管理器（需要 dnf/yum/apt-get/zypper），请手工安装 gcc make git tar 与 PostgreSQL devel 包后加 --skip-deps"
fi

needs_pg=0
if [[ "$NO_POSTGRES" != "1" ]]; then
  if ! command -v pg_config >/dev/null 2>&1; then
    needs_pg=1
  else
    inc=$(pg_config --includedir-server 2>/dev/null || true)
    [[ -f "$inc/postgres.h" ]] || needs_pg=1
  fi
fi

PKGS=()
if [[ "$PKG_MGR" == "apt-get" ]]; then
  PKGS=(build-essential git tar ca-certificates)
  command -v find >/dev/null 2>&1 || PKGS+=(findutils)
  command -v realpath >/dev/null 2>&1 || PKGS+=(coreutils)
  if [[ "$needs_pg" == "1" ]]; then
    PKGS+=(postgresql postgresql-server-dev-all)
  fi
else
  PKGS=(gcc make git tar)
  command -v find >/dev/null 2>&1 || PKGS+=(findutils)
  command -v realpath >/dev/null 2>&1 || PKGS+=(coreutils)
  PKGS+=(ca-certificates)
  if [[ "$needs_pg" == "1" ]]; then
    PKGS+=(postgresql-server postgresql-devel)
  fi
fi

log_info "包管理器: $PKG_MGR"
log_info "计划安装: ${PKGS[*]}"

if [[ "$DRY_RUN" == "1" ]]; then
  case "$PKG_MGR" in
    apt-get) printf '+ apt-get update -qq && apt-get install -y %s\n' "${PKGS[*]}" ;;
    zypper) printf '+ zypper --non-interactive install %s\n' "${PKGS[*]}" ;;
    *) printf '+ %s install -y %s\n' "$PKG_MGR" "${PKGS[*]}" ;;
  esac
  exit 0
fi

case "$PKG_MGR" in
  apt-get)
    apt-get update -qq || log_warn "apt-get update 失败，继续尝试 install"
    DEBIAN_FRONTEND=noninteractive apt-get install -y "${PKGS[@]}"
    ;;
  zypper)
    zypper --non-interactive install "${PKGS[@]}"
    ;;
  *)
    "$PKG_MGR" install -y "${PKGS[@]}"
    ;;
esac

for cmd in gcc make git tar find; do
  require_cmd "$cmd"
done
if [[ "$NO_POSTGRES" != "1" ]]; then
  resolve_pg_config
  check_pg_headers
fi

log_ok "依赖安装完成"
