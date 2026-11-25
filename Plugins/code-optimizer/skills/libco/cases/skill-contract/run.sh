#!/usr/bin/env bash
set -u
cd "$(dirname "$0")/../.."
exec python3 cases/skill-contract/checks.py
