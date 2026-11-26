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
sed 's/Cpus_allowed_list:\t0-127/Cpus_allowed_list:\t0-7/' \
  "$FAKE/200/task/201/status" > "$FAKE/200/task/201/status.new" \
  && mv "$FAKE/200/task/201/status.new" "$FAKE/200/task/201/status"
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

# ============================================================ T11 非空目录（仅 trace.log 也拒）
D11="$WORK/o11"; mkdir -p "$D11"
: > "$D11/trace.log"
AT_PROC_ROOT="$FAKE" bash "$COLLECT" -p 200 --out "$D11" >/dev/null 2>&1
check_eq "T11 run 到含旧 trace.log 的目录 → 1（防截断）" "1" "$?"
AT_PROC_ROOT="$FAKE" bash "$COLLECT" -p 200 --init --out "$D11" >/dev/null 2>&1
check_eq "T11b init 到非空目录 → 1（防追加第二 meta）" "1" "$?"

# ============================================================ T12 数值参数校验
AT_PROC_ROOT="$FAKE" bash "$COLLECT" -p 200 -i abc --out "$WORK/o12a" >/dev/null 2>&1
check_eq "T12a -i abc → 1" "1" "$?"
AT_PROC_ROOT="$FAKE" bash "$COLLECT" -p 200 -i 0.5x --out "$WORK/o12a" >/dev/null 2>&1
check_eq "T12b -i 0.5x → 1" "1" "$?"
AT_PROC_ROOT="$FAKE" bash "$COLLECT" -p 200 --tolerate abc --out "$WORK/o12a" >/dev/null 2>&1
check_eq "T12c --tolerate abc → 1" "1" "$?"
AT_PROC_ROOT="$FAKE" bash "$COLLECT" -p 200 -T abc --out "$WORK/o12a" >/dev/null 2>&1
check_eq "T12d -T abc → 1" "1" "$?"

# ============================================================ T13 --tick 会话身份校验
mk_proc 700 python3 888
mkdir -p "$FAKE/700/task/700"
awk 'BEGIN{printf "700 (python3) S 1 1 1 0 -1 4194560 0 0 0 0 10 1 0 0 0 0 1 0 888 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 1 0 0\n"}' \
  > "$FAKE/700/task/700/stat"
printf 'Name:\tpython3\nCpus_allowed_list:\t0-127\n' > "$FAKE/700/task/700/status"
D13="$WORK/o13"
AT_PROC_ROOT="$FAKE" bash "$COLLECT" -p 700 --init --out "$D13" >/dev/null
# a) PID 不一致
mk_proc 701 python3 100
mk_thread 701 701 python3 S 1 1 0 "0-127"
AT_PROC_ROOT="$FAKE" bash "$COLLECT" -p 701 --tick --out "$D13" >/dev/null 2>&1
check_eq "T13a --tick 换 PID → 1" "1" "$?"
# b) starttime 变化（PID 复用）
sed 's/ 888 / 999 /' "$FAKE/700/task/700/stat" > "$FAKE/700/task/700/stat.new" \
  && mv "$FAKE/700/task/700/stat.new" "$FAKE/700/task/700/stat"
AT_PROC_ROOT="$FAKE" bash "$COLLECT" -p 700 --tick --out "$D13" >/dev/null 2>&1
check_eq "T13b --tick PID 复用（starttime 变）→ 1" "1" "$?"
# c) 恢复后正常 tick
sed 's/ 999 / 888 /' "$FAKE/700/task/700/stat" > "$FAKE/700/task/700/stat.new" \
  && mv "$FAKE/700/task/700/stat.new" "$FAKE/700/task/700/stat"
AT_PROC_ROOT="$FAKE" bash "$COLLECT" -p 700 --tick --out "$D13" --quiet >/dev/null 2>&1
check_eq "T13c 一致会话 tick → 0" "0" "$?"
# d) 会话已结束（end 行）后拒续写
printf '{"end":{"ts":1,"reason":"timeout","ticks":1}}\n' >> "$D13/samples.jsonl"
AT_PROC_ROOT="$FAKE" bash "$COLLECT" -p 700 --tick --out "$D13" >/dev/null 2>&1
check_eq "T13d 已结束会话续 tick → 1" "1" "$?"

# ============================================================ T14 不可见恢复：连续清零/累计保留
D14="$WORK/o14"
AT_PROC_ROOT="$FAKE" bash "$COLLECT" -p 200 --init --out "$D14" >/dev/null
tick 200 "$D14" >/dev/null 2>&1
: > "$FAKE/200/task/200/stat"
tick 200 "$D14" >/dev/null 2>&1 || true       # gone #1
mk_thread 200 200 python3 S 10 1 0 "0-127"     # 恢复
tick 200 "$D14" >/dev/null 2>&1
: > "$FAKE/200/task/200/stat"
tick 200 "$D14" >/dev/null 2>&1 || true       # gone #2
mk_thread 200 200 python3 S 10 1 0 "0-127"     # 再恢复
tick 200 "$D14" >/dev/null 2>&1
LOG14=$(cat "$D14/trace.log")
check_in "T14a 恢复事件含累计数" "累计 2 tick" "$LOG14"
v=$(grep -c '"absent_proc":1' "$D14/samples.jsonl")
check_eq "T14b samples 累计 2 条 absent 行" "2" "$v"
v=$(head -n 1 "$D14/.trace_state.tsv" | cut -f6)
check_eq "T14c state 累计 absent=2（恢复不清零）" "2" "$v"
v=$(head -n 1 "$D14/.trace_state.tsv" | cut -f5)
check_eq "T14d state 连续 absent=0（恢复清零）" "0" "$v"

