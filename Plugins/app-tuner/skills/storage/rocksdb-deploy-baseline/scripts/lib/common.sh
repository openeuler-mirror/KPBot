#!/usr/bin/env bash
# Shared helpers for rocksdb-deploy-baseline.

SKILL_ROOT="${SKILL_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"

log_info() { printf '[INFO] %s\n' "$*" >&2; }
log_error() { printf '[ERROR] %s\n' "$*" >&2; }

die() {
  local message=$1
  local code=${2:-1}
  log_error "$message"
  exit "$code"
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing command: $1" 2
}

run() {
  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    printf '[dry-run]'
    printf ' %q' "$@"
    printf '\n'
    if [[ -n "${DRY_LOG:-}" ]]; then
      printf '%s\n' "$*" >> "$DRY_LOG"
    fi
    return 0
  fi
  "$@"
}

apply_deps_env() {
  local file=${1:-${RESULT_DIR:-}/env/deps.env}
  local java_home gcc_root
  [[ -f "$file" ]] || return 0
  java_home=$(awk -F= '$1 == "JAVA_HOME" {print $2; exit}' "$file" || true)
  if [[ -n "$java_home" && -x "$java_home/bin/javac" ]]; then
    export JAVA_HOME="$java_home"
    export PATH="$JAVA_HOME/bin:$PATH"
  fi
  gcc_root=$(awk -F= '$1 == "GCC_ROOT" {print $2; exit}' "$file" || true)
  if [[ -n "$gcc_root" && "$gcc_root" != "system" && -x "$gcc_root/bin/gcc" ]]; then
    export PATH="$gcc_root/bin:$PATH"
    export LD_LIBRARY_PATH="$gcc_root/lib64:${LD_LIBRARY_PATH:-}"
  fi
  return 0
}
