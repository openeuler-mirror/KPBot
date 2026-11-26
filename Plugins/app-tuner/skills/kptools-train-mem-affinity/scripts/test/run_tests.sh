#!/usr/bin/env bash
# kptools-train-mem-affinity 回归测试 — 合成 /proc + /sys fixture，不依赖真机
# 依赖：bash 4+、python3（>=3.6）；任意环境可跑
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SK="$(cd "$SCRIPT_DIR/.." && pwd)"
COLLECT="$SK/scripts/collect_rank_mem.py"
DISCOVER="$SK/scripts/discover.sh"

# 选一个真实可用的 python（跳过 Windows 商店占位符等假解释器）
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
[ -n "$PY" ] || { echo "FATAL: 没有可用的 python3"; exit 1; }
echo "python: $PY"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FP="$WORK/proc"      # proc_root
FS="$WORK/sys"       # sys_root

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "PASS: $*"; }
bad() { FAIL=$((FAIL+1)); echo "FAIL: $*"; }
check_eq() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expect=[$2] got=[$3])"; fi; }
check_in() { case "$3" in *"$2"*) ok "$1";; *) bad "$1 (missing [$2])";; esac; }

# ------------------------------------------------------------ fixture 工具
# numa_maps 数字设计（rank0 预期聚合）：
#   heap    anon=100 N0=100        → n0: total 400, anon 400
#   权重文件 N0=50 N1=50 (anon=0)  → n0 file 200, n1 file 200
#   shm大页  N1=64 kps=2048        → n1 file 131072
#   stack   anon=6 N0=4 N1=2       → n0 anon 16, n1 anon 8 (按页比例分摊)
#   ⇒ n0{total 616, anon 416, file 200} n1{total 131280, anon 8, file 131272}
#     resident 131896, anon_primary=node0 (98.1%)
RANK0_MAPS='55d0f2e00000 default heap anon=100 N0=100 kernelpagesize_kB=4
7f3a2b4f9000 default file=/data/model.safetensors N0=50 N1=50 kernelpagesize_kB=4
7f3a30000000 bind:0 file=/dev/shm/hccl_1000 mapped=64 N1=64 kernelpagesize_kB=2048
7fffce7e5000 default stack anon=6 N0=4 N1=2 kernelpagesize_kB=4'

RANK1_MAPS='55d1a2b00000 default heap anon=200 N0=200 kernelpagesize_kB=4
7ffd1c3f5000 default stack anon=6 N0=6 kernelpagesize_kB=4'

mk_proc() { # pid environ_content numa_maps rss mems cpus [ppid]
  local pid=$1 env=$2 maps=$3 rss=$4 mems=$5 cpus=$6 ppid=${7:-999}
  local d="$FP/$1"
  mkdir -p "$d"
  printf '%b' "$env" > "$d/environ"
  printf '%s\n' "$maps" > "$d/numa_maps"
  # stat 含 ppid：rank 发现按作业（进程树根）分组，同 ppid=同一 launcher
  printf '%s (python3) S %s %s 1 1 1 0 -1 4194560 0 0 0 0 10 1 0 0 0 0 1 0 1000 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 1 0 0\n' \
    "$pid" "$ppid" "$ppid" > "$d/stat"
  {
    printf 'Name:\tpython3\n'
    printf 'VmRSS:\t%s kB\n' "$rss"
    printf 'Mems_allowed:\t0000000f\n'
    printf 'Mems_allowed_list:\t%s\n' "$mems"
    printf 'Cpus_allowed_list:\t%s\n' "$cpus"
  } > "$d/status"
  printf 'python3\n' > "$d/comm"
}

