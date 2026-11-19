#!/usr/bin/env bash
# ============================================================================
# RDMA 带宽监控共享库 (支持本地 + 远程节点)
# 真源：本 skill（3fs-hotspot-code-optimization）；
#       3fs-parameter-tuning 的 fio_bench.sh 可选 source 本库
#
# 用法: 在测试脚本中 source 本文件，然后调用 start_rdma_monitor / stop_rdma_monitor
#
# 环境变量:
#   REMOTE_NODES:  空格分隔的远程节点 IP 列表
#                  (示例 "192.0.2.11 192.0.2.12"；192.0.2.x 为文档网段示例，
#                   使用时替换为实际节点地址)
#   SSH_USER:      远程 SSH 用户，默认 root
#   SSH_PASS:      SSH 密码，【无默认值】。留空 → 使用 SSH key 认证（优先推荐）；
#                  非空 → 使用 sshpass -e（密码经 SSHPASS 环境变量传递，
#                  绝不出现在命令行参数/进程列表里）
#   RDMA_MON_DEV:  RDMA 设备名，默认 mlx5_0
#   RDMA_MON_PORT: RDMA 端口号，默认 1
#
# 示例:
#   source /path/to/rdma_monitor.sh
#   REMOTE_NODES="192.0.2.11 192.0.2.12"
#   start_rdma_monitor
#   ... 运行测试 ...
#   stop_rdma_monitor "顺序写测试"
#
# 换算口径: perfquery PortXmitData/PortRcvData 差分 × 4 / 1048576 = MB/s
#   (计数器单位是 4 字节字)。峰值用于判"是否触顶"，均值用于判"稳态约束"，
#   两者必须分开报（见 SKILL.md 阶段 1g / 2a 网络轴）。
# ============================================================================
set -u

RDMA_MON_PID=""
RDMA_MON_LOG=""
RDMA_MON_DEV="${RDMA_MON_DEV:-mlx5_0}"
RDMA_MON_PORT="${RDMA_MON_PORT:-1}"
REMOTE_NODES="${REMOTE_NODES:-}"
SSH_USER="${SSH_USER:-root}"
SSH_PASS="${SSH_PASS:-}"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=3 -o LogLevel=ERROR"
_REMOTE_MONITOR_SCRIPT="/tmp/rdma_sample.sh"

declare -a _REMOTE_IPS=()
declare -a _REMOTE_LOCAL_LOGS=()

usage() {
    cat <<'EOF'
rdma_monitor.sh 是共享库，不能直接执行。用法:
  source /path/to/rdma_monitor.sh
  REMOTE_NODES="192.0.2.11 192.0.2.12"   # 可选；文档网段示例，替换为实际节点
  SSH_USER=root                          # 可选，默认 root
  SSH_PASS=...                           # 可选，无默认值；留空走 SSH key（推荐），
                                         # 非空则内部用 sshpass -e + SSHPASS 环境变量
  RDMA_MON_DEV=mlx5_0 RDMA_MON_PORT=1    # 可选
  start_rdma_monitor
  ... 运行测试 ...
  stop_rdma_monitor "测试标签"           # 输出每节点 TX/RX 峰值与均值 (MB/s)
EOF
}

# 直接执行（而非被 source）时打印用法退出
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    usage
    exit 2
fi

# SSH 封装：SSH_PASS 为空 → key 认证；非空 → sshpass -e（密码走 SSHPASS 环境变量）
_rdma_ssh() {
    local node="$1"
    shift
    if [ -n "${SSH_PASS}" ]; then
        SSHPASS="${SSH_PASS}" sshpass -e ssh ${SSH_OPTS} "${SSH_USER}@${node}" "$@"
    else
        ssh ${SSH_OPTS} "${SSH_USER}@${node}" "$@"
    fi
}

_rdma_scp() {
    local src="$1" node="$2" dst="$3"
    if [ -n "${SSH_PASS}" ]; then
        SSHPASS="${SSH_PASS}" sshpass -e scp ${SSH_OPTS} "${src}" "${SSH_USER}@${node}:${dst}"
    else
        scp ${SSH_OPTS} "${src}" "${SSH_USER}@${node}:${dst}"
    fi
}

