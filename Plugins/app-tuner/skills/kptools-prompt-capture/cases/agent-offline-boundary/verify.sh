#!/usr/bin/env bash
set -eu
cd /workspace
rm -f results/result.json
sha256sum --quiet -c /harness/pristine.sha256
python3 /harness/verify.py
