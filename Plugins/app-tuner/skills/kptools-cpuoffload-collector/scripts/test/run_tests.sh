#!/usr/bin/env bash
# kptools-cpuoffload-collector 回归测试 — 合成 /proc fixture
# 依赖：bash 4+、python3（>=3.6）。不使用 GNU 专属语法（BSD sed 兼容）。
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROBE="$SCRIPT_DIR/probe.py"
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
[ -n "$PY" ] || { echo "FATAL: 需要可用的 python3"; exit 1; }
echo "python: $PY"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAKE="$WORK/proc"
mkdir -p "$FAKE"
# fixture 页表按 4 KiB 构造；宿主机可能是 64 KiB 基页，必须显式注入
export AT_PAGE_SIZE=4096

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "PASS: $*"; }
bad() { FAIL=$((FAIL+1)); echo "FAIL: $*"; }
check_eq() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expect=[$2] got=[$3])"; fi; }
check_in() { case "$3" in *"$2"*) ok "$1";; *) bad "$1 (missing [$2])";; esac; }

jq_() { # jq_ <py-expr on d> <json file>
  "$PY" - "$2" "$1" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
print(eval(sys.argv[2], {"d": d}))
PYEOF
}

# ---------------------------------------------------------------- fixture
# mk_rank <pid> <rank> <ws> <start_kb> <nr_pages> <pfn_base> <present> [ppid]
mk_rank() {
  local pid=$1 rank=$2 ws=$3 start_kb=$4 nr_pages=$5 pfn_base=$6 present=$7
  local ppid=${8:-500}
  local d="$FAKE/$pid"
  mkdir -p "$d"
  if [ "$rank" != "-" ]; then
    printf 'RANK=%s\0LOCAL_RANK=%s\0WORLD_SIZE=%s\0' "$rank" "$rank" "$ws" > "$d/environ"
  else
    printf 'PATH=/usr/bin\0' > "$d/environ"
  fi
  printf 'python3\n' > "$d/comm"
  printf '%d (python3) S %d %d 1 1 1 0 1 0 1\n' "$pid" "$ppid" "$ppid" > "$d/stat"
  local end_kb=$(( start_kb + nr_pages * 4 ))
  printf '%x-%x rw-p 00000000 00:05 1 /dev/devmm_svm\n' \
    $(( start_kb * 1024 )) $(( end_kb * 1024 )) > "$d/maps"
  printf '%x-%x rw-p 00000000 00:06 2 /dev/shm/other\n' \
    $(( end_kb * 1024 + 4096 )) $(( end_kb * 1024 + 12288 )) >> "$d/maps"
  "$PY" - "$d/pagemap" "$nr_pages" "$pfn_base" "$present" "$start_kb" <<'PYEOF'
import sys, struct
path, nr, pfn_base, present, start_kb = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
start_page = start_kb // 4
total_pages = start_page + nr + 1
with open(path, "wb") as fh:
    for i in range(total_pages):
        if i >= start_page and i < start_page + nr:
            w = (1 << 63) | ((pfn_base + i - start_page) & ((1 << 55) - 1)) if present else 0
        else:
            w = 0
        fh.write(struct.pack("<Q", w))
PYEOF
}

# mk_ghost <pid> — 有目录无 maps（探测中消失 → partial）
mk_ghost() {
  local d="$FAKE/$1"
  mkdir -p "$d"
  printf 'PATH=/usr/bin\0' > "$d/environ"
}

# mk_nomatch <pid> — environ 无 RANK（--auto 跳过），maps 只有 /dev/shm（rc=4）
mk_nomatch() {
  local d="$FAKE/$1"
  mkdir -p "$d"
  printf 'PATH=/usr/bin\0' > "$d/environ"
  printf '%x-%x rw-p 00000000 00:06 2 /dev/shm/other\n' \
    $(( 1024 * 1024 )) $(( 1024 * 1024 + 4096 )) > "$d/maps"
  # 真实内核上 /proc/<pid>/pagemap 恒存在（进程存活时），fixture 需一致
  : > "$d/pagemap"
}

