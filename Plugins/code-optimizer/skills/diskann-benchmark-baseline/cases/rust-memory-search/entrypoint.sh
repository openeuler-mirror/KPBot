#!/usr/bin/env bash
set -euo pipefail
case_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${SOURCE:?Set SOURCE to the Rust DiskANN source directory}"
: "${BINARY:?Set BINARY to the native release diskann-benchmark executable}"
exec python3 "$case_dir/run.py" --source "$SOURCE" --binary "$BINARY" --output "${RESULTS:-results}" "$@"
