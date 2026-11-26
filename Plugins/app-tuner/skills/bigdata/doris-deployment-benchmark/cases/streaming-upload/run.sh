#!/usr/bin/env bash
set -eu
CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
command -v openssl >/dev/null || { echo "openssl is required for local TLS fixtures" >&2; exit 2; }
exec python3 "$CASE_DIR/test_streaming_upload.py"
