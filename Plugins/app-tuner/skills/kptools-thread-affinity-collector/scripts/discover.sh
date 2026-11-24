#!/usr/bin/env bash
# kptools-thread-affinity-collector — 环境自检
# 只读检查；退出码：0 就绪；2 环境不可用
#
# 依赖口径：本 skill 采集端（collect.sh）只依赖 bash/awk/sleep/procfs；
# python3 是**分析端**（kptools-thread-affinity-analyzer 的 analyze.py）
# 的依赖——采集机上缺失不算失败（样本可拿回分析机处理）。
set -euo pipefail

PROC_ROOT="${AT_PROC_ROOT:-/proc}"
SYS_ROOT="${AT_SYS_ROOT:-/sys}"

fail() { echo "DISCOVER_FAIL: $1" >&2; exit 2; }
note() { echo "OPTIONAL_ABSENT: $1" >&2; }

# --- 硬依赖（collect.sh 的实际依赖）--------------------------------------
[ -d "$PROC_ROOT" ] && [ -r "$PROC_ROOT" ] || fail "PROCFS_UNREADABLE ($PROC_ROOT)"
[ -r "$PROC_ROOT/stat" ] || fail "PROCFS_UNREADABLE ($PROC_ROOT/stat)"

command -v awk >/dev/null 2>&1 || fail "AWK_MISSING"
command -v sleep >/dev/null 2>&1 || fail "SLEEP_MISSING"

# --- 可选依赖（缺失只降级，不报错）--------------------------------------
# /sys 拓扑缺失时报告的 NUMA/NPU 列降级（虚拟机或容器限制场景）
[ -d "$SYS_ROOT/devices/system/cpu" ] || \
  note "$SYS_ROOT/devices/system/cpu (NUMA/NPU 归属列降级)"

# npu-smi 缺失 = 非昇腾环境，拓扑快照的 npu_smi_info_l 节记 (absent)
command -v npu-smi >/dev/null 2>&1 || \
  note "npu-smi (非昇腾主机：拓扑快照 npu_smi_info_l 记 absent)"

# python3 是分析端依赖：采集机缺失不失败（样本拿回分析机再分析）
found_py=""
for _c in python3 python; do
  if command -v "$_c" >/dev/null 2>&1 && "$_c" -c 'pass' >/dev/null 2>&1; then
    found_py="$_c"
    break
  fi
done
if [ -z "$found_py" ]; then
  note "python3 (分析端 analyze.py 需要 >= 3.6；采集端不需要——样本可拿回分析机)"
else
  pyver=$("$found_py" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "0.0")
  if ! awk -v v="$pyver" 'BEGIN{split(v,a,"."); exit !(a[1]>3 || (a[1]==3 && a[2]>=6))}'; then
    note "python3 $pyver < 3.6 (分析端需要；采集不受影响)"
  fi
fi

echo "DISCOVER_OK"