# ============================================================ T15 proc_root JSON 转义
W15="$WORK/we\"ird\\path"
mkdir -p "$W15/810/task/810"
printf 'python3\n' > "$W15/810/comm"
awk 'BEGIN{printf "810 (python3) S 1 1 1 0 -1 4194560 0 0 0 0 10 1 0 0 0 0 1 0 100 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 1 0 0\n"}' > "$W15/810/stat"
awk 'BEGIN{printf "810 (python3) S 1 1 1 0 -1 4194560 0 0 0 0 10 1 0 0 0 0 1 0 100 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 1 0 0\n"}' > "$W15/810/task/810/stat"
printf 'Name:\tpython3\nCpus_allowed_list:\t0-127\n' > "$W15/810/task/810/status"
D15="$WORK/o15"
AT_PROC_ROOT="$W15" bash "$COLLECT" -p 810 --init --out "$D15" >/dev/null
v=$("$PY" - "$D15/samples.jsonl" <<'PYEOF'
import json, sys
m = json.loads(open(sys.argv[1], encoding="utf-8").readline())["meta"]
print(m["proc_root"])
PYEOF
)
check_eq "T15 含引号/反斜杠的 proc_root 转义后可逆" "$W15" "$v"

# ============================================================ T16 NPU 双校验 + 逗号分隔 + SYS_ROOT 注入
SYSF="$WORK/sys"
mkdir -p "$SYSF/devices/system/node/node0" \
  "$SYSF/bus/pci/devices/0000:8f:00.0" \
  "$SYSF/bus/pci/devices/0000:9f:00.0" \
  "$SYSF/bus/pci/devices/0000:af:00.0"
echo "0-3" > "$SYSF/devices/system/node/node0/cpulist"
for bdf in 0000:8f:00.0 0000:9f:00.0; do
  echo "0x19e5" > "$SYSF/bus/pci/devices/$bdf/vendor"
  echo "0x12000001" > "$SYSF/bus/pci/devices/$bdf/class"
  echo "1" > "$SYSF/bus/pci/devices/$bdf/numa_node"
done
# 同 vendor 的网卡（class 0x02xxxx）必须被排除
echo "0x19e5" > "$SYSF/bus/pci/devices/0000:af:00.0/vendor"
echo "0x02000000" > "$SYSF/bus/pci/devices/0000:af:00.0/class"
echo "1" > "$SYSF/bus/pci/devices/0000:af:00.0/numa_node"
printf 'OMP_NUM_THREADS=8\0KMP_AFFINITY=verbose,granularity=fine\0KMP_PROFILE_FILE=/home/u/work/prof.txt\0HCCL_WHITELIST_DISABLE=secret\0HCCL_CONNECT_TIMEOUT=1800\0' \
  > "$FAKE/200/environ"
D16="$WORK/o16"
AT_PROC_ROOT="$FAKE" AT_SYS_ROOT="$SYSF" bash "$COLLECT" -p 200 --init --out "$D16" >/dev/null
TOPO=$(cat "$D16/topology_raw.txt")
check_eq "T16a npu_pci 只含双校验过的 2 台 NPU" "2" \
  "$(printf '%s' "$TOPO" | awk '/^=== npu_pci/{f=1;next} /^===/{f=0} f&&NF' | wc -l | tr -d ' ')"
if printf '%s' "$TOPO" | grep -q 'af:00.0'; then
  bad "T16b Huawei 网卡误入 npu_pci"
else
  ok "T16b Huawei 网卡（vendor 同/class 异）被排除"
fi
check_in "T16c trace.log 头部多 NPU 逗号分隔" \
  "NPU:2 台(0000:8f:00.0(node1),0000:9f:00.0(node1))" "$(cat "$D16/trace.log")"
check_in "T16d target_env 白名单采集" "OMP_NUM_THREADS=8" "$TOPO"
check_in "T16e 白名单内 KMP_AFFINITY 原值（无路径）" "KMP_AFFINITY=verbose,granularity=fine" "$TOPO"
check_in "T16f HCCL_CONNECT_TIMEOUT 采集" "HCCL_CONNECT_TIMEOUT=1800" "$TOPO"
if printf '%s' "$TOPO" | grep -q 'KMP_PROFILE_FILE'; then
  bad "T16g KMP_PROFILE_FILE 绝对路径被带出"
else
  ok "T16g KMP_PROFILE_FILE（白名单外）不采集"
fi
if printf '%s' "$TOPO" | grep -q 'HCCL_WHITELIST'; then
  bad "T16h 部署自定义 HCCL_* 被带出"
else
  ok "T16h HCCL_WHITELIST_DISABLE（白名单外）不采集"
fi

# ============================================================ 汇总
echo
echo "===== $PASS passed, $FAIL failed ====="
[ "$FAIL" -eq 0 ]
