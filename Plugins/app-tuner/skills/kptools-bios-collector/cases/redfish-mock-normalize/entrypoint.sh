#!/usr/bin/env bash
set -uo pipefail
CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
S="$(cd "$CASE_DIR/../.." && pwd)/scripts"
mkdir -p "$CASE_DIR/results"
command -v openssl >/dev/null 2>&1 || { echo "openssl required" >&2; exit 2; }
openssl req -x509 -newkey rsa:2048 -keyout "$CASE_DIR/results/key.pem" -out "$CASE_DIR/results/cert.pem"     -days 1 -nodes -subj /CN=localhost >/dev/null 2>&1
python3 - "$CASE_DIR/results/cert.pem" "$CASE_DIR/results/key.pem" <<'PY' &
import http.server, ssl, sys, json
bios = {"Attributes": {"WorkloadProfile": "Performance", "SmtControl": "Enabled", "CStateControl": "Disabled"}}
systems = {"Members": [{"@odata.id": "/redfish/v1/Systems/1"}]}
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if "/Bios" in self.path:
            body = json.dumps(bios).encode()
        else:
            body = json.dumps(systems).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(sys.argv[1], sys.argv[2])
srv = http.server.HTTPServer(("127.0.0.1", 18443), H)
srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
srv.serve_forever()
PY
MOCKPID=$!
sleep 1
# ① 采集断言：collect.sh --redfish 全链（/Systems → member → /Bios + Settings）
bash "$S/collect.sh" --redfish --bmc-host 127.0.0.1:18443 -u mock -p mock > "$CASE_DIR/results/redfish.txt" 2>&1
collect_ok=$(grep -c '/Bios/Settings' "$CASE_DIR/results/redfish.txt")
# ② 归一化断言：干净 JSON（直接 curl mock，避免 stdout 文本提取的脆弱性）
curl -sk -u mock:mock https://127.0.0.1:18443/redfish/v1/Systems/1/Bios -o "$CASE_DIR/results/bios.json"
kill $MOCKPID 2>/dev/null
bash "$S/redfish_attrs.sh" -f "$CASE_DIR/results/bios.json" > "$CASE_DIR/results/attrs.txt" 2>&1
ok=0
grep -q 'power_profile' "$CASE_DIR/results/attrs.txt" && ok=$((ok + 1))
[ "${collect_ok:-0}" -ge 1 ] && ok=$((ok + 1))
python3 -c "import json;json.dump({'success':bool($ok>=2),'power_profile_present':bool($ok>=2),'collection_chain':bool(${collect_ok:-0}>=1)},open('$CASE_DIR/results/result.json','w'),indent=2)"
[ "$ok" -ge 2 ] && exit 0 || exit 1
