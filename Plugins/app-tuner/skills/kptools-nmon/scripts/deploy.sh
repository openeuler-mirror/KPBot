#!/bin/bash
set -euo pipefail

print_help() {
    cat <<'EOF'
nmon deploy — Check/install the nmon tool (open source, via system package manager)

Usage:
  deploy.sh [options]

Options:
  --help    Show this help message

Behavior:
  - nmon already installed: report and exit 0
  - root: try yum install -y nmon, fallback apt-get install -y nmon
  - non-root: print the install command and exit 1
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --help) print_help; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

if command -v nmon &>/dev/null; then
    echo "nmon: already installed ($(command -v nmon))"
    exit 0
fi

if [ "$(id -u)" -eq 0 ]; then
    if command -v yum &>/dev/null && yum install -y nmon; then
        echo "nmon: installed via yum ($(command -v nmon))"
        exit 0
    fi
    if command -v apt-get &>/dev/null && apt-get install -y nmon; then
        echo "nmon: installed via apt-get ($(command -v nmon))"
        exit 0
    fi
    echo "ERROR: failed to install nmon via yum/apt-get" >&2
    exit 1
fi

echo "nmon: NOT_INSTALLED. Install with: yum install nmon or apt install nmon (root required)" >&2
exit 1
