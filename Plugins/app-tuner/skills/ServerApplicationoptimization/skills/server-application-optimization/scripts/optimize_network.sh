#!/bin/bash
# 远程压测服务端网络优化脚本
# 用途: 在服务器应用优化前消除网络侧阻碍因素，将服务端 CPU 利用率推至饱和
# 默认只输出 dry-run 计划；真实执行必须显式传 --execute 和 --approved-change-id。
# 使用: bash optimize_network.sh --iface <iface> --app-cpus <start-end> --port <port>
# 执行: sudo bash optimize_network.sh --execute --approved-change-id <id> --iface <iface> --app-cpus <start-end> --port <port>
# 回退: sudo bash optimize_network.sh --rollback --iface <iface>
#
# 配套 skill: subskills/network-optimization/SKILL.md

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
fail() { echo -e "${RED}[FAIL]${NC} $1"; exit 1; }

# 默认参数
IFACE=""
APP_CPUS=""
PORT=3306
ROLLBACK=false
EXECUTE=false
APPROVED_CHANGE_ID=""
BACKUP_DIR=""
IPTABLES_APPLIED=false

usage() {
    cat <<'EOF'
Usage:
  dry-run:  bash optimize_network.sh --iface <iface> --app-cpus <start-end> --port <port>
  execute:  sudo bash optimize_network.sh --execute --approved-change-id <id> --iface <iface> --app-cpus <start-end> --port <port>
  rollback: sudo bash optimize_network.sh --rollback --iface <iface>

Options:
  --iface <iface>       网卡接口名 (如 enp133s0)
  --app-cpus <start-end> 应用绑核范围 (如 32-39)
  --port <port>         应用监听端口 (默认 3306)
  --execute             真实执行网络优化；未指定时只输出计划
  --approved-change-id <id> 用户批准的变更编号，真实执行时必填
  --rollback            回退所有优化
  -h, --help            显示帮助

Examples:
  bash optimize_network.sh --iface enp133s0 --app-cpus 32-39 --port 3308
  sudo bash optimize_network.sh --execute --approved-change-id NET-20260621-001 --iface enp133s0 --app-cpus 32-39 --port 3308
  sudo bash optimize_network.sh --rollback --iface enp133s0
EOF
    exit 0
}

# 解析参数
while [[ $# -gt 0 ]]; do
    case "$1" in
        --iface)   IFACE="$2"; shift 2 ;;
        --app-cpus) APP_CPUS="$2"; shift 2 ;;
        --port)    PORT="$2"; shift 2 ;;
        --execute) EXECUTE=true; shift ;;
        --approved-change-id) APPROVED_CHANGE_ID="$2"; shift 2 ;;
        --rollback) ROLLBACK=true; shift ;;
        -h|--help) usage ;;
        *) fail "未知参数: $1" ;;
    esac
done

# 验证必要参数
if [ -z "$IFACE" ]; then
    fail "必须指定 --iface 参数"
fi

BACKUP_DIR="/tmp/network_opt_backup_${IFACE}"

# 只有真实执行和回退需要 root；dry-run 可由普通用户预审。
if { [ "$EXECUTE" = true ] || [ "$ROLLBACK" = true ]; } && [ "$(id -u)" -ne 0 ]; then
    fail "真实执行或回退需要 root 权限: sudo bash $0"
fi

