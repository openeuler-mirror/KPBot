#!/usr/bin/env bash
# Shared helpers for the vectordbbench-deploy scripts. Source only.

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

# 安装/源码/日志/构建/结果等参数路径不得直接指向系统级目录。
is_reserved_root() {
  case "$1" in
    /|/bin|/boot|/dev|/etc|/home|/lib|/lib64|/media|/mnt|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/usr/local|/var|/var/cache|/var/lib|/var/log|/var/opt|/var/spool|/var/tmp)
      return 0 ;;
  esac
  return 1
}

check_system_path() {
  is_reserved_root "$1" \
    && die "拒绝把系统级目录直接作为安装/源码/日志/构建/结果路径: $1（请使用其下专用子目录）"
  return 0
}

# install-root 不得落在系统配置/库目录树内。
is_system_install_tree() {
  case "$1" in
    /etc|/etc/*|/bin|/bin/*|/sbin|/sbin/*|/lib|/lib/*|/lib64|/lib64/*|/boot|/boot/*|/usr/bin|/usr/bin/*|/usr/sbin|/usr/sbin/*|/usr/lib|/usr/lib/*|/usr/lib64|/usr/lib64/*)
      return 0 ;;
  esac
  return 1
}

# bin-dir 不得落在系统二进制目录树内。
is_system_bin_tree() {
  case "$1" in
    /|/bin|/bin/*|/sbin|/sbin/*|/usr/bin|/usr/bin/*|/usr/sbin|/usr/sbin/*)
      return 0 ;;
  esac
  return 1
}

# unsafe_parent_of DIR: 输出第一个不安全父目录（路径|原因），安全时无输出。
# 使用 stat -L 解引用符号链接，按目标文件/目录判定属主与权限。
unsafe_parent_of() {
  local dir=$1
  local parent owner perm bits
  parent=$(dirname "$dir")
  while [[ "$parent" != "/" && "$parent" != "." ]]; do
    if [[ -d "$parent" ]]; then
      owner=$(stat -L -c '%u' "$parent" 2>/dev/null || echo unknown)
      perm=$(stat -L -c '%a' "$parent" 2>/dev/null || echo unknown)
      if [[ "$owner" != "0" ]]; then
        printf '%s|不属于 root' "$parent"
        return 0
      fi
      if [[ "$perm" =~ ^[0-9]+$ ]]; then
        bits=$((8#$perm))
        if (((bits & 0022) != 0 && (bits & 01000) == 0)); then
          printf '%s|可被组/其他用户写入且无 sticky 位（%s）' "$parent" "$perm"
          return 0
        fi
      fi
    fi
    parent=$(dirname "$parent")
  done
  return 0
}

# mask_url: 打印前去掉 URL 中的 userinfo，避免凭据进入日志。
mask_url() {
  local url=${1:-}
  if [[ "$url" =~ ^([A-Za-z][A-Za-z0-9+.-]*://)[^/@]*@(.*)$ ]]; then
    printf '%s***@%s' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
  else
    printf '%s' "$url"
  fi
}

# argv 中的 URL 不允许携带 userinfo（可被 ps/proc 读取）。
url_has_userinfo() {
  [[ "${1:-}" =~ ^[A-Za-z][A-Za-z0-9+.-]*://[^/]*@ ]]
}

# find_python: 选择 3.11+ 解释器，并设置 PYTHON / PY_TAG / PY_VERSION。
# 要求解释器属于 root 且组/其他不可写，避免 root 执行用户可替换的 Python。
find_python() {
  local candidates=()
  local c p owner mode
  if [[ -n "${PYTHON:-}" ]]; then
    if [[ "$PYTHON" != /* ]]; then
      PYTHON=$(command -v "$PYTHON" 2>/dev/null || true)
    fi
    [[ -n "$PYTHON" && -x "$PYTHON" ]] || die "指定的 Python 不存在或不可执行: ${PYTHON:-<empty>}"
    candidates=("$PYTHON")
  else
    for c in python3.13 python3.12 python3.11 python3; do
      p=$(command -v "$c" 2>/dev/null || true)
      [[ -n "$p" ]] && candidates+=("$p")
    done
  fi
  PYTHON=""
  local first_untrusted=""
  for p in "${candidates[@]}"; do
    if ! "$p" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
      continue
    fi
    owner=$(stat -L -c '%u' "$p" 2>/dev/null || echo '')
    mode=$(stat -L -c '%a' "$p" 2>/dev/null || echo '')
    if [[ "$owner" == "0" ]] && [[ "$mode" =~ ^[0-9]+$ ]] && (( (8#$mode & 0022) == 0 )); then
      PYTHON=$p
      break
    fi
    [[ -z "$first_untrusted" ]] && first_untrusted="$p (uid=${owner:-unknown} mode=${mode:-unknown})"
  done
  if [[ -z "$PYTHON" ]]; then
    if [[ -n "$first_untrusted" ]]; then
      die "找到 Python 3.11+ 但解释器不可信（必须是 root 属主且组/其他不可写）: $first_untrusted"
    fi
    die "未找到 Python 3.11+；请安装 python3.11/3.12（VectorDBBench master 要求 Python >= 3.11）"
  fi
  "$PYTHON" -m venv --help >/dev/null 2>&1 \
    || die "Python 缺少 venv 模块；请安装 ${PYTHON} 对应的 venv 包（如 python3.11-venv）"

  PY_TAG=$("$PYTHON" -c 'import sys; print("{}.{}".format(sys.version_info[0], sys.version_info[1]))')
  PY_VERSION=$("$PYTHON" -c 'import platform; print(platform.python_version())')
}
