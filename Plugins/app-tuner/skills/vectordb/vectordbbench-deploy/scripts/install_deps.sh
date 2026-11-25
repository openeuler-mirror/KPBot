#!/usr/bin/env bash
#===============================================================
# install_deps.sh — 安装 VectorDBBench 构建/虚拟环境依赖（幂等）
#
# 用法:
#   bash scripts/install_deps.sh [--dry-run]
#
# 说明:
#   - 安装 git/gcc/make/tar、系统 Python 3 + pip/venv/dev 头文件
#   - 不自动安装 Python 3.11+ 解释器；主脚本会查找 3.11+，找不到时明确失败
#===============================================================
set -euo pipefail

SKILL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/common.sh
source "$SKILL_ROOT/scripts/lib/common.sh"

DRY_RUN=${DRY_RUN:-0}

usage() {
  cat <<'EOF'
用法: install_deps.sh [--dry-run]

  --dry-run   只打印将执行的安装命令
  -h, --help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
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
  die "未识别的包管理器（需要 dnf/yum/apt-get/zypper），请手工安装 git gcc make tar python3(>=3.11)+venv 后加 --skip-deps"
fi

if [[ "$PKG_MGR" == "apt-get" ]]; then
  PKGS=(build-essential git tar ca-certificates
        python3 python3-pip python3-venv python3-dev
        libssl-dev libffi-dev)
elif [[ "$PKG_MGR" == "zypper" ]]; then
  PKGS=(gcc gcc-c++ make git tar ca-certificates
        python3 python3-pip python3-devel
        libopenssl-devel libffi-devel)
else
  PKGS=(gcc gcc-c++ make git tar ca-certificates
        python3 python3-pip python3-devel
        openssl-devel libffi-devel)
fi
command -v find >/dev/null 2>&1 || PKGS+=(findutils)
command -v realpath >/dev/null 2>&1 || PKGS+=(coreutils)

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

for cmd in git gcc make tar find; do
  require_cmd "$cmd"
done
require_cmd python3
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
  log_warn "系统 python3 低于 3.11；请安装 python3.11/3.12 并用 --python 指定（VectorDBBench master 要求 Python >= 3.11）"
fi
if ! python3 -m venv --help >/dev/null 2>&1; then
  log_warn "系统 python3 缺少 venv 模块；请安装对应 venv 包"
fi

log_ok "依赖安装完成"
