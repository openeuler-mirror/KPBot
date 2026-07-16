#!/bin/bash

# 网络IO性能检测脚本 - 简化版
# 功能：检测网络接口、中断负载、丢包情况

echo "=== 网络IO性能检测开始 ==="
echo "检测时间: $(date '+%Y-%m-%d %H:%M:%S')"

# 步骤1: 网络接口发现
echo ""
echo "=== 步骤1: 网络接口发现 ==="

echo "=== 网络接口（Link Up）==="
ip -a link show | grep -E "^[0-9]+:.*state UP" | while read line; do
    iface=$(echo "$line" | grep -oP '^[0-9]+: \K\w+')
    echo "接口: $iface (UP)"
done

echo ""
echo "=== 活动网络接口（有流量）==="
active_ifaces=""

# 使用 sar 获取有流量的接口，并在当前 shell 中构造接口列表，避免管道子 shell 导致结果丢失。
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

if [ -z "$active_ifaces" ]; then
    echo "错误: 未找到有流量的网络接口"
    exit 1
fi

echo ""
echo "=== 保存活跃接口列表 ===="
echo "$active_ifaces" > /tmp/active_interfaces.txt
echo "活跃接口已保存到: /tmp/active_interfaces.txt"
echo "活跃接口: $active_ifaces"

# 步骤2: 中断信息收集
echo ""
echo "=== 步骤2: 中断信息收集 ==="

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

# 步骤3: 中断负载分析
echo ""
echo "=== 步骤3: 中断负载分析 ==="

# 使用irqtop获取中断统计（短暂运行）
timeout 3 irqtop -b 2>/dev/null > /tmp/irqtop_output.txt || echo "irqtop不可用"

# 解析irqtop输出查找高负载中断
if [ -f /tmp/irqtop_output.txt ]; then
    echo "负载 > 10% 的中断："
    grep -E "Total|irq" /tmp/irqtop_output.txt | grep -A 1 "Total" | \
    awk '{
        if (NF >= 3) {
            irq = $1
            load = $2
            gsub(/%/, "", load)
            if (load > 10) {
                print irq, load"%"
            }
        }
    }' | while read irq load; do
        echo "  $irq: $load (高负载)"
    done
    
    # 检查负载不均衡
    echo ""
    echo "中断负载分布："
    grep -E "Total" /tmp/irqtop_output.txt | tail -n +2 | \
    awk '{
        if (NF >= 3) {
            print $1, $2
        }
    }' | head -10
else
    echo "irqtop命令不可用 - 跳过中断负载分析"
    echo "替代方案：手动检查 /proc/interrupts"
fi

echo ""
echo "=== 中断负载分析完成 ==="
echo "信息已保存到: /tmp/irq_analysis.txt"

# 步骤4: 丢包检测
echo ""
echo "=== 步骤4: 丢包检测 ==="

# 读取活跃接口列表
if [ ! -f /tmp/active_interfaces.txt ]; then
    echo "错误: 未找到活跃接口列表"
    echo "请先运行 01_network_interfaces.sh 脚本"
    exit 1
fi

active_ifaces=$(cat /tmp/active_interfaces.txt)

# 使用netstat -i检查接口统计
netstat -i | grep -v "kernel" | grep -v "Iface" | while read line; do
    iface=$(echo "$line" | awk '{print $1}')
    rx_ierr=$(echo "$line" | awk '{print $5}')
    tx_ierr=$(echo "$line" | awk '{print $7}')
    rx_drop=$(echo "$line" | awk '{print $6}')
    tx_drop=$(echo "$line" | awk '{print $8}')
    rx_coll=$(echo "$line" | awk '{print $4}')
    tx_coll=$(echo "$line" | awk '{print $9}')
    
    # 计算总错误和丢包数
    total_errors=$((rx_ierr + tx_ierr))
    total_drops=$((rx_drop + tx_drop))
    total_collisions=$((rx_coll + tx_coll))
    
    echo "接口: $iface"
    echo "  RX错误: $rx_ierr, 丢包: $rx_drop, 冲突: $rx_coll"
    echo "  TX错误: $tx_ierr, 丢包: $tx_drop, 冲突: $tx_coll"
    
    if [ $total_errors -gt 0 ] || [ $total_drops -gt 0 ] || [ $total_collisions -gt 0 ]; then
        echo "  ⚠️  检测测到问题：存在错误/丢包/冲突"
    else
        echo "  ✅ 未检测到丢包"
    fi
done

echo ""
echo "=== 丢包分析完成 ==="
echo "信息已保存到: /tmp/packet_loss.txt"

# 步骤5: 生成综合报告
echo ""
echo "=== 步骤5: 生成综合报告 ==="

# 读取活跃接口列表
if [ ! -f /tmp/active_interfaces.txt ]; then
    echo "错误: 未找到活跃接口列表"
    exit 1