mk_env() {
  rm -rf "$FP" "$FS"
  mkdir -p "$FP/sys/kernel" "$FS/devices/system/node/node0" \
           "$FS/devices/system/node/node1" \
           "$FS/kernel/mm/transparent_hugepage" \
           "$FS/bus/pci/devices/0000:0a:00.0" \
           "$FS/bus/pci/devices/0000:0b:00.0"
  echo 1 > "$FP/sys/kernel/numa_balancing"
  echo 0-31 > "$FS/devices/system/node/node0/cpulist"
  printf 'Node 0 MemTotal:  16777216 kB\nNode 0 MemFree:  8388608 kB\n' \
    > "$FS/devices/system/node/node0/meminfo"
  printf 'numa_hit 99000\nnuma_miss 1000\nlocal_node 98000\nother_node 1000\n' \
    > "$FS/devices/system/node/node0/numastat"
  echo 32-63 > "$FS/devices/system/node/node1/cpulist"
  printf 'Node 1 MemTotal:  16777216 kB\nNode 1 MemFree:  15000000 kB\n' \
    > "$FS/devices/system/node/node1/meminfo"
  printf 'numa_hit 95000\nnuma_miss 5000\nlocal_node 90000\nother_node 5000\n' \
    > "$FS/devices/system/node/node1/numastat"
  printf '[always] madvise never\n' \
    > "$FS/kernel/mm/transparent_hugepage/enabled"
  # 真 NPU：vendor + class 0x120000 双校验
  echo 0x19e5 > "$FS/bus/pci/devices/0000:0a:00.0/vendor"
  echo 0x120000 > "$FS/bus/pci/devices/0000:0a:00.0/class"
  echo 0xd801 > "$FS/bus/pci/devices/0000:0a:00.0/device"
  echo 1 > "$FS/bus/pci/devices/0000:0a:00.0/numa_node"
  # 华为 Hi1822 网卡：vendor 同为 0x19e5 但 class 是网络控制器——必须被排除
  echo 0x19e5 > "$FS/bus/pci/devices/0000:0b:00.0/vendor"
  echo 0x020000 > "$FS/bus/pci/devices/0000:0b:00.0/class"
  echo 1 > "$FS/bus/pci/devices/0000:0b:00.0/numa_node"

  # rank0: mems 0-1 / cpus 0-31(node0) → anon 主 n0 与 CPU 同域（C1 负例）
  mk_proc 1000 'RANK=0\0LOCAL_RANK=0\0WORLD_SIZE=2\0' "$RANK0_MAPS" \
    131896 "0-1" "0-31"
  # rank1: mems 0 / cpus 32-63(node1) → C3 强制分离 + C1 + C2
  mk_proc 1001 'RANK=1\0LOCAL_RANK=1\0WORLD_SIZE=2\0' "$RANK1_MAPS" \
    824 "0" "32-63"
  # 非 rank 进程
  mk_proc 2000 'HOME=/root\0TERM=xterm\0' "" 0 "0-1" "0-63"
}

collect() { # outdir extra...
  local out=$1; shift
  "$PY" "$COLLECT" --proc-root "$FP" --sys-root "$FS" --out "$out" "$@" 2>"$out.stderr"
}

jqpy() { # expr → python 取值
  "$PY" - "$1" <<PYEOF
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
rs = {r["rank"]: r for r in d["ranks"]}
print($2)
PYEOF
}

# ============================================================ T1 discover
mk_env
out=$(AT_PROC_ROOT="$FP" AT_SYS_ROOT="$FS" PATH="$(dirname "$PY"):$PATH" \
  bash "$DISCOVER" 2>"$WORK/t1.stderr")
check_eq "T1a discover 就绪检查（stdout 只有 DISCOVER_OK）" "DISCOVER_OK" "$out"
check_in "T1b discover 透出 numa_balancing" "numa_balancing=1" \
  "$(cat "$WORK/t1.stderr" 2>/dev/null)"

# ============================================================ T2 rank 发现
collect "$WORK/o2" >/dev/null
v=$(jqpy "$WORK/o2/train_mem_affinity.json" \
  'len(d["ranks"]), sorted(rs), sorted(r["pid"] for r in d["ranks"])')
check_eq "T2a 发现 2 ranks（0/1），非 rank 进程 2000 不入" \
  "2 [0, 1] [1000, 1001]" "$v"
