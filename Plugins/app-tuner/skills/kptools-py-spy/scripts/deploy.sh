#!/bin/bash
# py-spy deploy — 检查/安装 py-spy（开源工具，pip 分发）
set -euo pipefail

print_help() {
    cat <<'EOF'
py-spy deploy — Check/install the py-spy tool (open source, via pip)

Behavior:
  - py-spy already installed: report and exit 0
  - root: pip3 install py-spy
  - non-root: pip3 install --user py-spy（提示 PATH 可能需包含 ~/.local/bin）

  exit 0: 成功或已安装；非 0: 安装失败（无 pip/无网络）
EOF
    exit 0
}

[ "${1:-}" = "--help" ] && print_help

if command -v py-spy &>/dev/null; then
    echo "py-spy: already installed ($(py-spy --version 2>/dev/null || echo 'unknown'), $(command -v py-spy))"
    exit 0
fi

if ! command -v pip3 &>/dev/null && ! command -v pip &>/dev/null; then
    echo "ERROR: pip/pip3 not found. Install python3-pip first, or download the py-spy binary from https://github.com/benfred/py-spy/releases" >&2
    exit 1
fi

echo "installing py-spy via pip ..."
if [ "$(id -u)" = "0" ]; then
    pip3 install py-spy || { echo "ERROR: pip3 install py-spy failed" >&2; exit 1; }
else
    pip3 install --user py-spy || { echo "ERROR: pip3 install --user py-spy failed" >&2; exit 1; }
    if ! command -v py-spy &>/dev/null && [ -d "$HOME/.local/bin" ]; then
        echo "NOTICE: py-spy 可能安装在 ~/.local/bin，请确认 PATH 包含该目录" >&2
    fi
fi

if command -v py-spy &>/dev/null; then
    echo "py-spy: installed ($(py-spy --version 2>/dev/null || echo 'unknown'), $(command -v py-spy))"
    exit 0
fi
echo "ERROR: py-spy 仍不可用，请检查 PATH 或手动安装" >&2
exit 1
