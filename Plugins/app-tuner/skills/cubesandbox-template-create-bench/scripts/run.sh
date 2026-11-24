#!/usr/bin/env bash
set -euo pipefail
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMMON_DIR="$SKILL_DIR/../cubesandbox-lifecycle-bench"
if [ ! -f "$COMMON_DIR/scripts/cubesandbox_skill.py" ]; then
  echo "ERROR: 缺少共享执行器；请同时安装 cubesandbox-lifecycle-bench" >&2
  exit 2
fi
exec python3 "$COMMON_DIR/scripts/cubesandbox_skill.py" cubesandbox-template-create-bench "$@"
