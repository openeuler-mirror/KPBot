#!/usr/bin/env bash
#===============================================================
# preflight_check.sh — 3FS 部署前置检查（只读，无副作用）
#
# 用法:   preflight_check.sh [--help]
#         在每个参与部署的节点上执行；所有检查均为只读查询，
#         不修改任何配置、不安装任何软件、不启停任何服务。
#
# 检查项:
#   1. hostname 设置与期望值
#   2. /etc/hosts 角色名解析（不得解析到回环地址）
#   3. （可选）到对端 RDMA IP 的 ping 连通性
#   4. RDMA link 状态与网卡名（filter_list 依据；异构集群逐节点确认）
#   5. 时间同步偏差（chrony / timedatectl）
#   6. 必需端口空闲（部署前；已运行节点可 SKIP_PORTS=1）
#   7. 防火墙 / SELinux 状态提示（仅 WARN，处置见安装手册 §3.1）
#   8. 依赖命令与运行库存在性（按 NODE_ROLE 分级）
#
# 环境变量可覆盖:
#   EXPECT_HOSTNAME  本机期望 hostname（默认空: 仅检查非空且非 localhost）
#   CLUSTER_HOSTS    /etc/hosts 应解析的角色名列表（默认 "meta storage1 storage2"）
#   RDMA_IFACE       本机 RDMA 网卡名（默认空: 自动探测并提示）
#   PING_TARGETS     需连通性测试的对端 RDMA IP 列表（默认空: 跳过）
#   CHECK_PORTS      应空闲的端口列表（默认 "4500 8000 8001 9000 9001 9123 10000"，
#                    即 FDB/MGMTD/META/Core/Core/ClickHouse/MonitorCollector）
#   SKIP_PORTS       置 1 跳过端口空闲检查（用于已运行节点巡检）
#   NTP_MAX_OFFSET   时间偏差上限秒数（默认 1.0）
#   NODE_ROLE        节点角色 meta|storage|fuse（默认空: 角色相关项按 WARN）
#
# 输出: 逐项 [PASS]/[WARN]/[FAIL] + 汇总；存在 FAIL 时退出码非零。
#===============================================================
set -u

usage() {
    cat <<'EOF'
用法: preflight_check.sh [--help]

3FS 部署前置检查（只读、无副作用），在每个参与部署的节点上执行。
输出逐项 PASS/WARN/FAIL 与汇总；存在 FAIL 时退出码非零。

环境变量（均可选）:
  EXPECT_HOSTNAME   本机期望 hostname
  CLUSTER_HOSTS     /etc/hosts 应解析的角色名（默认 "meta storage1 storage2"）
  RDMA_IFACE        本机 RDMA 网卡名（缺省时自动探测并提示）
  PING_TARGETS      需 ping 的对端 RDMA IP 列表
  CHECK_PORTS       应空闲端口（默认 "4500 8000 8001 9000 9001 9123 10000"）
  SKIP_PORTS=1      跳过端口空闲检查（已运行节点巡检）
  NTP_MAX_OFFSET    时间偏差上限秒（默认 1.0）
  NODE_ROLE         meta|storage|fuse（角色相关依赖的判级依据）

示例:
  EXPECT_HOSTNAME=storage1 RDMA_IFACE=enp43s0f0np0 NODE_ROLE=storage \
    bash preflight_check.sh
  SKIP_PORTS=1 bash preflight_check.sh        # 已运行集群巡检
EOF
}

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
    "")        ;;
    *)         echo "ERROR: 未知参数: $1" >&2; usage >&2; exit 2 ;;
esac

EXPECT_HOSTNAME="${EXPECT_HOSTNAME:-}"
CLUSTER_HOSTS="${CLUSTER_HOSTS:-meta storage1 storage2}"
RDMA_IFACE="${RDMA_IFACE:-}"
PING_TARGETS="${PING_TARGETS:-}"
CHECK_PORTS="${CHECK_PORTS:-4500 8000 8001 9000 9001 9123 10000}"
SKIP_PORTS="${SKIP_PORTS:-0}"
NTP_MAX_OFFSET="${NTP_MAX_OFFSET:-1.0}"
NODE_ROLE="${NODE_ROLE:-}"

