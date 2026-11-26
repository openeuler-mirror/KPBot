#!/usr/bin/env bash
# tier A：PATH shim 注入 mock netstat/ss，验证 --retrans 与 --sock 的解析聚合逻辑。
set -uo pipefail
CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
S="$(cd "$CASE_DIR/../.." && pwd)/scripts"
mkdir -p "$CASE_DIR/results/shim"

# shim netstat：-s 输出含已知 retrans 计数
cat > "$CASE_DIR/results/shim/netstat" <<'SH'
#!/usr/bin/env bash
if [ "$1" = "-s" ]; then
cat <<'OUT'
Tcp:
    12345 segments sent
    100 segments retransmitted
    5 listen drops
    2 listen overflows
OUT
    exit 0
fi
exit 0
SH
chmod +x "$CASE_DIR/results/shim/netstat"

# shim ss：-tinp 输出含已知 pid=4242 的 socket 队列与 retrans
cat > "$CASE_DIR/results/shim/ss" <<'SH'
#!/usr/bin/env bash
if [ "$1" = "-tinp" ]; then
cat <<'OUT'
State  Recv-Q  Send-Q  Local:Port  Peer:Port  Process
ESTAB  100     200     1.2.3.4:80  5.6.7.8:443 users:(("mock",pid=4242,fd=10)) retrans:3/500
ESTAB  50      60      1.2.3.4:80  9.8.7.6:443 users:(("mock",pid=4242,fd=11)) retrans:1/300
OUT
    exit 0
fi
exit 0
SH
chmod +x "$CASE_DIR/results/shim/ss"

export PATH="$CASE_DIR/results/shim:$PATH"

ok=0; total=4

# 1) --retrans：应输出 grep 后的 retrans 相关行
OUT_R=$(bash "$S/collect.sh" --retrans 2>/dev/null)
echo "$OUT_R" | grep -q "100 segments retransmitted" && ok=$((ok + 1))
echo "$OUT_R" | grep -q "5 listen drops" && ok=$((ok + 1))

# 2) --sock -p 4242：应聚合两个 socket 的队列与重传
OUT_S=$(bash "$S/collect.sh" --sock -p 4242 2>/dev/null)
echo "$OUT_S" | grep -q "socket_count=2" && ok=$((ok + 1))
echo "$OUT_S" | grep -q "retrans_segments=800" && ok=$((ok + 1))

python3 -c "
import json
json.dump({'success': bool($ok >= $total), 'passed': $ok, 'total': $total},
          open('$CASE_DIR/results/result.json', 'w'), indent=2)
"
exit $((ok >= total ? 0 : 1))