_perfquery_sample() {
    local dev="$1" port="$2"
    local TX RX
    TX=$(perfquery -x -C "$dev" -P "$port" 2>/dev/null | awk -F: '/PortXmitData/ {gsub(/\./,"",$2); print $2}')
    RX=$(perfquery -x -C "$dev" -P "$port" 2>/dev/null | awk -F: '/PortRcvData/ {gsub(/\./,"",$2); print $2}')
    if [ -z "$TX" ] || [ -z "$RX" ]; then
        TX=$(perfquery -C "$dev" -P "$port" 2>/dev/null | awk -F: '/PortXmitData/ {gsub(/[. ]/,"",$2); print $2}')
        RX=$(perfquery -C "$dev" -P "$port" 2>/dev/null | awk -F: '/PortRcvData/ {gsub(/[. ]/,"",$2); print $2}')
    fi
    echo "${TX:-0} ${RX:-0}"
}

_generate_remote_script() {
    cat << 'REMOTE_EOF'
#!/bin/bash
DEV="$1"
PORT="$2"
LOGFILE="$3"
rm -f "$LOGFILE"
sample() {
    local TX RX
    TX=$(perfquery -x -C "$DEV" -P "$PORT" 2>/dev/null | awk -F: '/PortXmitData/ {gsub(/\./,"",$2); print $2}')
    RX=$(perfquery -x -C "$DEV" -P "$PORT" 2>/dev/null | awk -F: '/PortRcvData/ {gsub(/\./,"",$2); print $2}')
    if [ -z "$TX" ] || [ -z "$RX" ]; then
        TX=$(perfquery -C "$DEV" -P "$PORT" 2>/dev/null | awk -F: '/PortXmitData/ {gsub(/[. ]/,"",$2); print $2}')
        RX=$(perfquery -C "$DEV" -P "$PORT" 2>/dev/null | awk -F: '/PortRcvData/ {gsub(/[. ]/,"",$2); print $2}')
    fi
    echo "${TX:-0} ${RX:-0}"
}
while true; do
    vals1=$(sample); TX=${vals1%% *}; RX=${vals1##* }
    sleep 1
    vals2=$(sample); TX2=${vals2%% *}; RX2=${vals2##* }
    if [ "$TX2" -ge "$TX" ] 2>/dev/null && [ "$RX2" -ge "$RX" ] 2>/dev/null; then
        echo "$(( (TX2 - TX) * 4 / 1048576 )) $(( (RX2 - RX) * 4 / 1048576 ))" >> "$LOGFILE"
    else
        echo "0 0" >> "$LOGFILE"
    fi
done
REMOTE_EOF
}

start_rdma_monitor() {
    # --- 本地监控 ---
    if command -v perfquery &>/dev/null; then
        RDMA_MON_LOG=$(mktemp /tmp/rdma_mon_local_XXXXXX.log)
        (
            local dev="${RDMA_MON_DEV}" port="${RDMA_MON_PORT}" logfile="${RDMA_MON_LOG}"
            while true; do
                local vals1 vals2 TX RX TX2 RX2
                vals1=$(_perfquery_sample "$dev" "$port")
                TX=${vals1%% *}; RX=${vals1##* }
                sleep 1
                vals2=$(_perfquery_sample "$dev" "$port")
                TX2=${vals2%% *}; RX2=${vals2##* }
                if [ "$TX2" -ge "$TX" ] 2>/dev/null && [ "$RX2" -ge "$RX" ] 2>/dev/null; then
                    echo "$(( (TX2 - TX) * 4 / 1048576 )) $(( (RX2 - RX) * 4 / 1048576 ))" >> "$logfile"
                else
                    echo "0 0" >> "$logfile"
                fi
            done
        ) &
        RDMA_MON_PID=$!
        disown "$RDMA_MON_PID" 2>/dev/null
    fi

    # --- 远程节点监控 ---
    _REMOTE_IPS=()
    _REMOTE_LOCAL_LOGS=()

    if [ -z "${REMOTE_NODES}" ]; then
        return
    fi

    if [ -n "${SSH_PASS}" ] && ! command -v sshpass &>/dev/null; then
        echo "[RDMA][ERROR] 设置了 SSH_PASS 但本机无 sshpass；请安装 sshpass，或清空 SSH_PASS 改用 SSH key 认证" >&2
        return 1
    fi

    # 生成监控脚本到本地临时文件
    local local_script
    local_script=$(mktemp /tmp/rdma_sample_XXXXXX.sh)
    _generate_remote_script > "${local_script}"
    chmod +x "${local_script}"

    for node_ip in ${REMOTE_NODES}; do
        local remote_log="/tmp/rdma_mon_${node_ip//./_}.log"

        # 1. 先杀掉该节点上可能残留的监控进程
        _rdma_ssh "${node_ip}" \
            "pkill -f '${_REMOTE_MONITOR_SCRIPT}' 2>/dev/null; rm -f '${remote_log}'" 2>/dev/null || true

        # 2. 复制监控脚本到远程节点
        if ! _rdma_scp "${local_script}" "${node_ip}" "${_REMOTE_MONITOR_SCRIPT}" 2>/dev/null; then
            continue
        fi

        # 3. 在远程节点后台运行监控脚本
        if _rdma_ssh "${node_ip}" \
            "nohup bash ${_REMOTE_MONITOR_SCRIPT} ${RDMA_MON_DEV} ${RDMA_MON_PORT} ${remote_log} >/dev/null 2>&1 &" 2>/dev/null; then
            _REMOTE_IPS+=("${node_ip}")
        fi
    done

    rm -f "${local_script}"
    sleep 0.5
}

_parse_log() {
    local logfile="$1"
    if [ -f "$logfile" ] && [ -s "$logfile" ]; then
        awk '{
            if($1>max_tx) max_tx=$1;
            if($2>max_rx) max_rx=$2;
            sum_tx+=$1; sum_rx+=$2; n++;
        } END {
            if(n>0) printf "%d %d %.0f %.0f %d\n", max_tx, max_rx, sum_tx/n, sum_rx/n, n;
            else print "0 0 0 0 0";
        }' "$logfile"
    else
        echo "0 0 0 0 0"
    fi
}

stop_rdma_monitor() {
    local label="${1:-test}"

    # 停止本地监控
    if [ -n "$RDMA_MON_PID" ]; then
        kill "$RDMA_MON_PID" 2>/dev/null
        sleep 0.2
        RDMA_MON_PID=""
    fi

    # 停止远程监控并取回日志
    local -a remote_local_logs=()
    for i in "${!_REMOTE_IPS[@]}"; do
        local node_ip="${_REMOTE_IPS[$i]}"
        local remote_log="/tmp/rdma_mon_${node_ip//./_}.log"
        local local_copy
        local_copy=$(mktemp "/tmp/rdma_mon_${node_ip//./_}_XXXXXX.log")

        # 1. 杀掉远程监控进程
        _rdma_ssh "${node_ip}" \
            "pkill -f 'bash ${_REMOTE_MONITOR_SCRIPT}' 2>/dev/null" 2>/dev/null || true
        sleep 0.3

        # 2. 取回日志并清理
        _rdma_ssh "${node_ip}" \
            "cat '${remote_log}' 2>/dev/null; rm -f '${remote_log}' '${_REMOTE_MONITOR_SCRIPT}'" \
            > "${local_copy}" 2>/dev/null || true

        remote_local_logs+=("${local_copy}")
    done

    # --- 输出结果 ---
    echo -e "\033[33m[RDMA]\033[0m  ${label}:"

    # 本地
    if [ -n "$RDMA_MON_LOG" ]; then
        local stats
        stats=$(_parse_log "$RDMA_MON_LOG")
        local peak_tx peak_rx avg_tx avg_rx samples
        read -r peak_tx peak_rx avg_tx avg_rx samples <<< "$stats"
        if [ "$samples" -gt 0 ]; then
            local hostname_short
            hostname_short=$(hostname -s 2>/dev/null || echo "local")
            echo -e "\033[33m       \033[0m  [${hostname_short}] TX 峰值=${peak_tx} MB/s  均值=${avg_tx} MB/s | RX 峰值=${peak_rx} MB/s  均值=${avg_rx} MB/s (${samples}s)"
        fi
        rm -f "$RDMA_MON_LOG"
        RDMA_MON_LOG=""
    fi

    # 远程节点
    for i in "${!_REMOTE_IPS[@]}"; do
        local node_ip="${_REMOTE_IPS[$i]}"
        local local_copy="${remote_local_logs[$i]}"
        if [ -f "$local_copy" ]; then
            local stats
            stats=$(_parse_log "$local_copy")
            local peak_tx peak_rx avg_tx avg_rx samples
            read -r peak_tx peak_rx avg_tx avg_rx samples <<< "$stats"
            if [ "$samples" -gt 0 ]; then
                echo -e "\033[33m       \033[0m  [${node_ip}] TX 峰值=${peak_tx} MB/s  均值=${avg_tx} MB/s | RX 峰值=${peak_rx} MB/s  均值=${avg_rx} MB/s (${samples}s)"
            fi
            rm -f "$local_copy"
        fi
    done

    # 清理数组
    _REMOTE_IPS=()
    _REMOTE_LOCAL_LOGS=()
}