rollback() {
    echo "=== 回退网络优化 ==="

    # 恢复防火墙
    if [ -f "$BACKUP_DIR/firewalld_state" ] && command -v systemctl &>/dev/null; then
        previous_firewalld_state=$(cat "$BACKUP_DIR/firewalld_state" 2>/dev/null || true)
        previous_firewalld_enabled=$(cat "$BACKUP_DIR/firewalld_enabled" 2>/dev/null || true)

        if [ "$previous_firewalld_state" = "active" ]; then
            systemctl start firewalld 2>/dev/null && log "firewalld 已恢复为 active" || warn "firewalld 恢复失败"
        else
            systemctl stop firewalld 2>/dev/null && log "firewalld 已恢复为 inactive" || warn "firewalld 停止失败"
        fi

        if [ "$previous_firewalld_enabled" = "enabled" ]; then
            systemctl enable firewalld 2>/dev/null || warn "firewalld enable 恢复失败"
        elif [ "$previous_firewalld_enabled" = "disabled" ]; then
            systemctl disable firewalld 2>/dev/null || warn "firewalld disable 恢复失败"
        fi
    elif command -v systemctl &>/dev/null; then
        warn "缺少 firewalld 状态备份，跳过 firewalld 恢复"
    fi

    if [ -f "$BACKUP_DIR/iptables_rules_applied" ] && command -v iptables &>/dev/null; then
        if grep -q '^INPUT_PORT$' "$BACKUP_DIR/iptables_rules_applied"; then
            iptables -D INPUT -p tcp --dport "$PORT" -j ACCEPT 2>/dev/null && log "已删除 INPUT 放行规则" || warn "删除 INPUT 规则失败"
        fi
        if grep -q '^OUTPUT_PORT$' "$BACKUP_DIR/iptables_rules_applied"; then
            iptables -D OUTPUT -p tcp --sport "$PORT" -j ACCEPT 2>/dev/null && log "已删除 OUTPUT 放行规则" || warn "删除 OUTPUT 规则失败"
        fi
    fi

    # 恢复 RPS
    if [ -d "/sys/class/net/$IFACE/queues" ]; then
        for i in /sys/class/net/$IFACE/queues/rx-*/rps_cpus; do
            local qname=$(basename $(dirname "$i"))
            if [ -f "$BACKUP_DIR/rps_${qname}" ]; then
                cat "$BACKUP_DIR/rps_${qname}" > "$i"
            fi
        done
        log "RPS 已恢复"
    fi

    # 恢复 sysctl
    if [ -f "$BACKUP_DIR/sysctl.conf" ]; then
        sysctl -p "$BACKUP_DIR/sysctl.conf" 2>/dev/null && log "sysctl 已恢复" || warn "sysctl 恢复失败"
    fi

    # 恢复 ethtool coalescing
    if [ -f "$BACKUP_DIR/ethtool_coalesce.txt" ]; then
        local rx_usecs=""
        local rx_frames=""
        rx_usecs=$(awk -F': *' '/^[[:space:]]*rx-usecs:/ {print $2; exit}' "$BACKUP_DIR/ethtool_coalesce.txt")
        rx_frames=$(awk -F': *' '/^[[:space:]]*rx-frames:/ {print $2; exit}' "$BACKUP_DIR/ethtool_coalesce.txt")
        if [[ -n "$rx_usecs" && -n "$rx_frames" ]]; then
            ethtool -C "$IFACE" rx-usecs "$rx_usecs" rx-frames "$rx_frames" 2>/dev/null && \
                log "ethtool coalesce 已按备份恢复: rx-usecs=$rx_usecs rx-frames=$rx_frames" || \
                warn "ethtool 恢复失败"
        else
            warn "备份中缺少 rx-usecs/rx-frames，跳过 ethtool 恢复"
        fi
    fi

    echo "=== 回退完成 ==="
    exit 0
}

if [ "$ROLLBACK" = true ]; then
    rollback
fi

# 验证 app-cpus
if [ -z "$APP_CPUS" ]; then
    fail "优化模式必须指定 --app-cpus 参数（如 32-39）"
fi

if [ "$EXECUTE" = true ] && [ -z "$APPROVED_CHANGE_ID" ]; then
    fail "真实执行必须指定 --approved-change-id，确认该网络变更已由用户批准"
fi

echo "=========================================="
echo "  远程压测网络优化 — 目标 CPU 饱和"
echo "  模式: $([ "$EXECUTE" = true ] && echo execute || echo dry-run)"
echo "  网卡: $IFACE"
echo "  应用绑核: CPU $APP_CPUS"
echo "  应用端口: $PORT"
if [ "$EXECUTE" = true ]; then
    echo "  批准变更: $APPROVED_CHANGE_ID"
fi
echo "=========================================="
echo ""

if [ "$EXECUTE" != true ]; then
    cat <<EOF