echo "(fake procfs)" > "$FAKE/stat"
# 真实内核 zoneinfo 格式：start_pfn 带冒号（Linux >= 3.10）
cat > "$FAKE/zoneinfo" <<'EOF'
Node 0, zone      DMA32
  per-node stats
    start_pfn:           0
Node 1, zone      Normal
  per-node stats
    start_pfn:           16384
EOF
mk_rank 1000 0 2 4096 2048 20000 1 500
mk_rank 1001 1 2 16384 1024 500 1 500
BUNDLE="$WORK/bundle"

# ============================================================ T1 discover
out=$(AT_PROC_ROOT="$FAKE" PATH="$(dirname "$PY"):$PATH" \
  bash "$DISCOVER" 2>/dev/null)
check_in "T1a discover 就绪检查" "DISCOVER_OK" "$out"
d_out=$(AT_PROC_ROOT="$FAKE" PATH="$(dirname "$PY"):$PATH" \
  bash "$DISCOVER" 2>&1)
check_in "T1b discover 输出基页大小" "page_bytes=" "$d_out"

# ============================================================ T2 auto 采集（单作业分组）
"$PY" "$PROBE" --auto --tag after_init --out "$BUNDLE" \
  --proc-root "$FAKE" --sample-ratio 64 >/dev/null 2>&1
v=$?
check_eq "T2a --auto 采集 rc=0" "0" "$v"
check_in "T2b probe.after_init.json 生成" "probe.after_init.json" "$(ls "$BUNDLE" 2>/dev/null)"
v=$(jq_ "sorted(e['rank'] for e in d['ranks'])" "$BUNDLE/probe.after_init.json")
check_eq "T2c 发现 2 rank" "[0, 1]" "$v"
v=$(jq_ "d['ranks'][0]['mode']" "$BUNDLE/probe.after_init.json")
check_eq "T2d 采样模式标签" "sampled_1_64" "$v"
v=$(jq_ "d['expected_ranks']" "$BUNDLE/probe.after_init.json")
check_eq "T2e expected_ranks 记录" "2" "$v"
check_in "T2f run_meta.json 生成" "run_meta.json" "$(ls "$BUNDLE")"
v=$(jq_ "d['world_size']" "$BUNDLE/run_meta.json")
check_eq "T2g run_meta world_size" "2" "$v"
v=$(jq_ "sorted(d['ranks'][0]['node_kb'].keys())" "$BUNDLE/probe.after_init.json")
check_eq "T2h 采样 node_kb 为 KB 口径键（无 _pct）" "['1']" "$v"

# ============================================================ T3 全量模式
BUNDLE3="$WORK/bundle3"
"$PY" "$PROBE" --pid 1000 --tag full_init --out "$BUNDLE3" \
  --proc-root "$FAKE" --full >/dev/null 2>&1
v=$?
check_eq "T3a --full rc=0" "0" "$v"
v=$(jq_ "(d['ranks'][0]['total_present_kb'], sorted(d['ranks'][0]['node_kb'].keys()), d['ranks'][0]['vmas'][0]['pathname'])" "$BUNDLE3/probe.full_init.json")
check_eq "T3b 全量 present=8192KB/node1" "(8192, ['1'], '/dev/devmm_svm')" "$v"

# ============================================================ T3c 采样主路径（默认 --sample-ratio）
BUNDLE3C="$WORK/bundle3c"
"$PY" "$PROBE" --pid 1000 --tag s64 --out "$BUNDLE3C" \
  --proc-root "$FAKE" >/dev/null 2>&1
v=$?
check_eq "T3c-a 默认采样 rc=0" "0" "$v"
v=$(jq_ "d['ranks'][0]['total_present_kb']" "$BUNDLE3C/probe.s64.json")
check_eq "T3c-b 采样外推 present=8192KB（均匀 present 精确）" "8192" "$v"
v=$(jq_ "d['ranks'][0]['node_kb'].get('1')" "$BUNDLE3C/probe.s64.json")
check_eq "T3c-c 采样外推 node1=8192KB" "8192" "$v"
v=$(jq_ "d['ranks'][0]['vmas'][0].get('sampled', {}).get('samples')" "$BUNDLE3C/probe.s64.json")
check_eq "T3c-d 采样证据 samples=32" "32" "$v"

