#!/usr/bin/env bash
# 只读检查 VectorDBBench CLI 是否可执行并输出命令列表。
# 用法: smoke_check.sh /path/to/vectordbbench
set -euo pipefail

CLI=${1:?用法: smoke_check.sh /path/to/vectordbbench}
[[ -x "$CLI" ]] || { echo "CLI 不可执行: $CLI" >&2; exit 2; }
help=$("$CLI" --help 2>&1) || { echo "vectordbbench --help 失败: $CLI" >&2; exit 1; }
printf '%s\n' "$help" | grep -q "Commands:" \
  || { echo "vectordbbench --help 输出中未找到 Commands:" >&2; exit 1; }
echo "vectordbbench CLI OK: $CLI"
