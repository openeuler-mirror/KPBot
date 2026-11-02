#!/usr/bin/env bash
set -uo pipefail
CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
S="$(cd "$CASE_DIR/../.." && pwd)/scripts"
mkdir -p "$CASE_DIR/results"
bash "$S/collect.sh" --kernel --sysctl --thp --hugepages --numa-balancing --ulimit     > "$CASE_DIR/results/os.txt" 2>"$CASE_DIR/results/collect.err"
ok=1
grep -q '^=== sysctl' "$CASE_DIR/results/os.txt" || ok=0
grep -q '^=== THP' "$CASE_DIR/results/os.txt" || ok=0
grep -q 'HugePages_Total' "$CASE_DIR/results/os.txt" || ok=0
grep -q 'open files' "$CASE_DIR/results/os.txt" || ok=0
python3 -c "import json;json.dump({'success':bool($ok),'sections_ok':bool($ok),'source':'kptools-os-collector'},open('$CASE_DIR/results/result.json','w'),indent=2)"
exit $((1 - ok))
