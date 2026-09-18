#!/bin/bash
set -euo pipefail

print_help() {
    cat <<'EOF'
nmon discover — Check if nmon is installed and usable

Usage:
  discover.sh [options]

Options:
  --path    Print the nmon binary path only
  --help    Show this help message

Output:
  nmon: READY (<version>, <path>)
  nmon: NOT_INSTALLED (yum install nmon or apt install nmon)
EOF
}

PRINT_PATH=false
while [ $# -gt 0 ]; do
    case "$1" in
        --path) PRINT_PATH=true; shift ;;
        --help) print_help; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

if ! NMON_BIN=$(command -v nmon 2>/dev/null); then
    if [ "$PRINT_PATH" = true ]; then
        exit 1
    fi
    echo "nmon: NOT_INSTALLED (yum install nmon or apt install nmon)"
    exit 1
fi

if [ "$PRINT_PATH" = true ]; then
    echo "$NMON_BIN"
    exit 0
fi

VERSION=$(nmon -V 2>&1 | head -1 | grep -oE '[0-9]+[a-z]+' || echo "unknown")
echo "nmon: READY (${VERSION}, ${NMON_BIN})"
exit 0
