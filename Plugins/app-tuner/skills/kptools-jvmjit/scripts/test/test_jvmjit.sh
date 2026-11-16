#!/usr/bin/env bash
# kptools-jvmjit 自测：静态断言 + 宿主机端到端 + 容器端到端（核心场景）
# 环境不满足（无 java/perf/docker/可用镜像）的用例 SKIP，退出码 3。
# 用法: test_jvmjit.sh [--no-container]（CI 无 docker 时跳过容器用例）
set -uo pipefail
S="$(cd "$(dirname "$0")/../.." && pwd)"   # skill 根目录
T="$(cd "$(dirname "$0")" && pwd)"
PASS=0; FAIL=0; SKIP=0; CLEANUPS=(); WORK=""
NO_CONTAINER=0
[ "${1:-}" = "--no-container" ] && NO_CONTAINER=1

ok()   { PASS=$((PASS+1)); echo "PASS: $*"; }
bad()  { FAIL=$((FAIL+1)); echo "FAIL: $*" >&2; }
skip() { SKIP=$((SKIP+1)); echo "SKIP: $*" >&2; }
on_cleanup() { CLEANUPS+=("$1"); }
cleanup() { for c in "${CLEANUPS[@]:-}"; do eval "$c" >/dev/null 2>&1 || true; done; }
trap cleanup EXIT

mkdir -p /tmp/jvmjit-test-src
cat > /tmp/jvmjit-test-src/DemoCompute.java <<'JAVA'
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

# ---------- 1. 静态/参数断言 ----------
for f in "$S/SKILL.md" "$S/references/jvmjit-guide.md" "$S/references/jvmjit-vs-other-tools.md" \
         "$S/scripts/check.sh" "$S/scripts/collect.sh" "$S/scripts/toolpack.sh"; do
    [ -s "$f" ] && ok "文件存在: ${f#$S/}" || bad "文件缺失: $f"
done
for f in "$S/scripts/check.sh" "$S/scripts/collect.sh" "$S/scripts/toolpack.sh"; do
    bash -n "$f" 2>/dev/null && ok "语法: ${f##*/}" || bad "语法错误: $f"
done
python3 -c "
import json
for p in ['$S/evals/evals.json', '$S/evals/eval_queries.json']:
    d = json.load(open(p))
    assert 'skill_name' in d and ('evals' in d or 'queries' in d), p
print('json-ok')" >/dev/null 2>&1 && ok "evals JSON 有效" || bad "evals JSON 无效"

bash "$S/scripts/check.sh" --bogus >/dev/null 2>&1; [ $? -eq 3 ] && ok "check.sh 未知参数退出 3" || bad "check.sh 未知参数退出码错误"
bash "$S/scripts/collect.sh" >/dev/null 2>&1; [ $? -eq 3 ] && ok "collect.sh 无参退出 3" || bad "collect.sh 无参退出码错误"
bash "$S/scripts/toolpack.sh" >/dev/null 2>&1; [ $? -eq 3 ] && ok "toolpack.sh 无参退出 3" || bad "toolpack.sh 无参退出码错误"

OUT=$(bash "$S/scripts/check.sh" --agent /nonexistent/libperf-jvmti.so 2>&1 || true)
echo "$OUT" | grep -q "agent 不存在" && ok "check.sh 检出 agent 缺失" || bad "check.sh 未检出 agent 缺失"
OUT=$(bash "$S/scripts/check.sh" -p 999999 2>&1 || true)
echo "$OUT" | grep -q "不存在" && ok "check.sh 检出 PID 不存在" || bad "check.sh 未检出 PID 不存在"

if grep -q -- '-k 1' "$S/scripts/collect.sh"; then ok "collect.sh 内置 -k 1"; else bad "collect.sh 缺 -k 1"; fi
if grep -q 'PreserveFramePointer' "$S/scripts/collect.sh"; then ok "collect.sh 校验帧指针先决"; else bad "collect.sh 缺帧指针校验"; fi
if grep -qE 'libc\.so|libpthread' "$S/scripts/toolpack.sh"; then ok "toolpack.sh 排除 libc 族"; else bad "toolpack.sh 未排除 libc 族"; fi

# 注入防护（fail-closed 校验：check.sh PID / toolpack.sh --dest / collect.sh --tools）
bash "$S/scripts/check.sh" --container x -p '1;id' >/dev/null 2>&1; [ $? -eq 3 ] && ok "check.sh 拒绝非数字 PID（注入防护）" || bad "check.sh 未拒绝非数字 PID"
OUT=$(bash "$S/scripts/toolpack.sh" --container x --dest '/tmp/x;id' 2>&1 || true)
echo "$OUT" | grep -q "须为绝对路径" && ok "toolpack.sh 拒绝含元字符 --dest" || bad "toolpack.sh 未拒绝危险 --dest"
bash "$S/scripts/collect.sh" -p 1 --tools '/tmp/x;id' >/dev/null 2>&1; [ $? -eq 3 ] && ok "collect.sh 拒绝含元字符 --tools" || bad "collect.sh 未拒绝危险 --tools"

