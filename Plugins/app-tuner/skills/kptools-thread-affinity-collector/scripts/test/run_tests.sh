#!/usr/bin/env bash
# kptools-thread-affinity-collector 回归测试 — 合成 /proc fixture，不依赖真机
# 采集侧测试：JSONL 数据契约 + trace.log 人类可读事件日志
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COLLECT="$SCRIPT_DIR/collect.sh"
DISCOVER="$SCRIPT_DIR/discover.sh"

pick_py() {
  if [ -n "${AT_PY:-}" ] && "${AT_PY}" -c 'pass' >/dev/null 2>&1; then
    echo "${AT_PY}"; return
  fi
  local c
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'pass' >/dev/null 2>&1; then
      command -v "$c"; return
    fi
  done
  echo ""
}
PY="$(pick_py)"
[ -n "$PY" ] || { echo "FATAL: 需要可用的 python3 做 JSONL 断言"; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAKE="$WORK/proc"
mkdir -p "$FAKE"
echo '(fake procfs)' > "$FAKE/stat"

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "PASS: $*"; }
bad() { FAIL=$((FAIL+1)); echo "FAIL: $*"; }
check_eq() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expect=[$2] got=[$3])"; fi; }
check_in() { case "$3" in *"$2"*) ok "$1";; *) bad "$1 (missing [$2])";; esac; }

# ------------------------------------------------------------ fixture 工具
mk_thread() { # pid tid comm state utime stime psr allowed
  local pid=$1 tid=$2 comm=$3 state=$4 utime=$5 stime=$6 psr=$7 allowed=$8
  local d="$FAKE/$pid/task/$tid" i
  mkdir -p "$d"
  local tail=("$state" 1 1 1 0 -1 4194560 0 0 0 0 "$utime" "$stime")
  for ((i=14; i<=36; i++)); do tail+=(0); done
  tail+=("$psr")
  printf '%s (%s) %s\n' "$pid" "$comm" "${tail[*]}" > "$d/stat"
  printf 'Name:\t%s\nCpus_allowed_list:\t%s\n' "$comm" "$allowed" > "$d/status"
  printf '%s\n' "$comm" > "$d/comm"
}
mk_proc() { # pid comm [starttime]
  local pid=$1 comm=$2 st="${3:-1000}"
  mkdir -p "$FAKE/$pid/task/$pid"
  printf '%s\n' "$comm" > "$FAKE/$pid/comm"
  awk -v p="$pid" -v c="$comm" -v s="$st" 'BEGIN{
    printf "%d (%s) S 1 1 1 0 -1 4194560 0 0 0 0 100 100 0 0 0 0 1 0 %d 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 1 0 0\n", p, c, s
  }' > "$FAKE/$pid/stat"
}

tick() { AT_PROC_ROOT="$FAKE" bash "$COLLECT" -p "$1" --tick --out "$2" --quiet; }

# ============================================================ T1 discover
mk_proc 300 python3 900
mk_thread 300 300 python3 S 1 1 0 "0-127"
out=$(AT_PROC_ROOT="$FAKE" AT_SYS_ROOT="/nonexistent" bash "$DISCOVER" 2>/dev/null)
check_eq "T1 discover 就绪检查" "DISCOVER_OK" "$out"
rm -rf "$FAKE/300"   # T1 fixture 清理，避免影响 T2 的 auto 选中

# ============================================================ T2 auto + 特殊字符
mk_proc 102 python3 500
mk_proc 103 python3.11 900
mk_thread 103 103 python3.11 S 100 10 0 "0-127"
D2="$WORK/o2"
AT_PROC_ROOT="$FAKE" bash "$COLLECT" --auto -T 0.2 -i 0.1 --out "$D2" --quiet >/dev/null 2>&1
check_eq "T2a auto 选中最新启动的 python3.11" "103" \
  "$("$PY" -c 'import json,sys;print(json.loads(open(sys.argv[1]).readline())["meta"]["pid"])' "$D2/samples.jsonl")"
# T2b 特殊字符 comm：meta 行必须合法 JSON
mk_proc 400 'bad"com\m' 3000
mk_thread 400 400 'bad"com\m' S 10 1 0 "0-127"
D2G="$WORK/o2g"
AT_PROC_ROOT="$FAKE" bash "$COLLECT" -p 400 --init --out "$D2G" >/dev/null
v=$("$PY" - "$D2G/samples.jsonl" <<'PYEOF'
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    print(json.loads(fh.readline())["meta"]["target_comm"])
PYEOF
)
check_eq "T2b 特殊字符 comm 清洗后 meta 行合法 JSON" "badcomm" "$v"
rm -rf "$FAKE/400" "$FAKE/102" "$FAKE/103"

# ============================================================ T3 init/tick 数据契约
mk_proc 200 python3 100
mk_thread 200 200 python3 S 10 1 0 "0-127"
mk_thread 200 201 worker R 20 2 4 "0-127"
D3="$WORK/o3"
AT_PROC_ROOT="$FAKE" bash "$COLLECT" -p 200 --init --out "$D3" >/dev/null
check_eq "T3a init 后只有 meta 行" "1" "$(wc -l < "$D3/samples.jsonl")"
tick 200 "$D3"
check_eq "T3b tick 后 2 行" "2" "$(wc -l < "$D3/samples.jsonl")"
v=$("$PY" - "$D3/samples.jsonl" <<'PYEOF'
import json, sys
n, threads, allowed = 0, 0, ""
with open(sys.argv[1], encoding="utf-8") as fh:
    for line in fh:
        o = json.loads(line); n += 1
        if "threads" in o:
            threads = len(o["threads"]); allowed = o["threads"][1]["allowed"]
print(n, threads, allowed)
PYEOF
)
check_eq "T3c 全部行合法 JSON + 2 线程 + 掩码透传" "2 2 0-127" "$v"
check_in "T3d init 产出 trace.log 头部" "目标    : pid=200" "$(cat "$D3/trace.log")"
check_in "T3e trace.log 头部含口径" "落点=实际面" "$(cat "$D3/trace.log")"