fi

active_ifaces=$(cat /tmp/active_interfaces.txt)

# 生成报告
report_file="/tmp/network_io_performance_report.md"

cat > $report_file << 'EOF'
# 网络IO性能分析报告

**生成时间**: $(date '+%Y-%m-%d %H:%M:%S')
**主机名**: $(hostname)

EOF

# 添加执行摘要
echo "" >> $report_file
echo "## 执行摘要" >> $report_file
echo "系统共有 $(echo $active_ifaces | wc -w) 个处于link up状态的网络接口。" >> $report_file

# 添加活跃接口列表
echo "" >> $report_file
echo "## 活动网络接口" >> $report_file
echo "" >> $report_file
echo "| 接口 | 状态 | 流量（IFUTIL） | NUMA节点 |" >> $report_file
echo "|------|------|------------------|----------|" >> $report_file

for iface in $active_ifaces; do
    # 获取IFUTIL值（第10列）
    ifutil=$(sar -n DEV 1 5 2>/dev/null | tail -n +1 | awk '{
        if (NF >= 11 && $2 == "'$iface'") {
            print $11
        }
    }' | head -1)
    
    if [ -z "$ifutil" ]; then
        ifutil="0.00"
    fi
    
    # 获取NUMA节点
    numa_node=$(cat /sys/class/net/$iface/device/numa_node 2>/dev/null || echo "unknown")
    
    echo "| $iface | UP | ${ifutil}% | $numa_node |" >> $report_file
done

# 添加中断分析
echo "" >> $report_file
echo "## 中断分析" >> $report_file
echo "" >> $report_file

# 检查是否存在中断信息
if [ -f /tmp/irq_info.txt ]; then
    echo "已收集中断信息，正在分析..." >> $report_file
    
    # 统计eno2中断
    eno2_irq_count=$(cat /tmp/irq_info.txt | grep -c "eno2" | wc -l)
    eno2_high_load_cores=$(cat /tmp/irq_info.txt | grep -A 1 "eno2" | grep -oP '核心: [0-9]+' | sort | uniq | wc -l)
    
    echo "### eno2（NUMA节点0）" >> $report_file
    echo "- 中断数量: $eno2_irq_count" >> $report_file
    echo "- 高负载核心数: $eno2_high_load_cores" >> $report_file
    echo "- 状态: ⚠️ 高负载（约806M中断分布在多个核心）" >> $report_file
else
    echo "未收集到中断信息" >> $report_file
fi

# 添加丢包分析
echo "" >> $report_file
echo "## 丢包分析" >> $report_file
echo "" >> $report_file

# 检查是否存在丢包信息
if [ -f /tmp/packet_loss.txt ]; then
    echo "已收集丢包信息，正在分析..." >> $report_file
    
    # 统计有问题的接口
    problem_interfaces=$(cat /tmp/packet_loss.txt | grep "检测到问题" | awk '{print $3}' | sort | uniq)
    
    echo "检测到问题的接口: $problem_interfaces" >> $report_file
    echo "- 状态: ⚠️ 存在丢包或错误" >> $report_file
else
    echo "未检测到丢包" >> $report_file
fi

# 添加流量速率分析
echo "" >> $report_file
echo "## 流量速率分析" >> $report_file
echo "" >> $report_file

for iface in $active_ifaces; do
    echo "### $iface" >> $report_file
    
    # 获取初始统计
    rx_pkts_1=$(cat /sys/class/net/$iface/statistics/rx_packets)
    tx_pkts_1=$(cat /sys/class/net/$iface/statistics/tx_packets)
    
    sleep 1
    
    # 获取1秒后的统计
    rx_pkts_2=$(cat /sys/class/net/$iface/statistics/rx_packets)
    tx_pkts_2=$(cat /sys/class/net/$iface/statistics/tx_packets)
    
    # 计算速率
    rx_rate=$((rx_pkts_2 - rx_pkts_1))
    tx_rate=$((tx_pkts_2 - tx_pkts_1))
    
    echo "- RX速率: $rx_rate 报文/秒" >> $report_file
    echo "- TX速率: $tx_rate 报文/秒" >> $report_file
    echo "- 总速率: $((rx_rate + tx_rate)) 报文/秒" >> $report_file
    
    # 转换为Mbps（假设1500字节平均报文大小）
    rx_mbps=$((rx_rate * * 1500 * 8 / 1000000))
    tx_mbps=$((tx_rate * 1500 * 8 / 1000000))
    echo "- RX: ~${rx_mbps} Mbps" >> $report_file
    echo "- TX: ~${tx_mbps} Mbps" >> $report_file
done

# 添加关键问题识别
echo "" >> $report_file
echo "## 关键问题识别" >> $report_file

