#!/usr/bin/env bash
set -uo pipefail
CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
S="$(cd "$CASE_DIR/../.." && pwd)/scripts"
mkdir -p "$CASE_DIR/results"
if [ -z "${REDFISH_HOST:-}" ]; then
    python3 -c "import json;json.dump({'success':True,'skipped':True,'skipped_or_success':True,'reason':'BMC env not provided'},open('$CASE_DIR/results/result.json','w'),indent=2)"
    exit 0
fi
bash "$S/collect.sh" --redfish --bmc-host "${REDFISH_HOST}" -u "${REDFISH_USER}" -p "${REDFISH_PASS}"     > "$CASE_DIR/results/redfish.txt" 2>&1
ok=0
grep -q '/Bios/Settings' "$CASE_DIR/results/redfish.txt" && ok=1
python3 -c "import json;json.dump({'success':bool($ok),'skipped':False,'skipped_or_success':bool($ok)},open('$CASE_DIR/results/result.json','w'),indent=2)"
[ "$ok" -eq 1 ] && exit 0 || exit 1
