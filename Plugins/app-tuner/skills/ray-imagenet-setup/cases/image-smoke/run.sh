#!/usr/bin/env bash
set -eu
SKILL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$SKILL_DIR"
exec python3 cases/image-smoke/check.py
