#!/usr/bin/env bash
set -euo pipefail

# 01_network_interfaces.sh — 网络接口发现
# 功能：识别所有 link up 状态的网络接口，并确定哪些正在主动处理流量
# 输出：活跃接口列表保存到 /tmp/active_interfaces.txt

echo "=== 网络接口（Link Up）==="
ip -a link show | grep -E "^[0-9]+:.*state UP" | while read -r line; do
    iface=$(echo "$line" | grep -oP '^[0-9]+: \K\w+')
    echo "接口: $iface (UP)"
done

echo ""
echo "=== 活动网络接口（有流量）==="

mapfile -t active_iface_list < <(
    sar -n DEV 1 5 2>/dev/null | awk '
        NF >= 11 && $2 != "IFACE" && $2 != "Average:" {
            iface = $2
            ifutil = $11 + 0
            if (ifutil > 0) {
                print iface "|" ifutil
            }
        }
    ' | awk -F'|' '!seen[$1]++ { print $0 }'
)

active_ifaces=""
for entry in "${active_iface_list[@]}"; do
    iface="${entry%%|*}"
    ifutil="${entry##*|}"
    echo "接口: $iface, IFUTIL: ${ifutil}%"
    if [[ -z "${active_ifaces}" ]]; then
        active_ifaces="${iface}"
    else
        active_ifaces="${active_ifaces} ${iface}"
    fi
done

if [[ -z "$active_ifaces" ]]; then
    echo "错误: 未找到有流量的网络接口"
    exit 1
fi

echo ""
echo "=== 保存活跃接口列表 ===="
echo "$active_ifaces" > /tmp/active_interfaces.txt
echo "活跃接口已保存到: /tmp/active_interfaces.txt"
echo "活跃接口: $active_ifaces"
