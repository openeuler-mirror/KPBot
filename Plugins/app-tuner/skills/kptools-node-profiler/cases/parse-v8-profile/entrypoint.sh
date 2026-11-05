#!/usr/bin/env bash
set -uo pipefail
CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
S="$(cd "$CASE_DIR/../.." && pwd)/scripts"
rm -rf "$CASE_DIR/results/work"
mkdir -p "$CASE_DIR/results/work"
cd "$CASE_DIR/results/work"
cat > demo.js <<'JS'
function hotA() { let s = 0; for (let i = 0; i < 3e6; i++) s += Math.sqrt(i); return s; }
setInterval(() => { hotA(); }, 5);
setTimeout(() => process.exit(0), 3000);
JS
node --prof demo.js >/dev/null 2>&1
node --cpu-prof --cpu-prof-dir . demo.js >/dev/null 2>&1
sleep 1
bash "$S/collect.sh" --parse -f "$(ls -t isolate-*.log | head -1)" > "$CASE_DIR/results/parse.txt" 2>&1
bash "$S/collect.sh" --cpuprofile -d . > "$CASE_DIR/results/cpuprofile.txt" 2>&1
ok=1
grep -q 'hotA' "$CASE_DIR/results/parse.txt" || ok=0
ls "$CASE_DIR/results/work/"*.cpuprofile >/dev/null 2>&1 || ok=0
grep -q '^cpuprofile: \./' "$CASE_DIR/results/cpuprofile.txt" || ok=0
HOTA=$(grep -c 'hotA' "$CASE_DIR/results/parse.txt" || true)
CPUF=0; ls "$CASE_DIR/results/work/"*.cpuprofile >/dev/null 2>&1 && CPUF=1
python3 -c "import json;json.dump({'success':bool($ok),'parse_hotA_visible':bool($HOTA),'cpuprofile_located':bool($CPUF)},open('$CASE_DIR/results/result.json','w'),indent=2)"
exit $((1 - ok))
