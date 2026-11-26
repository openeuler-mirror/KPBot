#!/usr/bin/env bash
set -uo pipefail
CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
S="$(cd "$CASE_DIR/../.." && pwd)/scripts"
mkdir -p "$CASE_DIR/results"
cat > "$CASE_DIR/results/workload.py" <<'PY'
import threading, time
def work():
    while True:
        for _ in range(100000): pass
        time.sleep(0.001)
for _ in range(2):
    threading.Thread(target=work, daemon=True).start()
time.sleep(30)
PY
python3 "$CASE_DIR/results/workload.py" & WPID=$!
sleep 2
bash "$S/collect.sh" -p "$WPID" -d 4 -o "$CASE_DIR/results/flame.svg" >/dev/null 2>&1
bash "$S/collect.sh" -p "$WPID" -d 3 --format raw -o "$CASE_DIR/results/raw.txt" >/dev/null 2>&1
bash "$S/collect.sh" --dump -p "$WPID" > "$CASE_DIR/results/dump.txt" 2>&1
kill "$WPID" 2>/dev/null
ok=1
[ -s "$CASE_DIR/results/flame.svg" ] || ok=0
grep -q 'workload.py' "$CASE_DIR/results/raw.txt" || ok=0
grep -q 'Thread' "$CASE_DIR/results/dump.txt" || ok=0
python3 -c "import json;json.dump({'success':bool($ok),'business_frame_visible':bool($ok),'svg_written':bool($ok)},open('$CASE_DIR/results/result.json','w'),indent=2)"
exit $((1 - ok))
