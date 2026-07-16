#!/bin/bash

# 网络IO性能检测脚本 - 网络接口发现
# 功能：识别所有link up状态的网络接口，并确定哪些正在主动处理流量

echo "=== 网络接口（Link Up）==="
ip -a link show | grep -E "^[0-9]+:.*state UP" | while read line; do
    iface=$(echo "$line" | grep -oP '^[0-9]+: \K\w+')
    echo "接口: $iface (UP)"
done

echo ""
echo "=== 活动网络接口（有流量）==="
sar -n DEV 1 5 2>/dev/null | tail -n +1 | awk '{
    if (NF >= 8) {
        iface = $2
        ifutil = $11  # 接口利用率百分比
        if (ifutil > 0) {
            print iface, ifutil
        }
    }
}' | while read iface ifutil; do
    echo "接口: $iface, IFUTIL: $ifutil%"
done

echo ""
echo "=== 保存活跃接口列表 ===="
active_ifaces=$(sar -n DEV 1 5 2>/dev/null | tail -n +1 | awk '{
    if (NF >= 8) {
        iface = $2
        ifutil = $11  # 接口利用率百分比
        if (ifutil > 0) {
            print iface
        }
    }
}')
echo "$active_ifaces" > /.config/opencode/skills/network-io-performance/scripts/active_interfaces.txt
echo "活跃接口已保存到: /.config/opencode/skills/network-io-performance/scripts/active_interfaces.txt"
echo "活跃接口: $active_ifaces"

echo ""
echo "=== 保存活跃接口列表 ===="
active_ifaces=$(sar -n DEV 1 5 2>/dev/null | tail -n +1 | awk '{
    if (NF >= 11) {
        iface = $2
        ifutil = $10
        if (ifutil > 0) {
            print iface
        }
    }
}')
echo "$active_ifaces" > /tmp/active_interfaces.txt
echo "活跃接口已保存到: /tmp/active_interfaces.txt"
echo "活跃接口: $active_ifaces"
