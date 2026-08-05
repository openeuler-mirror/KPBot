#!/bin/bash

# 网络IO性能检测脚本 - 中断信息收集
# 功能：收集活跃网络接口的中断号及其CPU亲和性

echo "=== 活动接口的中断信息 ==="

# 读取活跃接口列表
if [ ! -f /tmp/active_interfaces.txt ]; then
    echo "错误：未找到活跃接口列表"
    echo "请先运行 01_network_interfaces.sh 脚本"
    exit 1
fi

active_ifaces=$(cat /tmp/active_interfaces.txt)

for iface in $active_ifaces; do
    echo ""
    echo "接口: $iface"
    
    # 获取此接口的中断号
    irqs=$(cat /proc/interrupts | grep "$iface" | awk '{print $1}' | tr -d ':')
    
    if [ -n "$irqs" ]; then
        echo "中断号: $irqs"
        
        # 获取此接口的NUMA节点
        numa_node=$(cat /sys/class/net/$iface/device/numa_node 2>/dev/null || echo "unknown")
        echo "NUMA节点: $numa_node"
        
        # 获取每个中断的亲和性
        for irq in $irqs; do
            affinity=$(cat /proc/irq/$irq/smp_affinity_list 2>/dev/null || echo "unknown")
            echo "  中断 $irq -> 核心: $affinity"
        done
    else
        echo "未找到 $iface 的中断号"
    fi
done

echo ""
echo "=== 中断信息收集完成 ==="
echo "信息已保存到: /tmp/irq_info.txt"
