#!/usr/bin/env bash
set -eu
SKILL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$SKILL_DIR"
exec timeout --signal=TERM --kill-after=15s 240s python3 cases/real-ray/check.py
