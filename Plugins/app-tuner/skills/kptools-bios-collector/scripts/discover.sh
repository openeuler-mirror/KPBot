#!/bin/bash
# bios-collector discover — 检查采集依赖工具与运行环境
set -euo pipefail

print_help() {
    cat <<'EOF'
bios-collector discover — Check tool availability for each collection mode

Output: 逐项 AVAILABLE/MISSING + root 状态 + BMC 未检测声明
  exit 0（报告式）
EOF
    exit 0
}

[ "${1:-}" = "--help" ] && print_help

check_tool() {
    local tool="$1" pkg="$2"
    if command -v "$tool" &>/dev/null; then
        echo "$tool: AVAILABLE ($(command -v "$tool"))"
    else
        echo "$tool: MISSING (install: $pkg)"
    fi
}

check_tool dmidecode "dmidecode"
check_tool cpupower "kernel-tools"
check_tool lspci "pciutils"
check_tool numactl "numactl"
check_tool curl "curl"
check_tool lscpu "util-linux"
check_tool python3 "python3"

if [ "$(id -u)" = "0" ]; then
    echo "root: yes（dmidecode/cpupower/lspci -vvv 可用）"
else
    echo "root: no（dmidecode/cpupower/lspci -vvv 模式将降级或失败）"
fi

echo "BMC: 可达性未检测（--redfish 模式需 --bmc-host 或 REDFISH_HOST 环境变量后实测）"
