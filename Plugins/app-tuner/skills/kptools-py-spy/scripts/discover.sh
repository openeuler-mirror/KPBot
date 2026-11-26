#!/bin/bash
# py-spy discover — 检查 py-spy 可用性与 ptrace 采集环境
set -euo pipefail

print_help() {
    cat <<'EOF'
py-spy discover — Check py-spy availability and ptrace environment

Usage:
  discover.sh [--path]

Options:
  --path    Print the py-spy binary path only

Output:
  py-spy: READY (<version>, <path>) / NOT_INSTALLED (install hint)
  ptrace: <scope 值与权限判定>
  python3: <path> (<version>)

  exit 0（报告式，采集可行性由调用方结合各状态行判断）
EOF
    exit 0
}

case "${1:-}" in
    --help|-h) print_help ;;
    --path) command -v py-spy || exit 1; exit 0 ;;
    "") ;;
    *) echo "ERROR: unknown option: $1" >&2; exit 3 ;;
esac

if ! PYSPY_BIN=$(command -v py-spy 2>/dev/null); then
    echo "py-spy: NOT_INSTALLED (pip install py-spy 或运行 bash scripts/deploy.sh)"
    exit 0
fi
VERSION=$(py-spy --version 2>/dev/null || echo "unknown")
echo "py-spy: READY (${VERSION}, ${PYSPY_BIN})"

YAMA="/proc/sys/kernel/yama/ptrace_scope"
if [ -r "$YAMA" ]; then
    SCOPE=$(cat "$YAMA")
    if [ "$SCOPE" = "0" ]; then
        echo "ptrace: ptrace_scope=0（允许采集同 uid 进程）"
    elif [ "$(id -u)" = "0" ]; then
        echo "ptrace: ptrace_scope=${SCOPE}（root 不受限）"
    else
        echo "ptrace: ptrace_scope=${SCOPE} 且非 root —— 采集可能失败，需 root 或 sysctl kernel.yama.ptrace_scope=0"
    fi
else
    if [ "$(id -u)" = "0" ]; then
        echo "ptrace: 未启用 yama（默认允许）"
    else
        echo "ptrace: 未启用 yama（同 uid 进程默认允许）"
    fi
fi

if command -v python3 >/dev/null 2>&1; then
    echo "python3: $(command -v python3) ($(python3 --version 2>&1))"
else
    echo "python3: NOT_FOUND（目标侧解释器由被测环境决定，此处仅提示本机）"
fi