v=$(jqpy "$WORK/o2/train_mem_affinity.json" \
  'rs[0]["rank_env"], rs[0]["local_rank"], rs[0]["world_size"]')
check_eq "T2b rank 元数据（RANK/LOCAL_RANK/WORLD_SIZE）" "RANK 0 2" "$v"

# ============================================================ T3 聚合数值精确
v=$(jqpy "$WORK/o2/train_mem_affinity.json" \
  '(rs[0]["nodes"]["0"]["total_kb"], rs[0]["nodes"]["0"]["anon_kb"], rs[0]["nodes"]["0"]["file_kb"], rs[0]["nodes"]["1"]["total_kb"], rs[0]["nodes"]["1"]["anon_kb"], rs[0]["nodes"]["1"]["file_kb"])')
check_eq "T3a rank0 节点聚合（4K页/大页/分摊）" \
  "(616, 416, 200, 131280, 8, 131272)" "$v"
v=$(jqpy "$WORK/o2/train_mem_affinity.json" \
  'rs[0]["resident_kb"], rs[0]["coverage"], rs[0]["anon_primary"]["node"], rs[0]["anon_primary"]["pct"]')
check_eq "T3b resident/coverage/anon 主node" "131896 1.0 0 0.9811" "$v"
v=$(jqpy "$WORK/o2/train_mem_affinity.json" 'sorted(rs[1]["nodes"]["0"].items())')
check_in "T3c rank1 聚合" "('anon_kb', 824)" "$v"

# ============================================================ T4 大页与 VMA 归因
collect "$WORK/o4" --top-vmas 3 >/dev/null
v=$(jqpy "$WORK/o4/train_mem_affinity.json" \
  'rs[0]["top_vmas"][0]["label"], rs[0]["top_vmas"][0]["total_kb"]')
check_eq "T4 大页 VMA 排第一（64页×2048KB=131072）" \
  "/dev/shm/hccl_1000 131072" "$v"
v=$(jqpy "$WORK/o4/train_mem_affinity.json" \
  '[x["label"] for x in rs[0]["top_vmas"]]')
check_in "T4b VMA label 归因（heap/文件路径）" "model.safetensors" "$v"

# ============================================================ T5 hints
v=$(jqpy "$WORK/o2/train_mem_affinity.json" 'sorted(rs[0]["hints"])')
check_eq "T5a rank0 hints（C2 错位 + C4 热点；C1/C3 负例不触发）" \
  "['C2_NPU_MISALIGN', 'C4_NODE_HOTSPOT']" "$v"
v=$(jqpy "$WORK/o2/train_mem_affinity.json" 'sorted(rs[1]["hints"])')
check_eq "T5b rank1 hints（C1/C2/C3/C4 全中）" \
  "['C1_REMOTE_SUSPECT', 'C2_NPU_MISALIGN', 'C3_FORCE_REMOTE', 'C4_NODE_HOTSPOT']" "$v"

# ============================================================ T6 系统侧
v=$(jqpy "$WORK/o2/train_mem_affinity.json" \
  'd["system"]["nodes"][0]["mem_free_kb"], d["system"]["numa_balancing"], d["system"]["thp"], d["system"]["npu_nodes"]')
check_eq "T6 node 水位/numa_balancing/THP/NPU 归属" \
  "8388608 1 always [1]" "$v"

# ============================================================ T7 NPU numa_node=-1 → C2 跳过
echo -1 > "$FS/bus/pci/devices/0000:0a:00.0/numa_node"
collect "$WORK/o7" >/dev/null
v=$(jqpy "$WORK/o7/train_mem_affinity.json" 'sorted(rs[0]["hints"])')
check_eq "T7 NPU 节点未知(-1)时 C2 跳过" "['C4_NODE_HOTSPOT']" "$v"
echo 1 > "$FS/bus/pci/devices/0000:0a:00.0/numa_node"

