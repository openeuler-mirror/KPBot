#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/binaries.yaml"

VERSION=$(grep '^  version:' "$CONFIG_FILE" | head -1 | sed 's/.*: *"\(.*\)".*/\1/')
DISPLAY_VERSION=$(grep '^  display_version:' "$CONFIG_FILE" | head -1 | sed 's/.*: *"\(.*\)".*/\1/')
BINARY_SUBPATH=$(grep '^[[:space:]]*binary_subpath:' "$CONFIG_FILE" | head -1 | sed 's/.*: *//')

CANDIDATE_PATHS=(
    "${DEVKIT_BIN:-}"
    "/opt/devkit/${BINARY_SUBPATH}"
    "$HOME/.local/share/devkit/${BINARY_SUBPATH}"
)

PRINT_PATH=false
while [ $# -gt 0 ]; do
    case "$1" in
        --path) PRINT_PATH=true; shift ;;
        --help)
            cat <<'EOF'
devkit discover — Check if devkit is installed and usable

Usage:
  discover_devkit.sh [options]

Options:
  --path    Print the devkit binary path only
  --help    Show this help message
EOF
            exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

DEVKIT_BIN=""
for candidate in "${CANDIDATE_PATHS[@]}"; do
    [ -z "$candidate" ] && continue
    if [ -x "$candidate" ]; then
        DEVKIT_BIN="$candidate"
        break
    fi
done

if [ -z "$DEVKIT_BIN" ]; then
    if [ "$PRINT_PATH" = true ]; then
        exit 1
    fi
    echo "devkit: NOT_INSTALLED"
    echo "  Run scripts/deploy_devkit.sh to install."
    exit 1
fi

if [ "$PRINT_PATH" = true ]; then
    echo "$DEVKIT_BIN"
    exit 0
fi

LIB_SUBPATH=$(grep '^[[:space:]]*lib_subpath:' "$CONFIG_FILE" | head -1 | sed 's/.*: *//')
BINARY_SUBPATH=$(grep '^[[:space:]]*binary_subpath:' "$CONFIG_FILE" | head -1 | sed 's/.*: *//')
DEVKIT_ROOT="${DEVKIT_BIN%/${BINARY_SUBPATH}}"
export LD_LIBRARY_PATH="${DEVKIT_ROOT}/${LIB_SUBPATH}:${LD_LIBRARY_PATH:-}"

status="READY"
issues=""

if ! output=$("$DEVKIT_BIN" --version 2>&1); then
    status="ERROR"
    issues="devkit --version failed"
else
    installed_version=$(echo "$output" | grep -oiE 'version[[:space:]]+[0-9]+\.[0-9]+\.[a-z0-9]+' | awk '{print $NF}')
    if [ -z "$installed_version" ]; then
        status="VERSION_MISMATCH"
        issues="could not parse version from output"
    elif [ "${installed_version,,}" != "${VERSION,,}" ]; then
        status="VERSION_MISMATCH"
        issues="expected ${VERSION}, got ${installed_version}"
    fi
fi

if [ "$status" = "READY" ]; then
    if ! command -v file &>/dev/null; then
        :
    else
        arch=$(file -b "$DEVKIT_BIN" 2>/dev/null)
        if ! echo "$arch" | grep -qi "aarch64"; then
            status="ARCH_MISMATCH"
            issues="not aarch64 ELF: $arch"
        fi
    fi
fi

if [ "$status" = "READY" ]; then
    output=$("$DEVKIT_BIN" tuner help 2>&1 || true)
    if ! echo "$output" | grep -qi "tuner"; then
        status="PLUGIN_MISSING"
        issues="tuner plugin not loaded"
    fi
fi

echo "devkit: ${status} (v${VERSION}, ${DEVKIT_BIN})"

if [ "$status" = "READY" ]; then
    TASKS=$(awk '/^tuner_tasks:/{f=1;next} f&&/^  - name:/{print $3} f&&/^[^[:space:]]/{f=0}' "$CONFIG_FILE")
    [ -z "$TASKS" ] && TASKS="turbostat top-down numafast"
    for task in $TASKS; do
        task_output=$("$DEVKIT_BIN" tuner "$task" --help 2>&1 | tr -d '\0' || true)
        if echo "$task_output" | grep -qi "NAME\|USAGE" && ! echo "$task_output" | grep -qi "^error:\|not found\|invalid sub task"; then
            echo "  ${task}: READY"
        else
            echo "  ${task}: TASK_UNAVAILABLE"
        fi
    done
fi

if [ -n "$issues" ]; then
    echo "  Issue: ${issues}"
    exit 1
fi
