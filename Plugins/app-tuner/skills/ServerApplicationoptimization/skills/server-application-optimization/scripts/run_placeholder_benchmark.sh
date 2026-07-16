#!/usr/bin/env bash
set -euo pipefail

SCENARIO_NAME="${1:-placeholder-scenario}"

cat <<EOF
[placeholder-benchmark]
scenario=${SCENARIO_NAME}
status=not-implemented
message=Attach your real deployment guide and benchmark script here.
EOF