# ============================================================ T8/T9 覆盖率门
sed 's/VmRSS:.*/VmRSS:\t200000 kB/' "$FP/1000/status" > "$FP/1000/status.new" && mv "$FP/1000/status.new" "$FP/1000/status"
collect "$WORK/o8" >/dev/null
v=$(jqpy "$WORK/o8/train_mem_affinity.json" 'rs[0]["coverage"], sorted(rs[0]["hints"])')
check_eq "T8 coverage<0.9 → C6_LOW_COVERAGE" \
  "0.6595 ['C2_NPU_MISALIGN', 'C4_NODE_HOTSPOT', 'C6_LOW_COVERAGE']" "$v"
sed 's/VmRSS:.*/VmRSS:\t100 kB/' "$FP/1000/status" > "$FP/1000/status.new" && mv "$FP/1000/status.new" "$FP/1000/status"
collect "$WORK/o9" >/dev/null
v=$(jqpy "$WORK/o9/train_mem_affinity.json" \
  '"C6_OVERCOUNT" in rs[0]["hints"]')
check_eq "T9 coverage>1.05 → C6_OVERCOUNT" "True" "$v"
sed 's/VmRSS:.*/VmRSS:\t131896 kB/' "$FP/1000/status" > "$FP/1000/status.new" && mv "$FP/1000/status.new" "$FP/1000/status"

# ============================================================ T10 --diff 增量
# heap N0 100→356（+256 页 = +1024KB），其余不动
sed 's/anon=100 N0=100/anon=356 N0=356/' "$FP/1000/numa_maps" > "$FP/1000/numa_maps.new" && mv "$FP/1000/numa_maps.new" "$FP/1000/numa_maps"
collect "$WORK/o10" --diff "$WORK/o2/train_mem_affinity.json" >/dev/null
v=$(jqpy "$WORK/o10/train_mem_affinity.json" \
  '[x for x in d["diff"] if x["rank"]==0][0]["delta_total_kb"]')
check_eq "T10a diff：rank0 Δtotal=1024KB" "1024" "$v"
v=$(jqpy "$WORK/o10/train_mem_affinity.json" \
  '[x for x in d["diff"] if x["rank"]==0][0]["per_node"]["0"]["anon_kb"]')
check_eq "T10b diff：per-node Δanon" "1024" "$v"
v=$("$PY" "$COLLECT" --proc-root "$FP" --sys-root "$FS" --out "$WORK/o10b" \
     --diff "$WORK/o2/train_mem_affinity.json" 2>/dev/null | grep -c '增量对比')
check_eq "T10c markdown 含增量表" "1" "$v"

# ============================================================ T11 --affinity-dir 联动
AD="$WORK/afdir"; mkdir -p "$AD"
cat > "$AD/topology_raw.txt" <<'EOF'
=== host
Linux test 5.10.0
=== clk_tck
100
=== node_cpulist
0|0-31
1|32-63
=== l3_domains
=== npu_pci
0000:0a:00.0|1
=== npu_smi_info_l
(absent)
=== target_view
cpuset=/
EOF
"$PY" - "$AD/summary.json" <<'PYEOF'
import json, sys
sm = {
  "skill": "kptools-thread-affinity-collector", "version": "1.0.0",
  "target": {"pid": 1000, "comm": "python3"},
  "profiles": [
    {"tid": 1001, "comm": "python3", "role": "__main__", "busy_pct": 95.0,
     "active_psr_top": [[40, 48]], "nodes_touched": [1]},
    {"tid": 1002, "comm": "worker", "role": None, "busy_pct": 10.0,
     "active_psr_top": [[5, 3]], "nodes_touched": [0]},
  ],
}
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump(sm, fh, ensure_ascii=False)
PYEOF
collect "$WORK/o11" --affinity-dir "$AD" >/dev/null
v=$(jqpy "$WORK/o11/train_mem_affinity.json" \
  'rs[0].get("thread_join",{}).get("thread_primary_node"), d["affinity_join"]["pid"], d["affinity_join"]["matched"]')