# ---------- 2. 宿主机端到端 ----------
# 解析 JDK 工具（优先 PATH，回退常见 JDK 目录，java/javac 同源避免版本错配）
JAVAC_BIN=$(command -v javac || ls /usr/local/jdk/bin/javac /usr/lib/jvm/*/bin/javac 2>/dev/null | head -1 || true)
JAVA_BIN=$(command -v java || ls /usr/local/jdk/bin/java /usr/lib/jvm/*/bin/java 2>/dev/null | head -1 || true)
if [ -z "$JAVAC_BIN" ] || [ -z "$JAVA_BIN" ] || ! command -v perf >/dev/null 2>&1; then
    skip "宿主机端到端：需要 java + javac + perf"
else
    WORK=$(mktemp -d /tmp/jvmjit-test-host.XXXXXX); on_cleanup "rm -rf $WORK; pkill -f jvmjit-test-src 2>/dev/null"
    "$JAVAC_BIN" /tmp/jvmjit-test-src/DemoCompute.java -d "$WORK" 2>/dev/null \
        && ok "宿主机 DemoCompute 编译" || { skip "javac 编译失败（需 JDK 非 JRE）"; }
    if [ -f "$WORK/DemoCompute.class" ]; then
        AGENT="${JVMJIT_AGENT:-/usr/lib64/libperf-jvmti.so}"
        if [ ! -f "$AGENT" ]; then
            skip "宿主机端到端：agent 不存在（$AGENT）"
        else
            ( cd "$WORK" && setsid nohup "$JAVA_BIN" -agentpath:"$AGENT" -XX:+PreserveFramePointer -cp "$WORK" DemoCompute > "$WORK/app.log" 2>&1 < /dev/null & ) ; sleep 6
            HPID=$(pgrep -f "cp.*DemoCompute|jvmjit-test-src" | head -1)
            if [ -n "$HPID" ] && bash "$S/scripts/check.sh" -p "$HPID" >/dev/null 2>&1; then
                ok "check.sh 先决全通过（宿主机 JVM）"
                if bash "$S/scripts/collect.sh" -p "$HPID" -d 6 --call-graph --stop-jvm --annotate-timeout 60 -o "$WORK/out" >"$WORK/collect.log" 2>&1; then
                    ok "collect.sh 宿主机模式退出 0"
                    grep -q "DemoCompute" "$WORK/out/report.txt" && ok "report.txt 含 DemoCompute 符号" || bad "report.txt 无 Java 符号"
                    grep -qE "jitted-[0-9]+-[0-9]+\.so" "$WORK/out/report.txt" && ok "report.txt 引用 jitted ELF" || bad "report.txt 无 jitted ELF"
                    grep -q "Disassembly of" "$WORK/out/annotate.txt" 2>/dev/null && ok "annotate.txt 指令级注解有效（源码+汇编）" || bad "annotate.txt 无效"
                    python3 -c "import json;m=json.load(open('$WORK/out/manifest.json'));assert m['pid']>0 and 'dump' in m and m['annotate_lines']>0" >/dev/null 2>&1 \
                        && ok "manifest.json 结构有效（含 annotate_lines）" || bad "manifest.json 无效"
                    RESIDUE=$(python3 -c "import json;print(json.load(open('$WORK/out/manifest.json'))['jit_residue_pct'])" 2>/dev/null || echo 100)
                    [ "$RESIDUE" -lt 50 ] && ok "JIT 残留 ${RESIDUE}%（刷盘后收敛）" || bad "JIT 残留 ${RESIDUE}% 过高（刷盘语义异常）"
                else
                    bad "collect.sh 宿主机模式失败"; tail -5 "$WORK/collect.log" >&2
                fi
            else
                bad "check.sh 宿主机先决未通过（agentpath/帧指针检查异常）"
            fi
            pkill -f DemoCompute >/dev/null 2>&1 || true
        fi
    fi
fi

# ---------- 3. 容器端到端（核心场景：采集必须在容器内） ----------
if [ "$NO_CONTAINER" -eq 1 ]; then
    skip "容器端到端：--no-container 指定跳过"
