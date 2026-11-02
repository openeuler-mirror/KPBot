#!/bin/bash
# os-collector discover — 检查只读采集能力（系统自带命令逐项）
set -euo pipefail

print_help() {
    cat <<'EOF'
os-collector discover — Check read-only collection capability per tool

Output: 逐项 AVAILABLE/OPTIONAL_ABSENT/MISSING + 容器提示
  exit 0（报告式）
EOF
    exit 0
}

[ "${1:-}" = "--help" ] && print_help

check_tool() {
    local tool="$1" pkg="$2" tag="${3:-MISSING}"
    if command -v "$tool" &>/dev/null; then
        echo "$tool: AVAILABLE ($(command -v "$tool"))"
    else
        echo "$tool: $tag (install: $pkg)"
    fi
}

check_tool sysctl "procps-ng"
check_tool systemctl "systemd（极简容器内可能缺失，对应模式 OPTIONAL_ABSENT）" "OPTIONAL_ABSENT"
check_tool tuned-adm "tuned" "OPTIONAL_ABSENT"
check_tool systemd-detect-virt "systemd" "OPTIONAL_ABSENT"
check_tool perf "perf" "OPTIONAL_ABSENT"
check_tool numactl "numactl" "OPTIONAL_ABSENT"

if [ -r /etc/os-release ]; then
    echo "os-release: readable"
else
    echo "os-release: NOT_FOUND"
fi

if [ -d /sys/fs/cgroup ] && grep -qs 'cgroup2' /proc/filesystems; then
    echo "cgroup: v2"
elif [ -d /sys/fs/cgroup ]; then
    echo "cgroup: v1"
else
    echo "cgroup: NOT_FOUND"
fi

if [ -f /.dockerenv ] || [ -r /run/.containerenv ]; then
    echo "container: yes（容器内采集为容器视角，宿主机视角需在宿主机执行）"
else
    echo "container: no（或未以典型标记检测到）"
fi