check_eq "T11a pid=1000 匹配 → 线程主落点 node1" "1 1000 True" "$v"
v=$("$PY" "$COLLECT" --proc-root "$FP" --sys-root "$FS" --out "$WORK/o11b" \
     --affinity-dir "$AD" 2>/dev/null | grep -c 'C1✗')
check_eq "T11b 联合表 C1 判定（内存n0/线程n1）" "1" "$v"

# ============================================================ T12 mpirun 识别
mk_proc 3000 'OMPI_COMM_WORLD_RANK=7\0OMPI_COMM_WORLD_SIZE=8\0' \
  "$RANK1_MAPS" 824 "0-1" "0-31"
collect "$WORK/o12" >/dev/null
v=$(jqpy "$WORK/o12/train_mem_affinity.json" \
  '[r for r in d["ranks"] if r["pid"]==3000][0]["rank"]')
check_eq "T12 OMPI_COMM_WORLD_RANK 识别为 rank 7" "7" "$v"
rm -rf "$FP/3000"

# ============================================================ T13 退出码
collect "$WORK/o13a" >/dev/null 2>&1
check_eq "T13a 正常退出码 0" "0" "$?"
"$PY" "$COLLECT" --proc-root "$FP" --sys-root "$FS" --pid 99999 \
  --out "$WORK/o13b" >/dev/null 2>&1
check_eq "T13b --pid 不存在 → 3" "3" "$?"
# T13d 参数错误 → 1（argparse error 覆盖；不能与 2=/proc 不可读撞码）
"$PY" "$COLLECT" --proc-root "$FP" --sys-root "$FS" --top-vmas abc \
  --out "$WORK/o13d" >/dev/null 2>&1
check_eq "T13d 参数错误（--top-vmas abc）→ 1" "1" "$?"
"$PY" "$COLLECT" --proc-root /nonexistent_dir_xyz --out "$WORK/o13e" \
  >/dev/null 2>&1
check_eq "T13e --proc-root 不存在 → 2" "2" "$?"
rm -rf "$FP/1000" "$FP/1001"
collect "$WORK/o13c" >/dev/null 2>&1
check_eq "T13c 无 rank 进程 → 3" "3" "$?"

# ============================================================ T14 markdown sanity
mk_env
v=$("$PY" "$COLLECT" --proc-root "$FP" --sys-root "$FS" --out "$WORK/o14" \
     --top-vmas 2 2>/dev/null)
check_in "T14a md 含分布表头" "rank 内存分布" "$v"
check_in "T14b md 含覆盖率行" "覆盖率" "$v"
check_in "T14c md 含系统侧水位" "系统侧 node 水位" "$v"
check_in "T14d md 含 top VMA" "top VMA" "$v"

# ============================================================ T15 华为非 NPU 设备排除（检视 #198）
v=$(jqpy "$WORK/o14/train_mem_affinity.json" \
  'sorted(d["system"]["npu_nodes"]), len(d["system"]["npu"])')
check_eq "T15 class 0x020000 网卡不进 NPU 集合（仅 accel 设备）" \
  "[1] 1" "$v"

# ============================================================ T16 C3 的 node 号语义（检视 #198 P1）
# mems=1（node1，非 cpu1）+ cpus=0-31（node0）→ 真值强制分离：
# 旧代码（把 mems 当 cpu 号→node0）会漏报，修复后必须触发 C3
mk_proc 5000 'RANK=9\0WORLD_SIZE=1\0' "$RANK1_MAPS" 824 "1" "0-31"
collect "$WORK/o16" >/dev/null 2>&1
v=$(jqpy "$WORK/o16/train_mem_affinity.json" \
  '[r for r in d["ranks"] if r["pid"]==5000][0]["hints"], [r for r in d["ranks"] if r["pid"]==5000][0]["mems_nodes"], [r for r in d["ranks"] if r["pid"]==5000][0]["cpu_nodes"]')
