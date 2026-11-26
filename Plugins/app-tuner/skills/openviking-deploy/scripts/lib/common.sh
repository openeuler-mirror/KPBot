#!/usr/bin/env bash
# Shared helpers for the openviking-deploy scripts. Source only.

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

trim_slash() {
  local p=$1
  while [[ "$p" == */ && "$p" != "/" ]]; do
    p=${p%/}
  done
  printf '%s' "$p"
}

valid_abs_path() {
  local p=$1
  [[ "$p" =~ ^/[A-Za-z0-9._/-]+$ ]] || return 1
  [[ "$p" == *".."* ]] && return 1
  [[ "$p" =~ (^|/)\.(/|$) ]] && return 1
  [[ "$p" == *"//"* ]] && return 1
  return 0
}

normalize_path() {
  local p=$1
  if command -v realpath >/dev/null 2>&1; then
    realpath -m -- "$p" || die "无法归一化路径: $p"
  else
    printf '%s' "$p"
  fi
}

check_system_path() {
  case "$1" in
    /|/bin|/boot|/dev|/etc|/home|/lib|/lib64|/media|/mnt|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/usr/local|/var|/var/cache|/var/lib|/var/log|/var/opt|/var/spool|/var/tmp)
      die "拒绝把系统级目录直接作为安装/源码/日志/构建/结果路径: $1（请使用其下专用子目录）" ;;
  esac
}

secure_path() {
  local name=$1
  local p=$2
  valid_abs_path "$p" || die "路径非法（必须是绝对路径，且不含 .、..、// 段）: $p"
  p=$(normalize_path "$p")
  valid_abs_path "$p" || die "归一化后的路径非法: $p"
  check_system_path "$p"
  printf -v "$name" '%s' "$p"
}

untrusted_parent_reason() {
  local dir=$1
  local allow_uid=${2:-0}
  local parent perm owner bits
  parent=$(dirname "$dir")
  while [[ "$parent" != "/" && "$parent" != "." ]]; do
    if [[ -d "$parent" ]]; then
      owner=$(stat -c '%u' "$parent")
      perm=$(stat -c '%a' "$parent")
      bits=$((8#$perm))
      if [[ "$owner" != "0" && "$owner" != "$allow_uid" ]]; then
        printf '%s 不属于 root 或允许的运行用户（uid=%s）' "$parent" "$owner"
        return 1
      fi
      if (((bits & 0022) != 0 && (bits & 01000) == 0)); then
        printf '%s 可被组/其他用户写入且无 sticky 位（权限 %s）' "$parent" "$perm"
        return 1
      fi
    fi
    parent=$(dirname "$parent")
  done
  return 0
}

check_trusted_parents() {
  local label=$1
  local reason
  if ! reason=$(untrusted_parent_reason "$2"); then
    die "$label 的父目录不可信: $reason（请改用 root 私有路径）"
  fi
}

check_build_parents() {
  check_trusted_parents "构建根目录" "$1"
}

url_has_userinfo() {
  local url=$1
  local rest authority
  rest=${url#*://}
  authority=${rest%%/*}
  [[ "$authority" == *"@"* ]]
}

prepare_private_dir() {
  local dir=$1
  local owner perm
  if [[ ! -e "$dir" ]]; then
    install -d -m 0700 -o root -g root "$dir"
  fi
  [[ -d "$dir" ]] || die "构建根路径不是目录: $dir"
  owner=$(stat -c '%u' "$dir")
  perm=$(stat -c '%a' "$dir")
  [[ "$owner" == "0" ]] || die "构建根目录必须属于 root（当前 uid=$owner）: $dir"
  case "$perm" in
    700|0700) ;;
    *) die "构建根目录权限必须为 0700（当前 $perm）: $dir" ;;
  esac
}

check_secret_file() {
  local file=$1
  local mode
  [[ -f "$file" ]] || die "密钥文件不存在: $file"
  mode=$(stat -c '%a' "$file")
  (( (8#$mode & 0077) == 0 )) || die "密钥文件权限过宽（$mode），请执行: chmod 600 $file"
}

read_secret_first_line() {
  local file=$1
  local line
  IFS= read -r line <"$file" || true
  [[ -n "$line" ]] || die "密钥文件首行为空: $file"
  printf '%s' "$line"
}

detect_pkg_manager() {
  if command -v dnf >/dev/null 2>&1; then
    printf 'dnf'
  elif command -v yum >/dev/null 2>&1; then
    printf 'yum'
  elif command -v apt-get >/dev/null 2>&1; then
    printf 'apt'
  elif command -v zypper >/dev/null 2>&1; then
    printf 'zypper'
  else
    printf 'unknown'
  fi
}

version_ge() {
  local have=$1
  local need=$2
  local IFS=.
  local -a a b
  read -r -a a <<<"$have"
  read -r -a b <<<"$need"
  local i
  for ((i = 0; i < ${#b[@]}; i++)); do
    local av=${a[i]:-0}
    local bv=${b[i]:-0}
    if ((10#$av > 10#$bv)); then
      return 0
    elif ((10#$av < 10#$bv)); then
      return 1
    fi
  done
  return 0
}