Dry-run 计划，不会修改系统：
1. 备份 RPS、sysctl、ethtool coalesce 和 firewalld 状态到 ${BACKUP_DIR}
2. 停止/禁用 firewalld，或临时添加 iptables 端口放行规则
3. 调整 ${IFACE} RX 队列 RPS，避开应用绑核 CPU ${APP_CPUS}
4. 写入 TCP sysctl 参数：tcp_fastopen、tcp_low_latency、tcp_tw_reuse、somaxconn、tcp_max_syn_backlog、tcp_no_metrics_save
5. 调整网卡中断聚合：rx-usecs / rx-frames

真实执行命令：
sudo bash $0 --execute --approved-change-id <id> --iface ${IFACE} --app-cpus ${APP_CPUS} --port ${PORT}

回退命令：
sudo bash $0 --rollback --iface ${IFACE} --port ${PORT}
EOF
    exit 0
fi

# === 备份当前状态 ===
mkdir -p "$BACKUP_DIR"
echo "=== 备份当前配置 ==="
rm -f "$BACKUP_DIR/iptables_rules_applied"

# 备份 RPS
for i in /sys/class/net/$IFACE/queues/rx-*/rps_cpus; do
    [ -f "$i" ] && cat "$i" > "$BACKUP_DIR/rps_$(basename $(dirname "$i"))"
done
log "RPS 配置已备份"

# 备份 sysctl
{
    sysctl net.ipv4.tcp_fastopen 2>/dev/null || true
    sysctl net.ipv4.tcp_low_latency 2>/dev/null || true
    sysctl net.ipv4.tcp_tw_reuse 2>/dev/null || true
    sysctl net.core.somaxconn 2>/dev/null || true
    sysctl net.ipv4.tcp_max_syn_backlog 2>/dev/null || true
    sysctl net.ipv4.tcp_no_metrics_save 2>/dev/null || true
} > "$BACKUP_DIR/sysctl.conf"
log "sysctl 已备份"

# 备份 ethtool
ethtool -c "$IFACE" 2>/dev/null > "$BACKUP_DIR/ethtool_coalesce.txt" || true
log "ethtool 配置已备份"

if command -v systemctl &>/dev/null; then
    systemctl is-active firewalld &>/dev/null && echo "active" > "$BACKUP_DIR/firewalld_state" || echo "inactive" > "$BACKUP_DIR/firewalld_state"
    systemctl is-enabled firewalld &>/dev/null && echo "enabled" > "$BACKUP_DIR/firewalld_enabled" || echo "disabled" > "$BACKUP_DIR/firewalld_enabled"
fi

echo ""

# === Step 1: 关闭防火墙 ===
echo "=== Step 1: 关闭防火墙 ==="
if systemctl is-active firewalld &>/dev/null; then
    systemctl stop firewalld
    systemctl disable firewalld
    log "firewalld 已停止并禁用"
elif command -v iptables &>/dev/null; then
    iptables -C INPUT -p tcp --dport "$PORT" -j ACCEPT 2>/dev/null || \
        { iptables -I INPUT -p tcp --dport "$PORT" -j ACCEPT; echo "INPUT_PORT" >> "$BACKUP_DIR/iptables_rules_applied"; IPTABLES_APPLIED=true; }
    iptables -C OUTPUT -p tcp --sport "$PORT" -j ACCEPT 2>/dev/null || \
        { iptables -I OUTPUT -p tcp --sport "$PORT" -j ACCEPT; echo "OUTPUT_PORT" >> "$BACKUP_DIR/iptables_rules_applied"; IPTABLES_APPLIED=true; }
    log "iptables 已放行端口 $PORT"
else
    warn "未检测到 firewalld 或 iptables，跳过"
fi

echo ""

# === Step 2: 调整 RPS ===
echo "=== Step 2: 调整 RPS（避开应用绑核 CPU $APP_CPUS）"