if [ -t 1 ]; then
    C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'; C_RST=$'\033[0m'
else
    C_RED=""; C_GRN=""; C_YEL=""; C_RST=""
fi
PASS_CNT=0; WARN_CNT=0; FAIL_CNT=0
pass() { PASS_CNT=$((PASS_CNT+1)); echo "${C_GRN}[PASS]${C_RST} $*"; }
warn() { WARN_CNT=$((WARN_CNT+1)); echo "${C_YEL}[WARN]${C_RST} $*"; }
fail() { FAIL_CNT=$((FAIL_CNT+1)); echo "${C_RED}[FAIL]${C_RST} $*"; }
info() { echo "[INFO] $*"; }
sect() { echo; echo "===== $* ====="; }

# ---------- 1. hostname ----------
sect "1. hostname"
HN=$(hostname 2>/dev/null || echo "")
if [ -z "$HN" ] || [ "$HN" = "localhost" ] || [ "$HN" = "(none)" ]; then
    fail "hostname 未设置或为 '$HN'（应 hostnamectl --static set-hostname <角色名>）"
else
    pass "hostname: $HN"
fi
if [ -n "$EXPECT_HOSTNAME" ] && [ "$HN" != "$EXPECT_HOSTNAME" ]; then
    fail "hostname '$HN' 与期望 '$EXPECT_HOSTNAME' 不一致"
fi

# ---------- 2. /etc/hosts 解析 ----------
sect "2. /etc/hosts 角色名解析"
for h in $CLUSTER_HOSTS; do
    hip=$(getent hosts "$h" 2>/dev/null | awk '{print $1; exit}')
    if [ -z "$hip" ]; then
        fail "角色名 '$h' 无法解析（/etc/hosts 应含 <RDMA网IP> $h）"
    elif echo "$hip" | grep -q '^127\.'; then
        fail "'$h' 解析到回环地址 $hip（角色名应解析到 RDMA 网 IP）"
    else
        pass "$h -> $hip"
    fi
done

# ---------- 3. 对端连通性（可选） ----------
if [ -n "$PING_TARGETS" ]; then
    sect "3. 对端 RDMA IP 连通性"
    for t in $PING_TARGETS; do
        if ping -c 1 -W 2 "$t" >/dev/null 2>&1; then
            pass "ping $t 可达"
        else
            fail "ping $t 不可达"
        fi
    done
fi

# ---------- 4. RDMA ----------
sect "4. RDMA link 与网卡名"
if ! command -v rdma >/dev/null 2>&1; then
    fail "缺少 rdma 命令（rdma-core/iproute 未安装？），无法确认 RDMA 链路状态"
else
    rdma_out=$(rdma link 2>/dev/null)
    if [ -z "$rdma_out" ]; then
        fail "rdma link 无输出（RDMA 设备/驱动未就绪？）"
    else
        echo "$rdma_out" | sed 's/^/    /'
        if echo "$rdma_out" | grep -q "state ACTIVE"; then
            pass "rdma link 存在 ACTIVE 端口"
        else
            fail "rdma link 无 ACTIVE 端口"
        fi
        if [ -n "$RDMA_IFACE" ]; then
            if echo "$rdma_out" | grep -qE "netdev ${RDMA_IFACE}([^A-Za-z0-9_]|$)"; then
                pass "RDMA netdev 与 RDMA_IFACE=${RDMA_IFACE} 匹配"
            else
                fail "rdma link 的 netdev 均不是 RDMA_IFACE=${RDMA_IFACE}（异构集群各节点网卡名可能不同，filter_list 必须填本机实际网卡名）"
            fi
            if ip addr show "$RDMA_IFACE" >/dev/null 2>&1; then
                nip=$(ip -4 addr show "$RDMA_IFACE" 2>/dev/null | awk '/inet /{print $2; exit}')
                if [ -n "$nip" ]; then
                    pass "网卡 ${RDMA_IFACE} IPv4: $nip"
                else
                    warn "网卡 ${RDMA_IFACE} 存在但无 IPv4 地址"
                fi
            else
                fail "网卡 ${RDMA_IFACE} 不存在（ip link show 核对）"
            fi
        else
            nds=$(echo "$rdma_out" | awk '{for(i=1;i<=NF;i++) if($i=="netdev") print $(i+1)}' | tr '\n' ' ')
            warn "未设置 RDMA_IFACE；rdma link 探测到 netdev: ${nds}（配置 filter_list 前请确认并传入）"
        fi
    fi
fi
if command -v ibdev2netdev >/dev/null 2>&1; then
    info "ibdev2netdev 设备映射:"
    ibdev2netdev 2>/dev/null | sed 's/^/    /'
fi

# ---------- 5. 时间同步 ----------
sect "5. 时间同步"
if command -v chronyc >/dev/null 2>&1; then
    if systemctl is-active chronyd >/dev/null 2>&1; then
        pass "chronyd 正在运行"
    else
        warn "chronyd 未运行（systemctl enable --now chronyd）"
    fi
    off=$(chronyc tracking 2>/dev/null | awk '/^System time/ {print $4; exit}')
    if [ -n "$off" ]; then
        if awk -v o="$off" -v t="$NTP_MAX_OFFSET" 'BEGIN{exit !(o+0 < t+0)}'; then
            pass "时间偏差 ${off}s < ${NTP_MAX_OFFSET}s"
        else
            fail "时间偏差 ${off}s >= ${NTP_MAX_OFFSET}s（3FS 集群要求各节点时间同步）"
        fi
    else
        warn "chronyc tracking 未取到偏差值（chronyd 未运行或无 NTP 源？）"
    fi
elif command -v timedatectl >/dev/null 2>&1; then
    ntpsync=$(timedatectl show -p NTPSynchronized --value 2>/dev/null || echo "")
    if [ "$ntpsync" = "yes" ]; then
        pass "系统时间已同步 (NTPSynchronized=yes)"
    elif [ "$ntpsync" = "no" ]; then
        fail "系统时间未同步 (NTPSynchronized=no)"
    else
        warn "无法判定时间同步状态"
    fi
else
    warn "缺少 chronyc/timedatectl，无法核验时间同步（部署前请人工确认）"
fi

# ---------- 6. 端口空闲 ----------
sect "6. 必需端口空闲检查"
if [ "$SKIP_PORTS" = "1" ]; then
    info "SKIP_PORTS=1，跳过（已运行节点这些端口应处于监听状态）"
elif ! command -v ss >/dev/null 2>&1; then
    warn "缺少 ss 命令，无法检查端口占用"
else
    listen=$(ss -tuln 2>/dev/null | awk 'NR>1 {print $5}')
    for p in $CHECK_PORTS; do
        if echo "$listen" | grep -qE ":${p}$"; then
            fail "端口 $p 已被占用（部署前应空闲；已运行节点巡检请设 SKIP_PORTS=1）"
        else
            pass "端口 $p 空闲"
        fi
    done
fi

# ---------- 7. 防火墙 / SELinux ----------
sect "7. 防火墙 / SELinux"
if command -v systemctl >/dev/null 2>&1 && systemctl is-active firewalld >/dev/null 2>&1; then
    warn "firewalld 运行中：测试环境可按手册停用；生产环境应保留并精确放行端口 ${CHECK_PORTS}"
else
    pass "firewalld 未运行"
fi
if command -v getenforce >/dev/null 2>&1; then
    se=$(getenforce 2>/dev/null || echo Unknown)
    if [ "$se" = "Enforcing" ]; then
        warn "SELinux Enforcing：测试环境可按手册改 permissive；生产环境建议保持并用 audit2allow 定制策略"
    else
        pass "SELinux: $se"
    fi
fi

# ---------- 8. 依赖命令与运行库 ----------
sect "8. 依赖命令与运行库"
for c in rsync lsblk awk grep; do
    if command -v "$c" >/dev/null 2>&1; then pass "命令 $c 存在"; else warn "命令 $c 缺失"; fi
done

check_cmd_role() { # <cmd> <需要它的角色, 空格分隔> <提示>
    local c=$1 roles=$2 hint=$3
    if command -v "$c" >/dev/null 2>&1; then
        pass "命令 $c 存在"
    elif [ -n "$NODE_ROLE" ] && echo " $roles " | grep -q " $NODE_ROLE "; then
        fail "命令 $c 缺失（$hint）"
    else
        warn "命令 $c 缺失（$hint；当前 NODE_ROLE='${NODE_ROLE:-未设置}' 按通用告警）"
    fi
}
check_cmd_role mkfs.xfs   "storage"     "storage 节点格式化 XFS 数据盘必需"
check_cmd_role fusermount3 "fuse meta"  "FUSE 挂载/卸载必需（fuse3 包）"
check_cmd_role python3    "meta"        "meta 节点生成 chain table（data_placement）必需"
check_cmd_role pip3       "meta"        "meta 节点安装 data_placement 依赖必需"

LIB_CORE="libibverbs libjemalloc"
LIB_EXT="libaio libnuma libglog libgflags libevent liblz4 libuv libdouble-conversion libunwind"
if command -v ldconfig >/dev/null 2>&1; then
    ldcache=$(ldconfig -p 2>/dev/null)
    for l in $LIB_CORE; do
        if echo "$ldcache" | grep -q "$l"; then pass "运行库 $l 存在"; else fail "运行库 $l 缺失（3FS 二进制运行必需，yum 安装对应包）"; fi
    done
    for l in $LIB_EXT; do
        if echo "$ldcache" | grep -q "$l"; then pass "运行库 $l 存在"; else warn "运行库 $l 未检出（结合节点角色与部署后 ldd 结果判定）"; fi
    done
    if echo "$ldcache" | grep -q libfuse3; then
        pass "运行库 libfuse3 存在"
    elif [ "$NODE_ROLE" = "fuse" ] || [ "$NODE_ROLE" = "meta" ]; then
        fail "运行库 libfuse3 缺失（FUSE 客户端/meta 节点必需，安装 fuse3）"
    else
        warn "运行库 libfuse3 未检出（FUSE 客户端必需）"
    fi
else
    warn "缺少 ldconfig，无法检查运行库"
fi

# ---------- 汇总 ----------
echo
echo "===== 汇总: PASS=${PASS_CNT} WARN=${WARN_CNT} FAIL=${FAIL_CNT} ====="
if [ "$FAIL_CNT" -gt 0 ]; then
    echo "${C_RED}存在 FAIL 项，先修复再部署${C_RST}"
    exit 1
fi
if [ "$WARN_CNT" -gt 0 ]; then
    echo "${C_YEL}无 FAIL；WARN 项请结合节点角色人工确认${C_RST}"
fi
echo "${C_GRN}关键检查全部通过${C_RST}"
exit 0
