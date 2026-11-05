#!/bin/bash
# node-profiler discover — 检查 node/npm/0x 可用性
set -euo pipefail

print_help() {
    cat <<'EOF'
node-profiler discover — Check node/npm/0x availability

Output: 逐项状态（0x 为 OPTIONAL_ABSENT）+ --cpu-prof 可用性（Node ≥ 12）
  exit 0（报告式）
EOF
    exit 0
}

[ "${1:-}" = "--help" ] && print_help

if command -v node &>/dev/null; then
    NODE_VER=$(node --version 2>/dev/null || echo "unknown")
    echo "node: AVAILABLE (${NODE_VER}, $(command -v node))"
    NODE_MAJOR=$(echo "$NODE_VER" | sed -E 's/^v([0-9]+).*/\1/')
    if [ "${NODE_MAJOR:-0}" -ge 12 ] 2>/dev/null; then
        echo "--cpu-prof: available（Node ≥ 12）"
    else
        echo "--cpu-prof: unavailable（需 Node ≥ 12，当前 ${NODE_VER}）"
    fi
else
    echo "node: MISSING（https://nodejs.org 安装；无 node 则本 SKILL 核心模式不可用）"
fi

if command -v npm &>/dev/null; then
    echo "npm: AVAILABLE ($(command -v npm))"
else
    echo "npm: MISSING（0x 安装需要 npm）"
fi

if command -v 0x &>/dev/null; then
    echo "0x: AVAILABLE ($(command -v 0x))"
else
    echo "0x: OPTIONAL_ABSENT（可选增强：npm install -g 0x）"
fi
