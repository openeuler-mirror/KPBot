#!/usr/bin/env bash
# kptools-train-mem-affinity — 环境自检
# 只读检查；退出码：0 就绪；2 环境不可用
set -euo pipefail

PROC_ROOT="${AT_PROC_ROOT:-/proc}"
SYS_ROOT="${AT_SYS_ROOT:-/sys}"

fail() { echo "DISCOVER_FAIL: $1" >&2; exit 2; }
note() { echo "OPTIONAL_ABSENT: $1" >&2; }

# --- 硬依赖 -------------------------------------------------------------
# 本 skill 全部脚本为纯 python3 stdlib（collect_rank_mem.py/run_tests.sh
# 均不使用 awk）——硬依赖只有 procfs 与 python3
[ -d "$PROC_ROOT" ] && [ -r "$PROC_ROOT" ] || fail "PROCFS_UNREADABLE ($PROC_ROOT)"

# 采集脚本需要 python3 >= 3.6（纯 stdlib）；python3 优先，缺失/为占位符时回退 python
found_py=""
for _c in python3 python; do
  if command -v "$_c" >/dev/null 2>&1 && "$_c" -c 'pass' >/dev/null 2>&1; then
    found_py="$_c"
    break
  fi
done
[ -n "$found_py" ] || fail "PYTHON_MISSING (install python3 >= 3.6 for collect_rank_mem.py)"
if ! "$found_py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 6) else 1)' 2>/dev/null; then
  fail "PYTHON3_TOO_OLD (collect_rank_mem.py needs >= 3.6)"
fi

# --- NUMA 拓扑可见性 ------------------------------------------------------
if [ ! -d "$SYS_ROOT/devices/system/node" ]; then
  note "$SYS_ROOT/devices/system/node (NUMA topology invisible: single-node box or restricted container)"
fi

# numa_balancing / THP 状态（best-effort，缺失只提示；信息行走 stderr，
# stdout 只留 DISCOVER_OK 供编排方判定）
if [ -r "$PROC_ROOT/sys/kernel/numa_balancing" ]; then
  echo "numa_balancing=$(cat "$PROC_ROOT/sys/kernel/numa_balancing")" >&2
else
  note "numa_balancing 状态不可读（判读时按未知处理）"
fi
if [ -r "$SYS_ROOT/kernel/mm/transparent_hugepage/enabled" ]; then
  echo "thp=$(cat "$SYS_ROOT/kernel/mm/transparent_hugepage/enabled")" >&2
else
  note "THP 状态不可读"
fi

# --- 可选工具（缺失只降级，不报错）---------------------------------------
command -v numastat >/dev/null 2>&1 \
  || note "numastat (numactl 包; 本 skill 不依赖, 仅人工复核用)"
command -v npu-smi >/dev/null 2>&1 \
  || note "npu-smi (非昇腾环境: C2 npu_misalign skipped)"

echo "DISCOVER_OK"
