#!/usr/bin/env bash
# kptools-jvmjit collect.sh — JIT 符号化采样：record(-k 1) → flush → inject --jit → report 导出
# 宿主机模式: 采集与注入在本机执行
# 容器模式(--container): 采集/注入/报告全部在容器内执行，产物 docker cp 回宿主输出目录
# 退出码: 0 成功; 2 先决缺失(agent 未加载/PID 非 JVM/容器内无 perf); 3 参数错误; 1 采集失败
set -uo pipefail

PID="" DURATION=8 EVENTS="cycles,instructions" OUT="" CONTAINER=""
TOOLS="/tmp/jvmjit-tools" DUMP="" CALL_GRAPH=0 STOP_JVM=0
ANN_TIMEOUT=10 ANN_LIMIT=1
AGENT_PATH="${JVMJIT_AGENT:-/usr/lib64/libperf-jvmti.so}"

usage() { echo "用法: collect.sh -p <PID> [-d 秒] [-e 事件] [-o 目录] [--container CTR] [--tools DIR] [--call-graph] [--dump PATH] [--stop-jvm] [--annotate-timeout 秒]"; }

while [ $# -gt 0 ]; do
    case "$1" in
        -p) PID="${2:?}"; shift 2 ;;
        -d) DURATION="$2"; shift 2 ;;
        -e) EVENTS="$2"; shift 2 ;;
        -o) OUT="$2"; shift 2 ;;
        --container) CONTAINER="${2:?}"; shift 2 ;;
        --tools) TOOLS="$2"; shift 2 ;;
        --call-graph) CALL_GRAPH=1; shift ;;
        --dump) DUMP="$2"; shift 2 ;;
        --stop-jvm) STOP_JVM=1; shift ;;
        --annotate-timeout) ANN_TIMEOUT="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "未知参数: $1" >&2; exit 3 ;;
    esac
done
[ -z "$PID" ] && { usage >&2; exit 3; }
[[ "$PID" =~ ^[0-9]+$ ]] || { echo "FAIL: PID 须为数字" >&2; exit 3; }
# -d / --annotate-timeout fail-closed 数值校验（纯正整数）：
# GNU sleep 接受 8s/8.5 等写法会正常跑完采集，但 manifest 的 int() 转换会失败，
# 必须在入口拦截而非静默产出缺失 manifest 的"成功"
for _num in "$DURATION" "$ANN_TIMEOUT"; do
    [[ "$_num" =~ ^[1-9][0-9]*$ ]] || { echo "FAIL: -d/--annotate-timeout 须为正整数（秒）: $_num" >&2; exit 3; }
done
# --tools fail-closed 校验：TOOLS 会拼入容器侧命令（PERF_ENV/PERF_BIN），拒绝 shell 元字符
if ! [[ "$TOOLS" =~ ^/[A-Za-z0-9._/-]+$ ]]; then
    echo "FAIL: --tools 须为绝对路径且仅含 [A-Za-z0-9._/-] 字符: $TOOLS" >&2
    exit 3
fi

TS=$(date +%Y%m%d_%H%M%S)
OUT="${OUT:-./jvmjit-evidence/$TS}"
SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# 目标侧命令包装：容器模式经 docker exec，宿主机直接执行
run_target() {
    if [ -n "$CONTAINER" ]; then docker exec "$CONTAINER" bash -c "$1"; else bash -c "$1"; fi
}
# 解析目标侧 perf 命令（容器内 PATH 无 perf 时用注入目录，toolpack.sh 部署位）
# PERF_ENV/PERF_BIN 分离：annotate 需在 env 前缀与二进制之间插入 timeout，
# 若作前缀拼接，bash 会把 `VAR=x` 当作 timeout 的参数而非环境前缀
PERF_ENV=""; PERF_BIN="perf"
if [ -n "$CONTAINER" ]; then
    if run_target "command -v perf >/dev/null 2>&1"; then
        PERF_BIN="perf"
    else
        PERF_ENV="LD_LIBRARY_PATH=$TOOLS/lib"; PERF_BIN="$TOOLS/perf"
    fi
fi
PERF_RUN="${PERF_ENV:+$PERF_ENV }$PERF_BIN"

# ---------- 先决 ----------
CMDLINE=$(run_target "tr '\\0' ' ' < /proc/$PID/cmdline 2>/dev/null") || CMDLINE=""
[ -z "$CMDLINE" ] && { echo "FAIL: PID $PID 不存在（容器模式须用容器内 PID）" >&2; exit 2; }
echo "$CMDLINE" | grep -qiE '(^|/)java|jvm' || { echo "FAIL: PID $PID 非 JVM 进程，按 SKILL.md 职责边界路由" >&2; exit 2; }
if ! echo "$CMDLINE" | grep -q -- '-agentpath'; then
    echo "FAIL: JVM 启动参数无 -agentpath（agent 无法运行期补加）。" >&2
    echo "修复: 启动参数追加 -agentpath:$AGENT_PATH 后重启；容器场景先把 agent cp 进容器。详见 $SKILL_DIR/references/jvmjit-guide.md §2" >&2
    exit 2
