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
#   SSH_PASS:      SSH 密码，【无默认值】。留空 → 使用 SSH key 认证（优先推荐，
#                  此时强制 PreferredAuthentications=publickey，不降级口令）；
#                  非空 → 使用 sshpass -e（密码经 SSHPASS 环境变量传递，
#                  绝不出现在命令行参数/进程列表里），并打印改用 key 的告警
#   SSH_OPTS:      ssh/scp 选项，默认 "-o StrictHostKeyChecking=accept-new
#                  -o ConnectTimeout=3 -o LogLevel=ERROR"。accept-new = 首次
#                  连接信任并写入 known_hosts、主机密钥变更则拒连；不要改为
#                  no（内网被劫持时可冒充节点截获口令/伪造采样数据）。
#                  校验失败必须中止排查，不得降级绕过
#   KNOWN_HOSTS:   可选；预置 known_hosts 文件路径（经带外渠道核对指纹后用
#                  ssh-keyscan 生成），设置后追加 -o UserKnownHostsFile=<path>
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
SSH_OPTS="${SSH_OPTS:--o StrictHostKeyChecking=accept-new -o ConnectTimeout=3 -o LogLevel=ERROR}"
KNOWN_HOSTS="${KNOWN_HOSTS:-}"
[ -n "${KNOWN_HOSTS}" ] && SSH_OPTS="${SSH_OPTS} -o UserKnownHostsFile=${KNOWN_HOSTS}"
# 远端采样脚本名前缀：实际路径由远端 mktemp 生成唯一名（0600/0700），
# 不再使用固定 /tmp/rdma_sample.sh（固定路径可被同机其他用户预置/符号链接劫持）
_REMOTE_MONITOR_GLOB="/tmp/rdma_sample."

declare -a _REMOTE_IPS=()
declare -a _REMOTE_SCRIPTS=()
declare -a _REMOTE_LOCAL_LOGS=()

