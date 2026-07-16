#!/usr/bin/env bash
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() {
  echo -e "${GREEN}[INFO]${NC} $*"
}

log_warn() {
  echo -e "${YELLOW}[WARN]${NC} $*"
}

log_error() {
  echo -e "${RED}[ERROR]${NC} $*"
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

json_array_from_lines() {
  awk 'BEGIN{printf "["} NF{gsub(/\\/,"\\\\"); gsub(/"/,"\\\""); printf "%s\"%s\"", sep, $0; sep=","} END{printf "]"}'
}

list_threads() {
  local pid="$1"
  ps -T -p "$pid" -o tid=,psr=,pcpu=,comm= 2>/dev/null || true
}

write_json_error() {
  local message="$1"
  cat <<EOF
{"error":"$message"}
EOF
}