fi
if [ "$CALL_GRAPH" -eq 1 ] && ! echo "$CMDLINE" | grep -q 'PreserveFramePointer'; then
    echo "FAIL: --call-graph 需要 JVM 启动参数含 -XX:+PreserveFramePointer（JVMTI agent 只解决符号不解决栈回溯），或去掉 --call-graph 用 flat 模式" >&2
    exit 2
fi

# dump 自动发现：新版 $HOME/.debug/jit/java-jit-*/jit-<pid>.dump，旧版 /tmp/perf-<pid>.dump
if [ -z "$DUMP" ]; then
    DUMP=$(run_target "ls -t \$HOME/.debug/jit/java-jit-*/jit-$PID.dump /tmp/perf-$PID.dump 2>/dev/null | head -1" || true)
fi
[ -z "$DUMP" ] && { echo "FAIL: 未发现 jitdump（agent 未加载或 HOME 变化）。从 JVM 启动日志 'java: jvmti: jitdump in <path>' 取路径后 --dump 显式指定" >&2; exit 2; }
run_target "[ -f '$DUMP' ]" >/dev/null 2>&1 || { echo "FAIL: dump 文件不存在: $DUMP" >&2; exit 2; }

mkdir -p "$OUT"
if [ -n "$CONTAINER" ]; then
    CTR_OUT="/tmp/jvmjit-evidence-$TS"
    run_target "mkdir -p $CTR_OUT" >/dev/null || exit 1
    RAW="$CTR_OUT/raw.data"; JIT="$CTR_OUT/jitted.data"
else
    RAW="$OUT/raw.data"; JIT="$OUT/jitted.data"
fi

echo "== 采集: PID=$PID 时长=${DURATION}s 事件=$EVENTS dump=$DUMP 模式=${CONTAINER:+容器:$CONTAINER}${CONTAINER:-宿主机} =="

# ---------- record（-k 1 为 inject --jit 强制要求，见 guide §3.1） ----------
GC_ARGS=""
[ "$CALL_GRAPH" -eq 1 ] && GC_ARGS="-g --call-graph fp"
RECORD_OUT=$(run_target "$PERF_RUN record -k 1 -p $PID -e $EVENTS $GC_ARGS -o '$RAW' -- sleep $DURATION" 2>&1) \
    || { echo "FAIL: perf record 失败（容器内 EPERM → 容器需 --cap-add=PERFMON/SYS_ADMIN，guide §5.1）" >&2; echo "$RECORD_OUT" >&2; exit 1; }
echo "$RECORD_OUT" | grep -E "Captured and wrote|samples" || true

DUMP_SIZE_BEFORE=$(run_target "stat -c %s '$DUMP'" 2>/dev/null || echo 0)

# ---------- flush：测试环境 --stop-jvm 触发全量刷盘 ----------
if [ "$STOP_JVM" -eq 1 ]; then
    run_target "kill -TERM $PID" >/dev/null 2>&1 || true
    for _ in $(seq 1 10); do run_target "kill -0 $PID" >/dev/null 2>&1 || break; sleep 1; done
    echo "NOTICE: --stop-jvm 已优雅退出目标 JVM（dump 全量刷盘）。生产环境禁用此参数。" >&2
fi

# ---------- inject + report ----------
INJECT_OUT=$(run_target "$PERF_RUN inject -i '$RAW' --jit -o '$JIT'" 2>&1) \
    || { echo "FAIL: perf inject --jit 失败（若报 'must be sampled with perf record -k 1' 检查 record 命令）" >&2; echo "$INJECT_OUT" >&2; exit 1; }
REPORT_OUT=$(run_target "$PERF_RUN report -i '$JIT' --stdio --no-children --percent-limit 0.5" 2>/dev/null) \
    || { echo "FAIL: perf report 失败" >&2; exit 1; }
# report 输出在采集侧捕获后统一落盘宿主 $OUT（容器模式同样直接可得）
printf '%s\n' "$REPORT_OUT" > "$OUT/report.txt"
[ -s "$OUT/report.txt" ] || { echo "FAIL: report 输出为空" >&2; exit 1; }

# ---------- 指令级产物：jitted ELF 清单（dump 同目录） ----------
DUMP_DIR=$(dirname "$DUMP")
HOT_SO=$(grep -oE "jitted-$PID-[0-9]+\.so" "$OUT/report.txt" | sort -u | head -5)

