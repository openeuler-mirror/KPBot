#!/usr/bin/env bash
set -eu
CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
RESULT_DIR="${GUIDE_PREFLIGHT_RESULTS:-$CASE_DIR/results}"
mkdir -p "$RESULT_DIR"
if ! command -v "${PYTHON:-python3}" >/dev/null 2>&1; then
    printf '%s\n' '{"success": false, "status": "env_not_satisfied", "tests": 0, "missing": ["python>=3.7"]}' \
        >"$RESULT_DIR/result.json"
    cat "$RESULT_DIR/result.json"
    exit 3
fi
cd "$CASE_DIR/../.."
rc=0
"${PYTHON:-python3}" "$CASE_DIR/preflight.py" || rc=$?
if [ "$rc" -ne 0 ]; then
    exit "$rc"
fi
exec "${PYTHON:-python3}" "$CASE_DIR/test_guide_import.py"
