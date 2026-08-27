#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

DEVKIT_BIN="${DEVKIT_BIN:-}"
if [ -z "$DEVKIT_BIN" ]; then
    if command -v devkit &>/dev/null; then
        DEVKIT_BIN=$(command -v devkit)
    else
        DEVKIT_BIN=$("$SCRIPT_DIR/discover_devkit.sh" --path 2>/dev/null || true)
        if [ -z "$DEVKIT_BIN" ]; then
            echo "ERROR: devkit not installed. Run scripts/deploy_devkit.sh to install." >&2
            exit 1
        fi
    fi
fi

CONFIG_FILE="$SCRIPT_DIR/binaries.yaml"
LIB_SUBPATH=$(grep '^[[:space:]]*lib_subpath:' "$CONFIG_FILE" | head -1 | sed 's/.*: *//')
BINARY_SUBPATH=$(grep '^[[:space:]]*binary_subpath:' "$CONFIG_FILE" | head -1 | sed 's/.*: *//')
DEVKIT_ROOT="${DEVKIT_BIN%/${BINARY_SUBPATH}}"
export LD_LIBRARY_PATH="${DEVKIT_ROOT}/${LIB_SUBPATH}:${LD_LIBRARY_PATH:-}"

exec "$DEVKIT_BIN" "$@"