usage() {
    cat <<'EOF'
rdma_monitor.sh 是共享库，不能直接执行。用法:
  source /path/to/rdma_monitor.sh
  REMOTE_NODES="192.0.2.11 192.0.2.12"   # 可选；文档网段示例，替换为实际节点
  SSH_USER=root                          # 可选，默认 root
  SSH_PASS=...                           # 可选，无默认值；留空走 SSH key（推荐，
                                         # 强制 publickey 认证），非空则 sshpass -e
  SSH_OPTS=...                           # 可选，默认 StrictHostKeyChecking=accept-new
  KNOWN_HOSTS=/path/to/known_hosts       # 可选，预置主机密钥文件（带外核对指纹）
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

# SSH 封装：SSH_PASS 为空 → key 认证（强制 publickey，不降级口令提示）；
# 非空 → sshpass -e（密码走 SSHPASS 环境变量，不进 argv）。
# 两者共用同一组 SSH_OPTS（含主机密钥校验；KNOWN_HOSTS 设置时固定 UserKnownHostsFile）
_rdma_ssh() {
    local node="$1"
    shift
    if [ -n "${SSH_PASS}" ]; then
        SSHPASS="${SSH_PASS}" sshpass -e ssh ${SSH_OPTS} "${SSH_USER}@${node}" "$@"
    else
        ssh ${SSH_OPTS} -o PreferredAuthentications=publickey "${SSH_USER}@${node}" "$@"
    fi
}

_rdma_scp() {
    local src="$1" node="$2" dst="$3"
    if [ -n "${SSH_PASS}" ]; then
        SSHPASS="${SSH_PASS}" sshpass -e scp ${SSH_OPTS} "${src}" "${SSH_USER}@${node}:${dst}"
    else
        scp ${SSH_OPTS} -o PreferredAuthentications=publickey "${src}" "${SSH_USER}@${node}:${dst}"
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
    _REMOTE_SCRIPTS=()
    _REMOTE_LOCAL_LOGS=()

    if [ -z "${REMOTE_NODES}" ]; then
        return
    fi

    if [ -n "${SSH_PASS}" ] && ! command -v sshpass &>/dev/null; then
        echo "[RDMA][ERROR] 设置了 SSH_PASS 但本机无 sshpass；请安装 sshpass，或清空 SSH_PASS 改用 SSH key 认证" >&2
        return 1
    fi
    if [ -n "${SSH_PASS}" ]; then
        echo "[RDMA][WARN] 正在使用口令认证（sshpass -e）；建议优先配置 SSH key 并在集群侧关闭口令登录" >&2
    fi

    # 生成监控脚本到本地临时文件
    local local_script
    local_script=$(mktemp /tmp/rdma_sample_XXXXXX.sh)
    _generate_remote_script > "${local_script}"
    chmod +x "${local_script}"

    local node_ip remote_script remote_log
    for node_ip in ${REMOTE_NODES}; do
        # 1. 清掉此前异常退出可能残留的采样进程（历史固定名与 mktemp 唯一名同前缀）
        _rdma_ssh "${node_ip}" \
            "pkill -f '${_REMOTE_MONITOR_GLOB}' 2>/dev/null; true" 2>/dev/null || true

        # 2. 远端 mktemp 生成唯一脚本路径（umask 077，防 /tmp 预置/符号链接劫持）
        remote_script=$(_rdma_ssh "${node_ip}" "umask 077; mktemp /tmp/rdma_sample.XXXXXX.sh" 2>/dev/null | tr -d '[:space:]')
        if [ -z "${remote_script}" ]; then
            echo "[RDMA][WARN] ${node_ip}: 不可达或主机密钥校验失败，该节点未纳入监控；" >&2
            echo "[RDMA][WARN]   网络轴数据缺失必须写进归因结论。密钥变更时不要降级" >&2
            echo "[RDMA][WARN]   StrictHostKeyChecking：先查明原因（节点重装?伪造?），再 ssh-keygen -R 清旧键" >&2
            continue
        fi
        remote_log="${remote_script%.sh}.log"

        # 3. 复制监控脚本到远程节点并后台运行
        if ! _rdma_scp "${local_script}" "${node_ip}" "${remote_script}" 2>/dev/null; then
            echo "[RDMA][WARN] ${node_ip}: scp 失败，该节点未纳入监控" >&2
            continue
        fi
        if _rdma_ssh "${node_ip}" \
            "nohup bash ${remote_script} ${RDMA_MON_DEV} ${RDMA_MON_PORT} ${remote_log} >/dev/null 2>&1 &" 2>/dev/null; then
            _REMOTE_IPS+=("${node_ip}")
            _REMOTE_SCRIPTS+=("${remote_script}")
        else
            echo "[RDMA][WARN] ${node_ip}: 采样进程启动失败，该节点未纳入监控" >&2
        fi
    done

    rm -f "${local_script}"
    if [ "${#_REMOTE_IPS[@]}" -eq 0 ]; then
        echo "[RDMA][ERROR] 所有远程节点均不可用，远程监控未启动（不要静默继续网络轴归因）" >&2
        return 1
    fi
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
        local remote_script="${_REMOTE_SCRIPTS[$i]}"
        local remote_log="${remote_script%.sh}.log"
        local local_copy
        local_copy=$(mktemp "/tmp/rdma_mon_${node_ip//./_}_XXXXXX.log")

        # 1. 杀掉远程监控进程（按本次运行的唯一脚本路径精确匹配，不误杀他人进程）
        _rdma_ssh "${node_ip}" \
            "pkill -f 'bash ${remote_script}' 2>/dev/null" 2>/dev/null || true
        sleep 0.3

        # 2. 取回日志并清理本次运行创建的远端文件
        _rdma_ssh "${node_ip}" \
            "cat '${remote_log}' 2>/dev/null; rm -f '${remote_log}' '${remote_script}'" \
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
    _REMOTE_SCRIPTS=()
    _REMOTE_LOCAL_LOGS=()
}
