#!/usr/bin/env bash
# Shared helpers for the redis-deploy scripts. Source only; no side effects
# besides setting color variables.

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

sha256_of() {
  local file=$1
  [[ -f "$file" ]] || die "文件不存在，无法校验: $file"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$file" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$file" | awk '{print $1}'
  else
    die "缺少 sha256sum/shasum，无法校验安装包"
  fi
}

is_sha256_hex() {
  [[ "$1" =~ ^[0-9a-fA-F]{64}$ ]]
}
