#!/usr/bin/env bash
# tier-A skill-integrity case entrypoint
set -u
cd "$(dirname "$0")/../.."

# python3 -> python -> py -3（Windows Git Bash 下 python3 可能命中 Store stub 返回 49）
PY_BIN=""
for cand in python3 python py; do
    if command -v "$cand" >/dev/null 2>&1; then
        if [ "$cand" = "py" ]; then
            "$cand" -3 -c "" >/dev/null 2>&1 && PY_BIN="$cand -3"
        else
            "$cand" -c "" >/dev/null 2>&1 && PY_BIN="$cand"
        fi
        [ -n "$PY_BIN" ] && break
    fi
done
if [ -z "$PY_BIN" ]; then
    echo "FAIL: no usable python interpreter (python3/python/py)" >&2
    exit 1
fi
exec $PY_BIN cases/skill-integrity/checks.py