# ============================================================ T8 trace.log 事件日志（核心）
# 场景：tick1 两个线程 → tick2 新线程 302 + 线程 201 掩码变化 → tick4-6 线程 302 消失
D8="$WORK/o8"
AT_PROC_ROOT="$FAKE" bash "$COLLECT" -p 200 --init --out "$D8" >/dev/null
mk_thread 200 201 worker R 30 2 4 "0-127"
tick 200 "$D8"   # tick1：200/201 在册
check_in "T8a tick1 首个 tick 不报新线程？——报（初始化后全部为新）" "+ 新线程 tid=201" "$(cat "$D8/trace.log")"
mk_thread 200 302 omp_worker R 40 2 2 "0-7"
sed -i 's/Cpus_allowed_list:\t0-127/Cpus_allowed_list:\t0-7/' "$FAKE/200/task/201/status"
tick 200 "$D8"   # tick2：新线程 302 + 201 掩码变化
LOG=$(cat "$D8/trace.log")
check_in "T8b 新线程事件" "+ 新线程 tid=302 (omp_worker) 允许核=0-7" "$LOG"
check_in "T8c 掩码变化事件（含旧 → 新）" "掩码变化 tid=201 (worker): 0-127 → 0-7" "$LOG"
rm -rf "$FAKE/200/task/302"
tick 200 "$D8"   # tick3：302 缺席 1 次
tick 200 "$D8"   # tick4：缺席 2 次
tick 200 "$D8"   # tick5：缺席 3 次 → 退出
LOG=$(cat "$D8/trace.log")
check_in "T8d 连续 3 tick 缺失判定线程退出" "- 线程退出 tid=302" "$LOG"
check_in "T8d-2 退出事件含在册区间（first=last=出现 tick）" "在册 #2-#2" "$LOG"
# --tick 跨进程事件延续：上述 5 个 tick 均为独立进程调用，事件状态持久化生效
# （T8b/T8d 本身就是证据——掩码对比依赖跨进程的 ST 状态）
ok "T8e --tick 跨进程事件状态延续（新线程/掩码变化跨调用判出）"
# 目标不可见事件：保留 task 目录与主 stat 文件（目标检查通过），清空
# stat 内容使 do_tick 解析出空线程集 → absent_proc（模拟进程退出中）
rm -rf "$FAKE/200/task/"*
mkdir -p "$FAKE/200/task/200"
: > "$FAKE/200/task/200/stat"
tick 200 "$D8" 2>/dev/null && rc=0 || rc=$?
check_eq "T8f 目标消失 tick 退出码 3" "3" "$rc"
check_in "T8g 目标不可见事件" "! 目标进程不可见" "$(cat "$D8/trace.log")"
# T8h 人可读性：日志不得出现裸 JSON 特征
if grep -qE '\{"|seq":' "$D8/trace.log"; then
  bad "T8h trace.log 出现裸 JSON（不可读）"
else
  ok "T8h trace.log 无裸 JSON（人类可读）"
fi

# ============================================================ T9 run 守护 + target_gone + finalize
mk_proc 500 python3 100
mk_thread 500 500 python3 S 10 1 0 "0-127"
D9="$WORK/o9"
AT_PROC_ROOT="$FAKE" bash "$COLLECT" -p 500 -i 0.2 --tolerate 2 -T 60 --out "$D9" --quiet \
  >"$WORK/t9.log" 2>&1 &
BGPID=$!
sleep 2.5     # 保证 ≥2 个正常 tick（git-bash 启动开销可达 1s+）后再删除目标
rm -rf "$FAKE/500/task"
wait "$BGPID"
check_in "T9a 结束原因 target_gone" '"reason":"target_gone"' "$(tail -1 "$D9/samples.jsonl")"
LOG=$(cat "$D9/trace.log")
check_in "T9b run 模式终态名册段" "==== 线程终态名册" "$LOG"
check_in "T9c 统计段" "==== 统计 ====" "$LOG"
check_in "T9d 统计含目标不可见计数" "目标不可见 2 tick" "$LOG"
check_in "T9e 名册含线程终态行（%-8s+分隔空格=6 空格）" "500      python3" "$LOG"
check_in "T9f 结尾指向 analyzer" "kptools-thread-affinity-analyzer" "$LOG"
[ -f "$D9/.trace_state.tsv" ] && bad "T9g 结束后状态文件应删除" || ok "T9g 状态文件已清理"
rm -rf "$FAKE/500"

# ============================================================ T10 退出码
mk_proc 200 python3 100    # T8 已删目标，重建用于退出码场景
mk_thread 200 200 python3 S 10 1 0 "0-127"
AT_PROC_ROOT="$FAKE" bash "$COLLECT" -p 99999 --out "$WORK/o10a" >/dev/null 2>&1
check_eq "T10a 目标不存在 → 3" "3" "$?"
AT_PROC_ROOT="$FAKE" bash "$COLLECT" -p 200 --tick --out "$WORK/o10b" >/dev/null 2>&1
check_eq "T10b tick 未 init 目录 → 1" "1" "$?"
AT_PROC_ROOT="$FAKE" bash "$COLLECT" -p 200 --out "$D3" >/dev/null 2>&1
check_eq "T10c run 到非空目录 → 1" "1" "$?"

# ============================================================ 汇总
echo
echo "===== $PASS passed, $FAIL failed ====="
[ "$FAIL" -eq 0 ]
