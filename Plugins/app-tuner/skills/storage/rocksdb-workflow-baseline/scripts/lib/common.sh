#!/usr/bin/env bash
# Shared helpers for rocksdb-workflow-baseline.

SKILL_ROOT="${SKILL_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"

log_error() { printf '[ERROR] %s\n' "$*" >&2; }

die() {
  local message=$1
  local code=${2:-1}
  log_error "$message"
  exit "$code"
}