# 计算 RPS 掩码：避开应用 CPU
# 将 APP_CPUS (如 "32-39") 转为要避开的范围
APP_START=${APP_CPUS%-*}
APP_END=${APP_CPUS#*-}

# 计算所有可用 CPU 数
TOTAL_CPUS=$(nproc 2>/dev/null || cat /proc/cpuinfo | grep -c "^processor")

# 生成避开应用核的 RPS 掩码
# 策略：使用应用核之后的相邻核，如果不够则从头开始
RPS_MASK=""
RPS_CPUS=""
NEXT_START=$((APP_END + 1))
NEXT_END=$((NEXT_START + (APP_END - APP_START)))

if [ "$NEXT_END" -lt "$TOTAL_CPUS" ]; then
    RPS_CPUS="${NEXT_START}-${NEXT_END}"
else
    # 回绕到开头
    NEXT_END=$((TOTAL_CPUS - 1))
    RPS_CPUS="${NEXT_START}-${NEXT_END},0-$((APP_START - 1))"
fi

# 生成十六进制掩码
compute_mask() {
    local highest=-1
    local cpus="$1"
    local -a bits=()
    IFS=',' read -ra ranges <<< "$cpus"
    for range in "${ranges[@]}"; do
        local s=${range%-*}
        local e=${range#*-}
        for ((c=s; c<=e; c++)); do
            bits[c]=1
            (( c > highest )) && highest=$c
        done
    done

    if (( highest < 0 )); then
        printf "0"
        return
    fi

    local result=""
    local nibble=0
    local nibble_value=0
    local hex_chars=(0 1 2 3 4 5 6 7 8 9 a b c d e f)
    for ((c=0; c<=highest; c++)); do
        if [[ "${bits[c]:-0}" -eq 1 ]]; then
            nibble_value=$((nibble_value | (1 << nibble)))
        fi
        nibble=$((nibble + 1))
        if (( nibble == 4 )); then
            result="${hex_chars[nibble_value]}${result}"
            nibble=0
            nibble_value=0
        fi
    done

    if (( nibble > 0 )); then
        result="${hex_chars[nibble_value]}${result}"
    fi

    result="${result#"${result%%[!0]*}"}"
    printf "%s" "${result:-0}"
}

RPS_MASK=$(compute_mask "$RPS_CPUS")

RPS_COUNT=0
for i in /sys/class/net/$IFACE/queues/rx-*/rps_cpus; do
    if [ -f "$i" ]; then
        OLD=$(cat "$i" | tr -d ' ,\n')
        echo "$RPS_MASK" > "$i"
        NEW=$(cat "$i" | tr -d ' ,\n')
        RPS_COUNT=$((RPS_COUNT + 1))
    fi
done
log "已调整 $RPS_COUNT 个 RPS 队列 → 掩码 $RPS_MASK (CPU $RPS_CPUS，避开应用核 $APP_CPUS)"

echo ""

# === Step 3: TCP 调优 ===
echo "=== Step 3: TCP 调优 ==="
sysctl -w net.ipv4.tcp_fastopen=3 2>/dev/null && log "tcp_fastopen=3 (客户端+服务端)" || warn "tcp_fastopen 设置失败"
sysctl -w net.ipv4.tcp_low_latency=1 2>/dev/null && log "tcp_low_latency=1" || warn "tcp_low_latency 设置失败"
sysctl -w net.ipv4.tcp_tw_reuse=1 2>/dev/null || true
sysctl -w net.core.somaxconn=65535 2>/dev/null || true
sysctl -w net.ipv4.tcp_max_syn_backlog=8192 2>/dev/null || true
sysctl -w net.ipv4.tcp_no_metrics_save=1 2>/dev/null || true

echo ""

# === Step 4: 网卡中断聚合 ===
echo "=== Step 4: 网卡中断聚合 ==="
if ethtool -C "$IFACE" rx-usecs 0 rx-frames 1 2>/dev/null; then
    log "ethtool 中断聚合已调整: rx-usecs=0, rx-frames=1"
else
    warn "ethtool -C rx-usecs=0 不支持，尝试降级"
    ethtool -C "$IFACE" rx-usecs 10 rx-frames 1 2>/dev/null && log "降级: rx-usecs=10" || warn "ethtool -C 设置失败（容器环境需在宿主机执行）"
fi

echo ""
echo "=========================================="
echo "  优化完成"
echo "  回退: sudo bash $0 --rollback --iface $IFACE"
echo "=========================================="