# ============================================================ T4 trace.log 可读
check_in "T4a trace.log 头部" "CPU offload 内存采集日志" "$(cat "$BUNDLE/trace.log")"
check_in "T4b trace.log 含 tag" "after_init" "$(cat "$BUNDLE/trace.log")"
check_in "T4c trace.log 含 rank 行" "rank 0" "$(cat "$BUNDLE/trace.log")"
check_in "T4d trace.log 披露时间窗" "时间窗" "$(cat "$BUNDLE/trace.log")"
check_in "T4e trace.log node 分布 KB 口径" "node 分布(KB)" "$(cat "$BUNDLE/trace.log")"

# ============================================================ T5 step 序列追加
"$PY" "$PROBE" --auto --tag step_1 --out "$BUNDLE" \
  --proc-root "$FAKE" --sample-ratio 64 >/dev/null 2>&1
v=$?
check_eq "T5a step_1 追加 rc=0" "0" "$v"
check_in "T5b probe.step_1.json" "probe.step_1.json" "$(ls "$BUNDLE")"

# ============================================================ T6 排除非 devmm 段
v=$(jq_ "sum(1 for e in d['ranks'] for v in e['vmas'] if 'other' in v['pathname'])" "$BUNDLE/probe.after_init.json")
check_eq "T6 非 devmm 段排除" "0" "$v"

# ============================================================ T7 退出码
"$PY" "$PROBE" --pid 99999 --tag x --out "$WORK/x" \
  --proc-root "$FAKE" >/dev/null 2>&1
v=$?
check_eq "T7a --pid 不存在 → 3" "3" "$v"
"$PY" "$PROBE" --pid abc --tag x --out "$WORK/x" \
  --proc-root "$FAKE" >/dev/null 2>&1
v=$?
check_eq "T7b --pid 非数字 → 1" "1" "$v"
"$PY" "$PROBE" --proc-root "$FAKE" --tag x --out "$WORK/x" >/dev/null 2>&1
v=$?
check_eq "T7c 缺 --pid/--auto → 1" "1" "$v"

# ============================================================ T7d argparse 用法错误 → 1（不与 2 撞码）
"$PY" "$PROBE" --pid 1000 --out "$WORK/x" \
  --proc-root "$FAKE" >/dev/null 2>&1
v=$?
check_eq "T7d 缺 --tag → 1" "1" "$v"
"$PY" "$PROBE" --pid 1000 --tag x --no-such-flag --out "$WORK/x" \
  --proc-root "$FAKE" >/dev/null 2>&1
v=$?
check_eq "T7e 未知 flag → 1" "1" "$v"

# ============================================================ T8 敏感自检
leak=$(grep -hoE '[a-f0-9]{32,}' "$SCRIPT_DIR/../SKILL.md" 2>/dev/null | sort -u)
if [ -z "$leak" ]; then ok "T8 无 32+ hex 泄露"; else bad "T8 泄露: $leak"; fi

# ============================================================ T9 NO_PINNED_VMA → 4
mk_nomatch 2000
"$PY" "$PROBE" --pid 2000 --tag nopin --out "$WORK/b9" \
  --proc-root "$FAKE" >/dev/null 2>&1
v=$?
check_eq "T9 无 devmm 段 → 4（未启用 cpu offload）" "4" "$v"
if [ -e "$WORK/b9/probe.nopin.json" ]; then bad "T9b rc=4 不应落盘"; else ok "T9b rc=4 不落盘（tag 不被占用）"; fi

# ============================================================ T10 partial rank → 6
mk_ghost 2500
"$PY" "$PROBE" --pid 1000,2500 --tag partial --out "$WORK/b10" \
  --proc-root "$FAKE" >/dev/null 2>&1
v=$?
check_eq "T10a 部分 rank 失败 → 6" "6" "$v"
v=$(jq_ "(d['partial'], d['missing'][0]['pid'], d['missing'][0]['reason'])" "$WORK/b10/probe.partial.json")
check_eq "T10b missing 明细记录" "(True, 2500, 'process_gone')" "$v"
v=$(jq_ "len(d['ranks'])" "$WORK/b10/probe.partial.json")
check_eq "T10c 已采集 rank 仍落盘" "1" "$v"