check_eq "T16 Mems_allowed_list 按 node 号解析（mems={1}∩cpu={0}=∅ → C3）" \
  "['C3_FORCE_REMOTE', 'C2_NPU_MISALIGN', 'C4_NODE_HOTSPOT'] [1] [0]" "$v"
rm -rf "$FP/5000"

# ============================================================ T17 rank 去重（DataLoader worker 继承 RANK）
mk_proc 6000 'RANK=0\0WORLD_SIZE=2\0' "$RANK1_MAPS" 824 "0-1" "0-31"
mk_proc 6001 'RANK=0\0WORLD_SIZE=2\0' "$RANK1_MAPS" 824 "0-1" "0-31"
mk_proc 6002 'RANK=0\0WORLD_SIZE=2\0' "$RANK1_MAPS" 824 "0-1" "0-31"
collect "$WORK/o17" >/dev/null 2>&1
v=$(jqpy "$WORK/o17/train_mem_affinity.json" \
  'len(d["ranks"]), [r for r in d["ranks"] if r["rank"]==0][0]["pid"], [r for r in d["ranks"] if r["rank"]==0][0].get("duplicate_pids")')
check_eq "T17a 同 rank 3 PID → 主条目(pid 最小 1000)+duplicate_pids" \
  "2 1000 [6000, 6001, 6002]" "$v"
rm -rf "$FP/6000" "$FP/6001" "$FP/6002"

# ============================================================ T18 观测缺口标记不被判据覆盖（检视 #198）
# rank0 的 numa_maps 删除（进程退出/权限场景）→ NUMA_MAPS_UNREADABLE 保留进 hints
mk_proc 7000 'RANK=5\0WORLD_SIZE=1\0' "$RANK1_MAPS" 824 "0-1" "0-31"
rm -f "$FP/7000/numa_maps"
collect "$WORK/o18" >/dev/null 2>&1
v=$(jqpy "$WORK/o18/train_mem_affinity.json" \
  '[r for r in d["ranks"] if r["pid"]==7000][0]["hints"]')
check_eq "T18 NUMA_MAPS_UNREADABLE 保留（不被判据 hints 覆盖）" \
  "['NUMA_MAPS_UNREADABLE']" "$v"
v=$("$PY" "$COLLECT" --proc-root "$FP" --sys-root "$FS" --out "$WORK/o18b" \
     2>&1 >/dev/null | grep -c 'rank 5')
check_eq "T18b 部分不可读 → stderr WARN 可见" "1" "$v"
rm -rf "$FP/7000"

# ============================================================ T19 kernel 无尾随换行
printf '5.10.0-test\n' > "$FP/sys/kernel/osrelease"
collect "$WORK/o19" >/dev/null 2>&1
v=$(jqpy "$WORK/o19/train_mem_affinity.json" \
  'd["meta"]["kernel"]')
check_eq "T19 meta.kernel 尾随换行已 strip" "5.10.0-test" "$v"

# ============================================================ T20 多作业 → MULTI_JOB（各自 rank 0 不混采）
mk_proc 8000 'RANK=0\0WORLD_SIZE=2\0' "$RANK1_MAPS" 824 "0-1" "0-31" 888
collect "$WORK/o20" >/dev/null 2>&1
check_eq "T20a 双作业 --auto → 1 MULTI_JOB" "1" "$?"
check_in "T20b stderr 列出候选作业" "MULTI_JOB" "$(cat "$WORK/o20.stderr")"
rm -rf "$FP/8000"

# ============================================================ T21 --pid 校验与去重
"$PY" "$COLLECT" --proc-root "$FP" --sys-root "$FS" --pid abc \
  --out "$WORK/o21a" >/dev/null 2>&1
check_eq "T21a --pid 非数字 → 1（参数错误，非 TARGET_MISSING）" "1" "$?"
mk_proc 6000 'RANK=0\0WORLD_SIZE=2\0' "$RANK1_MAPS" 824 "0-1" "0-31"
"$PY" "$COLLECT" --proc-root "$FP" --sys-root "$FS" --pid 1000,6000 \
  --out "$WORK/o21b" >/dev/null 2>&1