# ---------- [JIT] 残留检测（dump 滞后信号，见 guide §4） ----------
TOTAL_ROWS=$(grep -cE '^\s+[0-9.]+%' "$OUT/report.txt" || true)
JIT_ROWS=$(grep -cE '\[JIT\]' "$OUT/report.txt" || true)
RESIDUE=0
[ "$TOTAL_ROWS" -gt 0 ] && RESIDUE=$(( JIT_ROWS * 100 / TOTAL_ROWS ))
[ "$JIT_ROWS" -gt 0 ] && echo "NOTICE: report 有 $JIT_ROWS 行 [JIT] 裸地址（占热点行 ${RESIDUE}%）= dump 滞后。测试环境用 --stop-jvm；生产等优雅重启窗口刷盘后对同一 raw.data 重新 inject。guide §4" >&2

# ---------- 指令级注解导出：perf annotate（源码行 + 汇编 + 逐指令周期占比） ----------
# 全符号反汇编较慢（实测 10MB/58k 样本 <45s），默认 10s 保守限时避免拖长采集流程；
# 大数据集/需要完整注解时显式提高 --annotate-timeout；超时/失败保留部分输出，不判失败
ANN_SRC="$OUT/annotate.txt"
if [ -n "$CONTAINER" ]; then ANN_SRC="$CTR_OUT/annotate.txt"; fi
run_target "${PERF_ENV:+$PERF_ENV }timeout $ANN_TIMEOUT $PERF_BIN annotate -i '$JIT' --stdio --percent-limit $ANN_LIMIT > '$ANN_SRC' 2>/dev/null" \
    || echo "NOTICE: perf annotate 未在 ${ANN_TIMEOUT}s 内完成或失败，annotate.txt 保留部分输出；单函数快速路径用 objdump 热点 jitted-*.so（guide §6）" >&2
if [ -n "$CONTAINER" ]; then
    docker cp "$CONTAINER:$ANN_SRC" "$OUT/annotate.txt" >/dev/null 2>&1 || true
fi
ANN_LINES=$(wc -l < "$OUT/annotate.txt" 2>/dev/null || echo 0)

# ---------- 容器产物回传（docker cp 须带 容器名: 前缀） ----------
if [ -n "$CONTAINER" ]; then
    docker cp "$CONTAINER:$RAW" "$OUT/raw.data" >/dev/null 2>&1 || echo "WARN: raw.data 回传失败" >&2
    docker cp "$CONTAINER:$JIT" "$OUT/jitted.data" >/dev/null 2>&1 || true
    for f in $HOT_SO; do
        docker cp "$CONTAINER:$DUMP_DIR/$f" "$OUT/" >/dev/null 2>&1 || true
    done
    run_target "rm -rf $CTR_OUT" >/dev/null 2>&1 || true
fi

# ---------- manifest ----------
# 生成失败必须 fail-closed：manifest 是 SKILL.md 声明的核心产物（残留判据/热点清单），
# 缺失时禁止打印「完成」并以 0 退出
python3 - "$OUT" "$PID" "$DURATION" "$EVENTS" "$DUMP" "$DUMP_SIZE_BEFORE" "$RESIDUE" "$HOT_SO" "$ANN_LINES" <<'PY' || { echo "FAIL: manifest.json 生成失败（采集产物不完整）" >&2; exit 1; }
import json, sys, glob, os
out, pid, dur, events, dump, size_before, residue, hot, ann_lines = sys.argv[1:10]
manifest = {
    "skill": "kptools-jvmjit",
    "pid": int(pid), "duration_s": int(dur), "events": events,
    "dump": dump, "dump_size_at_record_end": int(size_before),
    "jit_residue_pct": int(residue),
    "jit_residue_hint": "dump 滞后：--stop-jvm 或优雅重启后重新 inject" if int(residue) > 20 else "",
    "hot_jitted_elfs": hot.split(),
    "annotate_lines": int(ann_lines),
    "artifacts": sorted(os.path.basename(p) for p in glob.glob(os.path.join(out, "*")) if os.path.isfile(p)),
}
json.dump(manifest, open(os.path.join(out, "manifest.json"), "w"), indent=2, ensure_ascii=False)
PY
[ -s "$OUT/manifest.json" ] || { echo "FAIL: manifest.json 缺失或为空" >&2; exit 1; }

echo "== 完成: $OUT =="
echo "方法级热点: $OUT/report.txt"
[ "$ANN_LINES" -gt 0 ] && echo "指令级注解: $OUT/annotate.txt（${ANN_LINES} 行，源码+汇编+逐指令周期占比）"
[ -n "$HOT_SO" ] && echo "热点 jitted ELF（objdump -d 可下钻指令级）: $HOT_SO"
[ -n "$CONTAINER" ] && echo "容器内 dump（含全部 jitted-*.so）: $DUMP_DIR"
exit 0
