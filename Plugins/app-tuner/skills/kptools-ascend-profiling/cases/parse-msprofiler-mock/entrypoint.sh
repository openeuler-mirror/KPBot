#!/usr/bin/env bash
set -uo pipefail
CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
S="$(cd "$CASE_DIR/../.." && pwd)/scripts"
mkdir -p "$CASE_DIR/results/shim" "$CASE_DIR/results/msout" "$CASE_DIR/results/parse_out"
cat > "$CASE_DIR/results/shim/msserviceprofiler" <<'SH'
#!/usr/bin/env bash
out=""
for a in "$@"; do case "$a" in --output-path=*) out="${a#--output-path=}" ;; esac; done
[ -n "$out" ] || { echo "no output-path" >&2; exit 1; }
mkdir -p "$out"
printf 'req_id,latency\n1,12\n' > "$out/request.csv"
printf 'kv\n1\n' > "$out/kvcache.csv"
printf 'chrome\n' > "$out/chrome_tracing.json"
SH
chmod +x "$CASE_DIR/results/shim/msserviceprofiler"
export PATH="$CASE_DIR/results/shim:$PATH"
mkdir -p "$CASE_DIR/results/work"
( cd "$CASE_DIR/results/work" && bash "$S/collect.sh" msp-parse -d "$CASE_DIR/results/msout" ) > "$CASE_DIR/results/parse.log" 2>&1
EVBASE="$CASE_DIR/results/work/ascend-profiling-evidence"
EV=""
[ -d "$EVBASE" ] && EV=$(ls -td "$EVBASE"/*/ 2>/dev/null | head -1)
ok=0
[ -n "$EV" ] && [ -f "${EV}request.csv" ] && ok=$((ok + 1))
[ -n "$EV" ] && [ -f "${EV}chrome_tracing.json" ] && ok=$((ok + 1))
python3 -c "import json;json.dump({'success':bool($ok>=2),'artifacts':$ok},open('$CASE_DIR/results/result.json','w'),indent=2)"
exit $((ok >= 2 ? 0 : 1))
