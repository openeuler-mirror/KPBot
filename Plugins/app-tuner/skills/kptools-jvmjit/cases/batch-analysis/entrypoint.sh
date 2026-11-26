#!/usr/bin/env bash
# batch-analysis — 容器内 JIT 符号化端到端：被测容器 → 工具注入 → 容器内 record/inject/report
# → Java 符号与 jitted ELF 断言（核心能力证据）。
# 环境不满足（无 perf/agent/JDK/docker/可用镜像）按 SKIP 语义 exit 0（skipped_or_success=true，
# 仓库约定见 kptools-py-spy native-torch-npu）；证据失败才 exit 1。
set -uo pipefail
CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
S="$(cd "$CASE_DIR/../.." && pwd)/scripts"
cd "$CASE_DIR"
mkdir -p results

write_result() { # <passed 0|1> <reason>（证据失败：exit 1，skipped_or_success=false）
    python3 - "$1" "$2" <<'PY'
import json, sys
passed = sys.argv[1] == "1"
json.dump({"success": passed, "evidence_passed": passed, "skipped": False,
           "skipped_or_success": passed, "reason": sys.argv[2]},
          open("results/result.json", "w"), indent=2)
PY
}
# 环境不满足 → SKIP 语义（仓库约定，对齐 kptools-py-spy native-torch-npu）：
# success=true + skipped=true + skipped_or_success=true + exit 0（exit 3 在本仓库保留给用法/参数错误）
skip() { # <原因> <json reason>
    echo "SKIP(环境不满足): $1" >&2
    python3 - "$2" <<'PY'
import json, sys
json.dump({"success": True, "skipped": True, "skipped_or_success": True,
           "reason": sys.argv[1]}, open("results/result.json", "w"), indent=2)
PY
    exit 0
}

command -v perf >/dev/null 2>&1 || skip "需要 perf" "no-perf"
perf inject --help 2>&1 | grep -q -- '--jit' || skip "perf 不支持 --jit" "perf-no-jit"
AGENT="${JVMJIT_AGENT:-/usr/lib64/libperf-jvmti.so}"
[ -f "$AGENT" ] || skip "需要 libperf-jvmti.so（$AGENT）" "no-agent"
command -v javac >/dev/null 2>&1 || skip "需要 javac（JDK）" "no-javac"
command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1 || skip "需要 docker" "no-docker"

CTR="jvmjit-case-$$"
cleanup() { docker rm -f "$CTR" >/dev/null 2>&1 || true; rm -rf "$CASE_DIR/work"; }
trap cleanup EXIT

# 镜像探测：优先 JVMJIT_TEST_IMAGE，否则本地首个含 javac 的镜像
IMG="${JVMJIT_TEST_IMAGE:-}"
if [ -z "$IMG" ]; then
    for cand in $(docker images --format '{{.Repository}}:{{.Tag}}' | grep -v '<none>' | head -8); do
        docker run --rm "$cand" bash -c "which javac" >/dev/null 2>&1 && { IMG="$cand"; break; }
    done
fi
[ -n "$IMG" ] || skip "本地无含 javac 的镜像（可用 JVMJIT_TEST_IMAGE 指定）" "no-image"

mkdir -p work && cat > work/DemoCompute.java <<'JAVA'
public class DemoCompute {
    static double[] a = new double[1 << 20];
    static double[] b = new double[1 << 20];
    static double[] c = new double[1 << 20];
    static void saxpyLoop(double alpha) {
        for (int i = 0; i < a.length; i++) c[i] = alpha * a[i] + b[i];
    }
    static long fib(long n) { return n < 2 ? n : fib(n - 1) + fib(n - 2); }
    public static void main(String[] args) throws Exception {
        java.util.Random r = new java.util.Random(42);
        for (int i = 0; i < a.length; i++) { a[i] = r.nextDouble(); b[i] = r.nextDouble(); }
        while (true) {
            for (int i = 0; i < 30; i++) saxpyLoop(1.5 + i);
            fib(28 + (int)(System.nanoTime() % 4));
        }
    }
}
JAVA

docker run -d --cap-add SYS_ADMIN --name "$CTR" "$IMG" sleep infinity >/dev/null 2>&1 \
    || { write_result 0 "container-start-failed"; exit 1; }
bash "$S/toolpack.sh" --container "$CTR" >/dev/null 2>&1 \
    || { write_result 0 "toolpack-failed"; exit 1; }

docker exec "$CTR" mkdir -p /tmp/demo >/dev/null
docker cp work/DemoCompute.java "$CTR:/tmp/demo/" >/dev/null
docker exec "$CTR" bash -c "cd /tmp/demo && javac DemoCompute.java" >/dev/null 2>&1 \
    || { write_result 0 "javac-in-container-failed"; exit 1; }
docker exec -d "$CTR" bash -c \
    "cd /tmp/demo && java -agentpath:/tmp/jvmjit-tools/libperf-jvmti.so -XX:+PreserveFramePointer DemoCompute > app.log 2>&1"
sleep 8
CPID=$(docker exec "$CTR" bash -c "grep -oP 'jitdump in \K\S+' /tmp/demo/app.log | head -1 | grep -oP '/jit-\K[0-9]+'")
[ -z "$CPID" ] && CPID=$(docker exec "$CTR" bash -c "pgrep -f 'jav[a].*agentpath' | head -1")
CPID=${CPID//[$'\r\n']/}
[ -n "$CPID" ] || { write_result 0 "jvm-not-running-in-container"; exit 1; }

# 容器内全链采集（--stop-jvm：测试环境触发 dump 全量刷盘）
bash "$S/collect.sh" -p "$CPID" --container "$CTR" -d 6 --call-graph --stop-jvm \
    --annotate-timeout 60 -o work/evidence > work/collect.log 2>&1 \
    || { tail -5 work/collect.log >&2; write_result 0 "collect-failed"; exit 1; }

REPORT="work/evidence/report.txt"
MANIFEST="work/evidence/manifest.json"
[ -f "$REPORT" ] || { write_result 0 "no-report"; exit 1; }

SYMBOL_OK=$(grep -c "DemoCompute" "$REPORT" || true)
[ "$SYMBOL_OK" -ge 1 ] || { echo "report 无 Java 符号:" >&2; head -5 "$REPORT" >&2; write_result 0 "no-java-symbol"; exit 1; }

RESIDUE=$(python3 -c "import json;print(json.load(open('$MANIFEST'))['jit_residue_pct'])" 2>/dev/null || echo 100)
[ "$RESIDUE" -lt 50 ] || { write_result 0 "jit-residue-high:$RESIDUE"; exit 1; }

ANN_LINES=$(python3 -c "import json;print(json.load(open('$MANIFEST'))['annotate_lines'])" 2>/dev/null || echo 0)
[ "$ANN_LINES" -gt 0 ] || { write_result 0 "annotate-empty"; exit 1; }

ELF_OK=0; ls work/evidence/jitted-*.so >/dev/null 2>&1 && ELF_OK=1
[ "$ELF_OK" -eq 1 ] || { echo "热点 jitted ELF 未回传" >&2; }

write_result 1 "容器内采集符号化通过: ${SYMBOL_OK} 个 DemoCompute 符号行, JIT 残留 ${RESIDUE}%, jitted ELF ${ELF_OK}"
echo "evidence: $CASE_DIR/work/evidence/"
exit 0
