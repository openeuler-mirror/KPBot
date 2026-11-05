#!/usr/bin/env bash
set -uo pipefail
CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
S="$(cd "$CASE_DIR/../.." && pwd)/scripts"
mkdir -p "$CASE_DIR/results"
python3 -c "import torch_npu" 2>/dev/null || {
    python3 -c "import json;json.dump({'success':True,'skipped':True,'skipped_or_success':True,'reason':'torch_npu not available'},open('$CASE_DIR/results/result.json','w'),indent=2)"
    exit 0
}
cat > "$CASE_DIR/results/npu_workload.py" <<'PY'
import torch, torch_npu, time
x = torch.randn(1024, 1024, device="npu")
time.sleep(2)
y = x @ x
time.sleep(2)
PY
python3 "$CASE_DIR/results/npu_workload.py" & WPID=$!
sleep 3
bash "$S/collect.sh" -p "$WPID" -d 3 -n -o "$CASE_DIR/results/flame_native.svg" >/dev/null 2>&1
kill "$WPID" 2>/dev/null
ok=$([ -s "$CASE_DIR/results/flame_native.svg" ] && echo 1 || echo 0)
python3 -c "import json;json.dump({'success':bool($ok),'skipped':False,'skipped_or_success':bool($ok)},open('$CASE_DIR/results/result.json','w'),indent=2)"
exit $((1 - ok))
