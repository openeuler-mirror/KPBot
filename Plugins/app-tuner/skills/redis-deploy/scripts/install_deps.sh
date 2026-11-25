#!/usr/bin/env bash
#===============================================================
# install_deps.sh — 安装 Redis 源码构建所需依赖（幂等）
#
# 用法:
#   bash scripts/install_deps.sh [--dry-run] [--tls]
#
# 环境变量:
#   DRY_RUN=1    只打印将执行的命令
#   BUILD_TLS=1  额外安装 OpenSSL 开发包
#
# 支持 yum/dnf、apt-get、zypper；不修改内核参数。
#===============================================================
set -euo pipefail

SKILL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/common.sh
source "$SKILL_ROOT/scripts/lib/common.sh"

DRY_RUN=${DRY_RUN:-0}
BUILD_TLS=${BUILD_TLS:-0}

usage() {
  cat <<'EOF'
用法: install_deps.sh [--dry-run] [--tls]

  --dry-run   只打印将执行的安装命令
  --tls       额外安装 OpenSSL 开发包（Redis 构建 BUILD_TLS=yes 时需要）
  -h, --help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --tls) BUILD_TLS=1; shift ;;
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
  die "未识别的包管理器（需要 dnf/yum/apt-get/zypper），请手工安装 gcc make tar findutils coreutils 后加 --skip-deps"
fi

PKGS=()
if [[ "$PKG_MGR" == "apt-get" ]]; then
  PKGS=(build-essential tar ca-certificates)
  command -v curl >/dev/null 2>&1 || PKGS+=(curl)
  command -v wget >/dev/null 2>&1 || PKGS+=(wget)
  command -v sha256sum >/dev/null 2>&1 || PKGS+=(coreutils)
  command -v realpath >/dev/null 2>&1 || PKGS+=(coreutils)
  command -v find >/dev/null 2>&1 || PKGS+=(findutils)
  command -v useradd >/dev/null 2>&1 || PKGS+=(passwd)
  if [[ "$BUILD_TLS" == "1" ]]; then
    PKGS+=(libssl-dev)
  fi
else
  PKGS=(gcc make tar)
  command -v curl >/dev/null 2>&1 || PKGS+=(curl)
  command -v wget >/dev/null 2>&1 || PKGS+=(wget)
  command -v sha256sum >/dev/null 2>&1 || PKGS+=(coreutils)
  command -v realpath >/dev/null 2>&1 || PKGS+=(coreutils)
  command -v find >/dev/null 2>&1 || PKGS+=(findutils)
  if [[ "$PKG_MGR" == "zypper" ]]; then
    command -v useradd >/dev/null 2>&1 || PKGS+=(shadow)
    PKGS+=(ca-certificates)
    if [[ "$BUILD_TLS" == "1" ]]; then
      PKGS+=(libopenssl-devel)
    fi
  else
    command -v useradd >/dev/null 2>&1 || PKGS+=(shadow-utils)
    PKGS+=(ca-certificates)
    if [[ "$BUILD_TLS" == "1" ]]; then
      PKGS+=(openssl-devel)
    fi
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

for cmd in gcc make tar find; do
  require_cmd "$cmd"
done
if ! command -v sha256sum >/dev/null 2>&1 && ! command -v shasum >/dev/null 2>&1; then
  die "安装后仍缺少 sha256 校验工具"
fi
if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
  die "安装后仍缺少 curl/wget"
fi

log_ok "依赖安装完成"
