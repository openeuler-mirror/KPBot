#!/usr/bin/env bash
# tier A：PATH shim 伪造 nmon 二进制（接受 -p -F -s -c -m 参数），验证
# collect.sh 的 PID 解析、快照等待（ZZZZ 计数）与数据文件输出链路。
set -uo pipefail
CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
S="$(cd "$CASE_DIR/../.." && pwd)/scripts"
mkdir -p "$CASE_DIR/results/shim" "$CASE_DIR/results/keep"

cat > "$CASE_DIR/results/shim/nmon" <<'SH'
#!/usr/bin/env bash
OUT_FILE=""; OUT_DIR=""; COUNT=1
for a in "$@"; do
    case "$a" in
        -F) NEXT="F" ;;
        -m) NEXT="m" ;;
        -s) NEXT="s" ;;
        -c) NEXT="c" ;;
        *) case "$NEXT" in
               F) OUT_FILE="$a"; NEXT="" ;;
               m) OUT_DIR="$a"; NEXT="" ;;
               c) COUNT="$a"; NEXT="" ;;
               s) NEXT="" ;;
           esac ;;
    esac
done
# -p 输出纯 PID（本进程）
echo $$
# 写 mock nmon CSV：含 COUNT 个 ZZZZ 快照行
{
    echo "AAA,host,mock"
    for i in $(seq 1 "$COUNT"); do
        echo "ZZZZ,mock,ts$i"
        echo "CPU01,10,20,70"
    done
} > "$OUT_DIR/$OUT_FILE"
exit 0
SH
chmod +x "$CASE_DIR/results/shim/nmon"

export PATH="$CASE_DIR/results/shim:$PATH"

ok=0; total=3

# snapshot 模式（-d 1 -i 1 = 1 快照），保留数据文件
OUT=$(bash "$S/collect.sh" --snapshot -m "$CASE_DIR/results/keep" 2>"$CASE_DIR/results/stderr.log")
# 1) stdout 应含 mock CSV 数据
echo "$OUT" | grep -q "CPU01,10,20,70" && ok=$((ok + 1))
# 2) 数据文件应在 keep 目录中保留
ls "$CASE_DIR/results/keep/"*.nmon 2>/dev/null | head -1 | grep -q "nmon_" && ok=$((ok + 1))
# 3) stderr 应含数据文件路径提示
grep -q "data file" "$CASE_DIR/results/stderr.log" 2>/dev/null || grep -q "\.nmon" "$CASE_DIR/results/stderr.log" 2>/dev/null && ok=$((ok + 1))

python3 -c "
import json
json.dump({'success': bool($ok >= $total), 'passed': $ok, 'total': $total},
          open('$CASE_DIR/results/result.json', 'w'), indent=2)
"
exit $((ok >= total ? 0 : 1))
