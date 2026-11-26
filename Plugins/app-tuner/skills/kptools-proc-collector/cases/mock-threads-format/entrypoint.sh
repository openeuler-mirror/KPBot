#!/usr/bin/env bash
# tier A：PATH shim 伪造 ps -L 输出，验证 --threads 模式的表头与数据透传格式。
set -uo pipefail
CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
S="$(cd "$CASE_DIR/../.." && pwd)/scripts"
mkdir -p "$CASE_DIR/results/shim"

cat > "$CASE_DIR/results/shim/ps" <<'SH'
#!/usr/bin/env bash
# 只响应 -L -p <PID> -o <fmt> 形态
if [ "$1" = "-L" ] && [ "$2" = "-p" ]; then
cat <<'OUT'
  PID   TID PSR %CPU STAT COMMAND           WCHAN
     1     1   0  5.0 S    mockproc          -
     1     2   1  3.0 R    mockproc          -
     1     3   2  2.0 S    mockproc          -
OUT
    exit 0
fi
exit 1
SH
chmod +x "$CASE_DIR/results/shim/ps"

export PATH="$CASE_DIR/results/shim:$PATH"

ok=0; total=3

OUT=$(bash "$S/collect.sh" -p 1 --threads 2>/dev/null)
# 1) 应含注释行（模式说明）
echo "$OUT" | grep -q "^# ps -L" && ok=$((ok + 1))
# 2) 应含表头行
echo "$OUT" | grep -q "PID.*TID.*PSR" && ok=$((ok + 1))
# 3) 应含 3 个 mock 线程行
[ "$(echo "$OUT" | grep -c "mockproc")" = "3" ] && ok=$((ok + 1))

python3 -c "
import json
json.dump({'success': bool($ok >= $total), 'passed': $ok, 'total': $total},
          open('$CASE_DIR/results/result.json', 'w'), indent=2)
"
exit $((ok >= total ? 0 : 1))
