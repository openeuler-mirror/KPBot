#!/usr/bin/env bash
# tier-B bitexact-bench case entrypoint
set -u
cd "$(dirname "$0")"
mkdir -p results

if ! command -v go >/dev/null 2>&1; then
  printf '%s\n' '{"case": "bitexact-bench", "status": "SKIP", "reason": "go toolchain not available"}' > results/result.json
  echo "SKIP: go toolchain not available; recorded in results/result.json"
  exit 0
fi

go test -count=1 ./... 2>&1 | tee results/test.log
status=${PIPESTATUS[0]}

# A/B benchmark evidence (recorded, not gated): machine-dependent numbers.
go test -run '^$' -bench . -benchmem -count 1 ./... > results/bench.txt 2>&1 || true

if [ "$status" -eq 0 ]; then
  printf '%s\n' '{"case": "bitexact-bench", "status": "PASS", "evidence": ["results/test.log", "results/bench.txt"]}' > results/result.json
  echo "bitexact-bench: all bit-exactness and parity tests passed"
else
  printf '%s\n' '{"case": "bitexact-bench", "status": "FAIL", "evidence": ["results/test.log", "results/bench.txt"]}' > results/result.json
  echo "bitexact-bench: FAILED (see results/test.log)" >&2
fi
exit "$status"
