#!/usr/bin/env bash
set -uo pipefail
CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
S="$(cd "$CASE_DIR/../.." && pwd)/scripts"
mkdir -p "$CASE_DIR/results"
bash "$S/collect.sh" --dmi --smt --cstate --turbo --numa --ddr --aspm     > "$CASE_DIR/results/bios.txt" 2>"$CASE_DIR/results/collect.err"
ok=1
for sec in "=== SMT" "=== C-State" "=== Turbo" "=== NUMA Interleaving" "=== DDR Speed" "=== PCIe ASPM"; do
    grep -q "^$sec" "$CASE_DIR/results/bios.txt" || ok=0
done
python3 -c "import json;json.dump({'success':bool($ok),'sections_ok':bool($ok),'mode':'local'},open('$CASE_DIR/results/result.json','w'),indent=2)"
exit $((1 - ok))
