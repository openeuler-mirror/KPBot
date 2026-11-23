#!/usr/bin/env bash
# kptools-cpuoffload-collector — 环境自检
# 只读检查；退出码：0 就绪；2 环境不可用
set -euo pipefail

PROC_ROOT="${AT_PROC_ROOT:-/proc}"

fail() { echo "DISCOVER_FAIL: $1" >&2; exit 2; }
note() { echo "OPTIONAL_ABSENT: $1" >&2; }

# --- 硬依赖 -------------------------------------------------------------
[ -d "$PROC_ROOT" ] && [ -r "$PROC_ROOT" ] || fail "PROCFS_UNREADABLE ($PROC_ROOT)"

found_py=""
for _c in python3 python; do
  if command -v "$_c" >/dev/null 2>&1 && "$_c" -c 'pass' >/dev/null 2>&1; then
    found_py="$_c"
    break
  fi
done
if [ -z "$found_py" ]; then
  fail "PYTHON_MISSING (install python3 >= 3.6 for probe.py)"
fi
if ! "$found_py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 6) else 1)' 2>/dev/null; then
  fail "PYTHON3_TOO_OLD (probe.py needs >= 3.6)"
fi

# --- pagemap 可读性（node 分析的前提）-----------------------------------
if [ -r "$PROC_ROOT/self/pagemap" ]; then
  echo "pagemap=self_ok"
else
  echo "pagemap=self_unreadable(跨 uid 需 root)" >&2
fi

# PFN 可读性（CAP_SYS_ADMIN）——非失败，只提示降级
pfn_test=$("$found_py" -c "
import os
fd = os.open('$PROC_ROOT/self/pagemap', os.O_RDONLY)
with open('$PROC_ROOT/self/maps') as fh:
    for line in fh:
        p = line.split(None, 5)
        if len(p) >= 2 and p[1].startswith('rw'):
            addr = int(p[0].split('-')[0], 16)
            w = int.from_bytes(os.pread(fd, 8, (addr // 4096) * 8), 'little')
            print('pfn_readable=%s' % ('true' if (w & ((1<<63))) and (w & ((1<<55)-1)) else 'false'))
            break
os.close(fd)
" 2>/dev/null || echo "pfn_readable=probe_failed")
if echo "$pfn_test" | grep -q 'pfn_readable=false'; then
  echo "OPTIONAL_ABSENT: CAP_SYS_ADMIN (pagemap PFN 被屏蔽——node 归属将降级为 unknown, 总量仍可用)" >&2
fi

# --- 可选工具 -----------------------------------------------------------
command -v npu-smi >/dev/null 2>&1 || note "npu-smi (设备亲和基准采集降级: 手动查 sysfs)"

echo "DISCOVER_OK"