v=$(jqpy "$WORK/o21b/train_mem_affinity.json" \
  'len([r for r in d["ranks"] if r["rank"]==0]), [r for r in d["ranks"] if r["rank"]==0][0]["pid"], [r for r in d["ranks"] if r["rank"]==0][0].get("duplicate_pids")')
check_eq "T21b --pid 同 rank 两 PID → 归并（min pid 主 + duplicate）" \
  "1 1000 [6000]" "$v"
rm -rf "$FP/6000"

# ============================================================ T22 LOCAL_WORLD_SIZE 本机口径
mk_proc 8100 'RANK=2\0WORLD_SIZE=8\0LOCAL_WORLD_SIZE=1\0' \
  "$RANK1_MAPS" 824 "0-1" "0-31"
collect "$WORK/o22" >/dev/null 2>&1
check_in "T22 本机期望数（LOCAL_WORLD_SIZE=1，发现 3）→ WARN 本机口径" \
  "!= LOCAL_WORLD_SIZE" "$(cat "$WORK/o22.stderr")"
rm -rf "$FP/8100"

# ============================================================ T23 --top-vmas 默认 0 → 不打
collect "$WORK/o23" >/dev/null 2>&1
v=$(jqpy "$WORK/o23/train_mem_affinity.json" 'rs[0]["top_vmas"]')
check_eq "T23a 默认 top-vmas=0 → top_vmas 空表（文档口径'0（不打）'）" "[]" "$v"
"$PY" "$COLLECT" --proc-root "$FP" --sys-root "$FS" --out "$WORK/o23b" \
  2>/dev/null > "$WORK/o23b.md" || true
v=$(grep -c 'top VMA' "$WORK/o23b.md" || true)
check_eq "T23b markdown 默认无 top VMA 节" "0" "$v"

# ============================================================ T24 联合表：无数据 ≠ 干净 / 本地双校验优先 / 分节空 WARN
# AD2：快照 npu_pci 追加一个本地会被双校验排除的 node0 设备
# （按 producer 旧版 vendor-only 全列行为模拟——消费侧对旧/新 producer
#  一律本地双校验优先，该场景两种版本下都可能出现：旧版真有网卡、
#  新版理论无双网卡但快照集合与本地不一致时仍走同一 WARN+覆盖路径）
AD2="$WORK/afdir2"; mkdir -p "$AD2"
cat > "$AD2/topology_raw.txt" <<'EOF'
=== host
Linux test 5.10.0
=== clk_tck
100
=== node_cpulist
0|0-31
1|32-63
=== l3_domains
=== npu_pci
0000:0a:00.0|1
0000:0c:00.0|0
=== npu_smi_info_l
(absent)
=== target_view
cpuset=/
EOF
cp "$AD/summary.json" "$AD2/summary.json"
collect "$WORK/o24" --affinity-dir "$AD2" >/dev/null 2>&1
check_in "T24a 快照集合与本地双校验不一致 → WARN" "不一致" "$(cat "$WORK/o24.stderr")"
# 6100：anon 主 node1（∈ NPU 集合，联合表无 C2）且 pid 未匹配 → 应显示"无数据"
MAPS_N1='55d1a2b00000 default heap anon=200 N1=200 kernelpagesize_kB=4
7ffd1c3f5000 default stack anon=6 N1=6 kernelpagesize_kB=4'
mk_proc 6100 'RANK=3\0WORLD_SIZE=8\0' "$MAPS_N1" 824 "0-1" "32-63"
"$PY" "$COLLECT" --proc-root "$FP" --sys-root "$FS" --out "$WORK/o24b" \
  --affinity-dir "$AD2" 2>/dev/null > "$WORK/o24b.md" || true
rm -rf "$FP/6100"
check_in "T24b 联合表 NPU 列标注本地双校验覆盖" "本地双校验，已覆盖" \
  "$(cat "$WORK/o24b.md")"