# ============================================================ T11 同 tag 重跑
"$PY" "$PROBE" --auto --tag after_init --out "$BUNDLE" \
  --proc-root "$FAKE" >/dev/null 2>&1
v=$?
check_eq "T11a 同 tag 重跑 → 1 TAG_EXISTS" "1" "$v"
"$PY" "$PROBE" --auto --tag after_init --out "$BUNDLE" \
  --proc-root "$FAKE" --force >/dev/null 2>&1
v=$?
check_eq "T11b --force 覆盖 → 0" "0" "$v"

# ============================================================ T12 多作业 → 1
mk_rank 3000 0 2 $(( 64 * 1024 )) 128 30000 1 600
mk_rank 3001 1 2 $(( 80 * 1024 )) 128 30200 1 600
"$PY" "$PROBE" --auto --tag multi --out "$WORK/b12" \
  --proc-root "$FAKE" 2>"$WORK/b12.err" >/dev/null
v=$?
check_eq "T12a 双作业 --auto → 1 MULTI_JOB" "1" "$v"
check_in "T12b stderr 列出候选作业" "MULTI_JOB" "$(cat "$WORK/b12.err")"

# ============================================================ T13 run_meta 设备拓扑（vendor+class 双校验）
SYST="$WORK/sys"
mkdir -p "$SYST/bus/pci/devices/0000:8f:00.0" "$SYST/bus/pci/devices/0000:9f:00.0"
echo "0x19e5" > "$SYST/bus/pci/devices/0000:8f:00.0/vendor"
echo "0x12000001" > "$SYST/bus/pci/devices/0000:8f:00.0/class"
echo "1" > "$SYST/bus/pci/devices/0000:8f:00.0/numa_node"
echo "0x19e5" > "$SYST/bus/pci/devices/0000:9f:00.0/vendor"
echo "0x02000000" > "$SYST/bus/pci/devices/0000:9f:00.0/class"   # 网卡：vendor 同、class 异
echo "1" > "$SYST/bus/pci/devices/0000:9f:00.0/numa_node"
AT_SYS_ROOT="$SYST" "$PY" "$PROBE" --pid 1000 --tag top --out "$WORK/b13" \
  --proc-root "$FAKE" >/dev/null 2>&1
v=$?
check_eq "T13a AT_SYS_ROOT 采集 rc=0" "0" "$v"
v=$(jq_ "d['device_node_map']" "$WORK/b13/run_meta.json")
check_eq "T13b NPU 双校验（网卡排除）" "{'0000:8f:00.0': {'node': 1}}" "$v"

# ============================================================ T17 全 rank PermissionError → 5
# 契约对齐：跨 uid --pid 的 maps 不可读时退 5（无权限），非退 3（目标
# 不存在）。root 下 chmod 000 无法产生 EACCES（root 无视权限位）——
# 用例只在非 root 环境（多数 CI runner）真跑，root 下 skip 属诚实标注
if [ "$(id -u)" = "0" ]; then
  echo "SKIP: T17 root 下 chmod 000 不可产生 EACCES（非 root CI 环境覆盖此路径）"
else
  FPERM="$WORK/proc_perm"
  mkdir -p "$FPERM/2100"
  printf 'PATH=/usr/bin\0' > "$FPERM/2100/environ"
  printf '%x-%x rw-p 00000000 00:05 1 /dev/devmm_svm\n' 65536 131072 \
    > "$FPERM/2100/maps"
  chmod 000 "$FPERM/2100/maps"
  "$PY" "$PROBE" --pid 2100 --tag perm --out "$WORK/b17" \
    --proc-root "$FPERM" >/dev/null 2>&1
  check_eq "T17 全 rank maps 无权限 → 5（非 3）" "5" "$?"
fi

# ============================================================ 汇总
echo
echo "===== $PASS passed, $FAIL failed ====="
[ "$FAIL" -eq 0 ]
