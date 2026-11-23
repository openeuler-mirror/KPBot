#!/usr/bin/env bash
# kptools-affinity-trace — 环境自检
# 只读检查；退出码：0 就绪；2 环境不可用
set -euo pipefail

PROC_ROOT="${AT_PROC_ROOT:-/proc}"

fail() { echo "DISCOVER_FAIL: $1" >&2; exit 2; }
note() { echo "OPTIONAL_ABSENT: $1" >&2; }

# --- 硬依赖 -------------------------------------------------------------
[ -d "$PROC_ROOT" ] && [ -r "$PROC_ROOT" ] || fail "PROCFS_UNREADABLE ($PROC_ROOT)"
[ -r "$PROC_ROOT/stat" ] || fail "PROCFS_UNREADABLE ($PROC_ROOT/stat)"

command -v awk >/dev/null 2>&1 || fail "AWK_MISSING"
command -v sleep >/dev/null 2>&1 || fail "SLEEP_MISSING"

# 报告阶段需要 python3 >= 3.6（无第三方库）；python3 优先，缺失/为占位符时回退 python
found_py=""
for _c in python3 python; do
  if command -v "$_c" >/dev/null 2>&1 && "$_c" -c 'pass' >/dev/null 2>&1; then
    found_py="$_c"
    break
  fi
done
if [ -z "$found_py" ]; then
  fail "PYTHON_MISSING (install python3 >= 3.6 for build_report.py)"
fi
pyver=$("$found_py" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "0.0")
if ! awk -v v="$pyver" 'BEGIN{split(v,a,"."); exit !(a[1]>3 || (a[1]==3 && a[2]>=6))}'; then
  fail "PYTHON3_TOO_OLD ($pyver, build_report.py needs >= 3.6)"
fi

# --- 可选依赖（缺失只降级，不报错）--------------------------------------
# /sys 拓扑缺失时报告的 NUMA/L3 列降级（虚拟机或容器限制场景）
[ -d /sys/devices/system/cpu ] || note "/sys/devices/system/cpu (NUMA/L3 columns degraded)"

# npu-smi 缺失 = 非昇腾环境，NPU 对齐判定(A2)自动跳过
command -v npu-smi >/dev/null 2>&1 || note "npu-smi (non-Ascend host: A2 npu_numa_misaligned skipped)"

echo "DISCOVER_OK"