v=$(grep -c '无数据（未匹配线程）' "$WORK/o24b.md" || true)
check_eq "T24c 未匹配 rank 显示'无数据'（非'干净'）" "1" "$v"
AD3="$WORK/afdir3"; mkdir -p "$AD3"
printf '=== host\nLinux test\n=== node_cpulist\n garbage\n' > "$AD3/topology_raw.txt"
collect "$WORK/o24d" --affinity-dir "$AD3" >/dev/null 2>&1
check_in "T24d topology 存在但分节全空 → WARN（不静默）" "均无有效行" \
  "$(cat "$WORK/o24d.stderr")"

# ============================================================ T25 diff：node 并集（纯迁移不误报新增）+ 消失 rank 进 struct
mk_env
collect "$WORK/o25a" >/dev/null 2>&1
# heap 100 页从 N0 全部迁到 N1（总量不变）
sed 's/anon=100 N0=100/anon=100 N1=100/' "$FP/1000/numa_maps" \
  > "$FP/1000/numa_maps.new" && mv "$FP/1000/numa_maps.new" "$FP/1000/numa_maps"
collect "$WORK/o25b" --diff "$WORK/o25a/train_mem_affinity.json" >/dev/null 2>&1
v=$(jqpy "$WORK/o25b/train_mem_affinity.json" \
  '(([x for x in d["diff"] if x["rank"]==0][0]["delta_total_kb"]), ([x for x in d["diff"] if x["rank"]==0][0]["per_node"] == {"0": {"total_kb": -400, "anon_kb": -400}, "1": {"total_kb": 400, "anon_kb": 400}}))')
check_eq "T25a 纯迁移 → Δtotal=0 且双侧 node 计入（并集，旧侧缺失按零）" \
  "(0, True)" "$v"
rm -rf "$FP/1001"
"$PY" "$COLLECT" --proc-root "$FP" --sys-root "$FS" --out "$WORK/o25c" \
  --diff "$WORK/o25b/train_mem_affinity.json" 2>/dev/null > "$WORK/o25c.md" || true
v=$(jqpy "$WORK/o25c/train_mem_affinity.json" \
  '[x for x in d["diff"] if x.get("status") == "gone"]')
check_eq "T25b 消失 rank 进 JSON struct（与新增对称）" "[{'rank': 1, 'status': 'gone'}]" "$v"
check_in "T25c markdown 增量表含消失行" "消失" "$(cat "$WORK/o25c.md")"

# ============================================================ T26 容器化训练：launcher 即 PID 1 时所有 rank 共享同一作业
# 修复前 bug：_tree_root() 把 ppid=1 的 rank 判为独立树根 → MULTI_JOB 退 1
# 修复后：ppid=1 返回 1 → 所有 rank 共享 tree:1 → 单一作业组
rm -rf "$FP"/[0-9]*   # 清掉前面所有 fixture 进程，确保 T26 只测容器场景
mk_proc 9100 'RANK=0\0WORLD_SIZE=3\0' "$RANK1_MAPS" 824 "0-1" "0-31" 1
mk_proc 9101 'RANK=1\0WORLD_SIZE=3\0' "$RANK1_MAPS" 824 "0-1" "0-31" 1
mk_proc 9102 'RANK=2\0WORLD_SIZE=3\0' "$RANK1_MAPS" 824 "0-1" "0-31" 1
collect "$WORK/o26" >/dev/null 2>&1
v=$?
check_eq "T26a 容器化 ppid=1 三个 rank → 单一作业 rc=0（非 MULTI_JOB）" "0" "$v"
v=$(jqpy "$WORK/o26/train_mem_affinity.json" 'len(d["ranks"]), sorted(e["rank"] for e in d["ranks"])')
check_eq "T26b 发现 3 rank（0/1/2），未误报多作业" "3 [0, 1, 2]" "$v"
rm -rf "$FP/9100" "$FP/9101" "$FP/9102"

# ============================================================ 汇总
echo
echo "===== $PASS passed, $FAIL failed ====="
[ "$FAIL" -eq 0 ]
