#!/usr/bin/env bash
# batch-analysis — 宿主机端到端：DemoApp（慢接口 + 阻塞线程）→ 批处理采集 → 证据断言
# 环境不满足（无 java/javac 或 arthas 不可得）退出 3 并说明。
set -uo pipefail
CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
S="$(cd "$CASE_DIR/../.." && pwd)/scripts"
cd "$CASE_DIR"
mkdir -p "$CASE_DIR/results"

write_result() { # <evidence_passed 0|1> <reason>
    python3 - "$1" "$2" <<'PY'
import json, sys
passed, reason = sys.argv[1] == "1", sys.argv[2]
json.dump({"success": passed, "evidence_passed": passed, "reason": reason},
          open("results/result.json", "w"), indent=2)
PY
}

if ! command -v java >/dev/null 2>&1; then
    echo "SKIP(环境不满足): 需要 java（JDK 11+）" >&2
    python3 -c "import json;json.dump({'success':False,'evidence_passed':False,'reason':'no-java'},open('results/result.json','w'),indent=2)"
    exit 3
fi
if ! command -v javac >/dev/null 2>&1; then
    echo "SKIP(环境不满足): 需要 javac（JDK 开发包，JRE 不够）" >&2
    python3 -c "import json;json.dump({'success':False,'evidence_passed':False,'reason':'no-javac'},open('results/result.json','w'),indent=2)"
    exit 3
fi

# arthas-boot.jar 未命中时先联网安装；仍失败则退出 3
bash "$S/discover.sh" | grep -q "arthas-boot.jar: READY" || {
    bash "$S/deploy.sh" || {
        echo "SKIP(环境不满足): arthas-boot.jar 不可得且无法联网安装" >&2
        python3 -c "import json;json.dump({'success':False,'evidence_passed':False,'reason':'no-arthas'},open('results/result.json','w'),indent=2)"
        exit 3
    }
}

# workload：api-worker 慢接口（io 120ms 为最耗时段）+ lock-waiter BLOCKED
cat > "$CASE_DIR/results/DemoApp.java" <<'JAVA'
public class DemoApp {
    static final Object LOCK = new Object();
    static void sleep(long ms) {
        try { Thread.sleep(ms); } catch (InterruptedException ignored) {}
    }
    static void parse(int id)  { sleep(40); }
    static void io(int id)     { sleep(120); }
    static int handle(int id) { parse(id); io(id); return id; }
    public static void main(String[] args) throws Exception {
        Thread apiWorker = new Thread(() -> {
            while (true) { handle((int)(System.nanoTime() % 100)); sleep(50); }
        }, "api-worker");
        Thread lockHolder = new Thread(() -> {
            synchronized (LOCK) { sleep(300000L); }
        }, "lock-holder");
        Thread lockWaiter = new Thread(() -> {
            synchronized (LOCK) { }
        }, "lock-waiter");
        apiWorker.start(); lockHolder.start();
        Thread.sleep(100);
        lockWaiter.start();
        Thread.sleep(300000L);
    }
}
JAVA
javac -d "$CASE_DIR/results" "$CASE_DIR/results/DemoApp.java" || {
    echo "SKIP(环境不满足): DemoApp 编译失败" >&2
    python3 -c "import json;json.dump({'success':False,'evidence_passed':False,'reason':'javac-failed'},open('results/result.json','w'),indent=2)"
    exit 3
}
java -cp "$CASE_DIR/results" DemoApp & JPID=$!
sleep 3
trap 'kill $JPID 2>/dev/null' EXIT

# 批处理采集：接口慢 trace + 阻塞线程
EVID=$(bash "$S/collect.sh" -p "$JPID" \
    -c "thread -n 3; trace DemoApp handle '#cost > 10' -n 2; thread --state BLOCKED" \
    -o "$CASE_DIR/results/evidence" --shutdown 2>"$CASE_DIR/results/collect.stderr")
RC=$?

ok=1
[ "$RC" -eq 0 ] && [ -n "$EVID" ] && [ -s "$EVID" ] || ok=0
grep -q "DemoApp.io" "$EVID" 2>/dev/null || ok=0        # trace 证据：最耗时段 io 可见
grep -q "lock-waiter" "$EVID" 2>/dev/null || ok=0       # BLOCKED 证据：阻塞线程可见

kill $JPID 2>/dev/null

TRACE_OK=0; grep -q "DemoApp.io" "$EVID" 2>/dev/null && TRACE_OK=1
BLOCKED_OK=0; grep -q "lock-waiter" "$EVID" 2>/dev/null && BLOCKED_OK=1
python3 - "$ok" "$TRACE_OK" "$BLOCKED_OK" "$RC" "$EVID" <<'PY'
import json, os, sys
ok, trace_ok, blocked_ok, rc = (int(sys.argv[i]) for i in range(1, 5))
evid = sys.argv[5]
json.dump({
    "success": ok == 1,
    "evidence_passed": ok == 1,
    "trace_segment_visible": trace_ok == 1,
    "blocked_thread_visible": blocked_ok == 1,
    "collect_exit": rc,
    "evidence_file": os.path.basename(evid) if evid else "",
}, open("results/result.json", "w"), indent=2)
PY
[ "$ok" -eq 1 ] && exit 0 || exit 1
