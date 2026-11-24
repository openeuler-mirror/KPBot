#!/usr/bin/env bash
set -eu
SKILL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$SKILL_DIR"

write_env_result() {
    python3 - "$1" <<'PY'
import json
import pathlib
import sys

out = pathlib.Path("results/real-ray")
out.mkdir(parents=True, exist_ok=False)
(out / "result.json").write_text(
    json.dumps(
        {"success": False, "env_not_satisfied": True, "reason": sys.argv[1]},
        indent=2,
    )
    + "\n",
    encoding="utf-8",
)
PY
}

if ! command -v python3 >/dev/null 2>&1; then
    echo "SKIP(env_not_satisfied): python3 not found on PATH" >&2
    exit 3
fi
if ! command -v timeout >/dev/null 2>&1; then
    echo "SKIP(env_not_satisfied): GNU timeout not found on PATH" >&2
    write_env_result "GNU timeout not found on PATH" || true
    exit 3
fi
if ! timeout --version 2>/dev/null | grep -q "GNU coreutils"; then
    echo "SKIP(env_not_satisfied): timeout is not GNU coreutils" >&2
    write_env_result \
        "GNU coreutils timeout required (--kill-after support); found non-GNU timeout" \
        || true
    exit 3
fi
exec timeout --signal=TERM --kill-after=15s 240s python3 cases/real-ray/check.py
