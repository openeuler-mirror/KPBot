#!/usr/bin/env bash
# tier-A skill-integrity case entrypoint
set -u
cd "$(dirname "$0")/../.."
exec python3 cases/skill-integrity/checks.py