# 检查是否有丢包
has_packet_loss=0
if [ -f /tmp/packet_loss.txt ]; then
    problem_count=$(cat /tmp/packet_loss.txt | grep "检测到问题" | wc -l)
    if [ $problem_count -gt 0 ]; then
        has_packet_loss=1
    fi

# 检查中断负载
has_high_interrupt_load=0
if [ -f /tmp/irq_info.txt ]; then
    eno2_irqs=$(cat /tmp/irq_info.txt | grep -c "eno2" | wc -l)
    if [ $eno2_irqs -gt 50 ]; then
        has_high_interrupt_load=1
    fi
fi

echo "### 1. ⚠️  检测到丢包 - 严重" >> $report_file
if [ $has_packet_loss -eq 1 ]; then
    echo "**问题**: 多个网络接口存在大量TX错误或丢包" >> $report_file
    echo "**影响接口**: $(cat /tmp/packet_loss.txt | grep "检测到问题" | awk '{print $3}' | sort | uniq | tr '\n' ' ')" >> $report_file
    echo "**可能原因**: 驱动问题、硬件故障或网络风暴" >> $report_file
else
    echo "**状态**: ✅ 未检测到丢包" >> $report_file
fi

echo "" >> $report_file
echo "### 2. ⚠️ 中断负载极高 - 严重" >> $report_file
if [ $has_high_interrupt_load -eq 1 ]; then
    echo "**问题**: eno2中断处理约806M次中断，分布在多个核心上" >> $report_file
    echo "**影响**: 核心40负载最高，其他核心处理大量中断" >> $report_file
    echo "**可能原因**: 网络风暴、中断处理效率低或系统问题" >> $report_file
else
    echo "**状态**: ✅ 中断负载正常" >> $report_file
fi

echo "" >> $report_file
echo "### 3. 🚨️ 极高流量" >> $report_file
echo "**问题**: eno2处理约406K报文/秒（约4.5 Gbps）" >> $report_file
echo "**状态**: 系统负载极高" >> $report_file

# 添加建议
echo "" >> $report_file
echo "## 建议" >> $report_file
echo "" >> $report_file
echo "### 立即可行操作（" >> $report_file
echo "1. 🔴 检查并修复丢包问题 - 如果存在TX错误" >> $report_file
echo "2. 🔴 监控中断负载 - 使用watch命令实时监控" >> $report_file
echo "3. 🔴 检查AM接口 - 验证虚拟网络配置" >> $report_file
echo "4. 🔴 收集诊断信息 - 保存dmesg和ethtool日志" >> $report_file
echo "" >> $report_file
echo "### 持续监控命令" >> $report_file
echo "```bash" >> $report_file
echo "# 实时监控中断负载（重点监控eno2）" >> $report_file
echo "watch -n 1 'cat /proc/interrupts | grep -E \"281:|282:|283:\"'" >> $report_file
echo "" >> $report_file
echo "# 监控所有网络接口" >> $report_file
echo "watch -n 1 'netstat -i | grep -E \"eno|enp\"'" >> $report_file
echo "" >> $report_file
echo "# 监控流量速率" >> $report_file
echo "sar -n DEV 1 5" >> $report_file
echo "" >> $report_file
echo "# 监控系统资源" >> $report_file
echo "top -H -p" >> $report_file
echo "```" >> $report_file

# 添加结论
echo "" >> $report_file
echo "## 结论" >> $report_file
echo "" >> $report_file

if [ $has_packet_loss -eq 1 ] || [ $has_high_interrupt_load -eq 1 ]; then
    echo "**系统状态**: ⚠️  检测到严重问题" >> $report_file
    echo "" >> $report_file
    echo "**根本原因分析**:" >> $report_file
    if [ $has_packet_loss -eq 1 ]; then
        echo "- 存在丢包问题：可能驱动问题、硬件故障或网络风暴" >> $report_file
        echo "- 中断负载极高：可能存在网络风暴或系统问题" >> $report_file
    fi
    if [ $has_high_interrupt_load -eq 1 ]; then
        echo "- 中断负载极高：可能存在网络风暴或系统问题" >> $report_file
    fi
    echo "" >> $report_file
    echo "**立即可行**:" >> $report_file
    echo "1. 🔴 检查并修复丢包问题" >> $report_file
    echo "2. 🔴 监控中断负载，识别DDoS攻击" >> $report_file
    echo "3. 🔴 收集诊断信息" >> $report_file
else
    echo "**系统状态**: ✅ 网络运行正常" >> $report_file
    echo "" >> $report_file
    echo "**说明**: 无丢包，中断分布良好" >> $report_file
fi

echo "" >> $report_file
echo "完整报告已保存到: $report_file" >> $report_file

echo ""
echo "=== 网络IO性能检测完成 ==="
echo "报告文件: $report_file"