elif ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    skip "容器端到端：docker 不可用"
else
    # 选镜像：优先 JVMJIT_TEST_IMAGE；否则找本地第一个带 javac 的镜像
    CTR="jvmjit-test-ctr-$$"; on_cleanup "docker rm -f $CTR"
    [ -z "$WORK" ] && { WORK=$(mktemp -d /tmp/jvmjit-test-ctr.XXXXXX); on_cleanup "rm -rf $WORK"; }
    IMG="${JVMJIT_TEST_IMAGE:-}"
    if [ -z "$IMG" ]; then
        for cand in $(docker images --format '{{.Repository}}:{{.Tag}}' | grep -v '<none>' | head -8); do
            if timeout 90 docker run --rm "$cand" bash -c "which javac" >/dev/null 2>&1; then IMG="$cand"; break; fi
        done
    fi
    if [ -z "$IMG" ]; then
        skip "容器端到端：本地无含 javac 的镜像（可设 JVMJIT_TEST_IMAGE 指定）"
    else
        ok "测试镜像: $IMG"
        if docker run -d --cap-add SYS_ADMIN --name "$CTR" "$IMG" sleep infinity >/dev/null 2>&1; then
            ok "容器已启动（--cap-add SYS_ADMIN）"
            if bash "$S/scripts/toolpack.sh" --container "$CTR" >"$WORK/toolpack.log" 2>&1 \
               || bash "$S/scripts/toolpack.sh" --container "$CTR" >/dev/null 2>&1; then
                ok "toolpack.sh 工具注入成功"
                docker cp /tmp/jvmjit-test-src/DemoCompute.java "$CTR:/tmp/demo-src.java" >/dev/null 2>&1 || docker exec "$CTR" mkdir -p /tmp && docker cp /tmp/jvmjit-test-src/DemoCompute.java "$CTR:/tmp/demo-src.java" >/dev/null 2>&1
                docker exec "$CTR" bash -c "mkdir -p /tmp/demo && cp /tmp/demo-src.java /tmp/demo/DemoCompute.java && cd /tmp/demo && javac DemoCompute.java" >/dev/null 2>&1 \
                    && ok "容器内编译 DemoCompute" || bad "容器内编译失败"
                docker exec -d "$CTR" bash -c "cd /tmp/demo && java -agentpath:/tmp/jvmjit-tools/libperf-jvmti.so -XX:+PreserveFramePointer DemoCompute > app.log 2>&1"
                sleep 8
                CPID=$(docker exec "$CTR" bash -c "grep -oP 'jitdump in \K\S+' /tmp/demo/app.log | head -1 | grep -oP '/jit-\K[0-9]+'")
                [ -z "$CPID" ] && CPID=$(docker exec "$CTR" bash -c "pgrep -f 'jav[a].*agentpath' | head -1")
                CPID=${CPID//[$'\r\n']/}
                if [ -n "$CPID" ]; then
                    ok "容器内 JVM 运行（PID=$CPID）"
                    # P1 回归：容器模式 check.sh 必须在容器内读取 cmdline（宿主重定向缺陷曾致 100% 误报）
                    if bash "$S/scripts/check.sh" -p "$CPID" --container "$CTR" >"$WORK/ctr-check.log" 2>&1; then
                        ok "check.sh 容器模式先决通过（cmdline 容器内读取）"
                    else
                        bad "check.sh 容器模式失败"; tail -6 "$WORK/ctr-check.log" >&2
                    fi
                    if bash "$S/scripts/collect.sh" -p "$CPID" --container "$CTR" -d 6 --call-graph --stop-jvm --annotate-timeout 60 -o "$WORK/ctr-out" >"$WORK/ctr-collect.log" 2>&1; then
                        ok "collect.sh 容器模式退出 0"
                        grep -q "DemoCompute" "$WORK/ctr-out/report.txt" && ok "容器内采集：report 含 DemoCompute 符号" || bad "容器模式 report 无 Java 符号"
                        grep -q "Disassembly of" "$WORK/ctr-out/annotate.txt" 2>/dev/null && ok "容器模式 annotate.txt 指令级注解有效" || bad "容器模式 annotate.txt 无效"
                        ls "$WORK/ctr-out/"jitted-*.so >/dev/null 2>&1 && ok "热点 jitted ELF 已回传宿主" || bad "jitted ELF 未回传"
                        RESIDUE=$(python3 -c "import json;print(json.load(open('$WORK/ctr-out/manifest.json'))['jit_residue_pct'])" 2>/dev/null || echo 100)
                        [ "$RESIDUE" -lt 50 ] && ok "容器模式 JIT 残留 ${RESIDUE}%" || bad "容器模式 JIT 残留 ${RESIDUE}%"
                    else
                        bad "collect.sh 容器模式失败"; tail -8 "$WORK/ctr-collect.log" >&2
                    fi
                else
                    bad "容器内 JVM 未启动（agent 路径/javac 异常）"
                fi
            else
                bad "toolpack.sh 注入失败"; tail -5 "${WORK:-/dev/null}/toolpack.log" 2>/dev/null >&2 || true
            fi
        else
            bad "容器启动失败（--cap-add 不支持？）"
        fi
    fi
fi

echo "== 结果: PASS=$PASS FAIL=$FAIL SKIP=$SKIP =="
[ "$FAIL" -eq 0 ] && exit 0
exit 1
