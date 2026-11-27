#!/usr/bin/env bash

# OceanBase 部署前只读检查。

#

# 用法（所有变量可选，缺省值仅用于演示）：

# OB_BASE=/ob OB_SRC=/opt/src/oceanbase \

  # MYSQL_PORT=2882 RPC_PORT=2881 PROXY_PORT=2828 \

  # bash preflight_check.sh

#

# 输出 PASS/WARN/FAIL；存在 FAIL 时退出码非 0。本脚本只读，不修改系统。

set -u

OB_BASE="${OB_BASE:-/ob}"
OB_SRC="${OB_SRC:-}"
MYSQL_PORT="${MYSQL_PORT:-2882}"
RPC_PORT="${RPC_PORT:-2881}"
PROXY_PORT="${PROXY_PORT:-2828}"
MIN_MEM_GB="${MIN_MEM_GB:-64}"
MIN_CPU="${MIN_CPU:-16}"

FAILS=0
WARNS=0

pass() { printf 'PASS  %s\n' "$*"; }
warn() { WARNS=$((WARNS + 1)); printf 'WARN  %s\n' "$*"; }
fail() { FAILS=$((FAILS + 1)); printf 'FAIL  %s\n' "$*"; }
info() { printf 'INFO  %s\n' "$*"; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1
}

echo "== OceanBase preflight (read-only) =="
info "OB_BASE=${OB_BASE} MYSQL_PORT=${MYSQL_PORT} RPC_PORT=${RPC_PORT} PROXY_PORT=${PROXY_PORT}"

# 1. 架构与内核

ARCH="$(uname -m)"
info "arch=${ARCH} kernel=$(uname -r)"
case "${ARCH}" in
  aarch64|x86_64) pass "架构受支持：${ARCH}" ;;
  *) fail "不支持的架构：${ARCH}" ;;
esac

# 2. CPU

if need_cmd nproc; then
  CPUS="$(nproc)"
  if [ "${CPUS}" -lt "${MIN_CPU}" ]; then
    warn "CPU 核数 ${CPUS} < 建议 ${MIN_CPU}"
  else
    pass "CPU 核数 ${CPUS}"
  fi
else
  warn "缺少 nproc，跳过 CPU 检查"
fi

# 3. NUMA

if need_cmd numactl; then
  NODES="$(numactl -H 2>/dev/null | awk '/^available:/{print $2}')"
  info "NUMA nodes=${NODES:-unknown}"
  numactl -H 2>/dev/null | awk '/node [0-9]+ size/{print "INFO  " $0}'
  numactl -H 2>/dev/null | awk '/node distances:/{flag=1} flag{print "INFO  " $0}' | head -n 10
  if [ -n "${NODES}" ] && [ "${NODES}" -ge 2 ]; then
    pass "多 NUMA 拓扑，部署时必须规划绑核/绑内存"
  else
    warn "未检测到多 NUMA 或 numactl 输出异常"
  fi
else
  warn "缺少 numactl，无法检查 NUMA"
fi

# 4. 内存

if [ -r /proc/meminfo ]; then
  MEM_KB="$(awk '/MemTotal/{print $2}' /proc/meminfo)"
  MEM_GB=$((MEM_KB / 1024 / 1024))
  if [ "${MEM_GB}" -lt "${MIN_MEM_GB}" ]; then
    warn "内存 ${MEM_GB}G < 建议 ${MIN_MEM_GB}G"
  else
    pass "内存 ${MEM_GB}G"
  fi
else
  warn "无法读取 /proc/meminfo"
fi

# 5. 磁盘与目录

if [ -d "${OB_BASE}" ]; then
  pass "OB_BASE 存在：${OB_BASE}"
  df -h "${OB_BASE}" 2>/dev/null | sed 's/^/INFO  /'
  DATA_DEV="$(df -P "${OB_BASE}" 2>/dev/null | awk 'NR==2{print $1}')"
  info "OB_BASE 所在设备：${DATA_DEV:-unknown}"
else
  warn "OB_BASE 不存在（首次部署可忽略）：${OB_BASE}"
fi
for d in "${OB_BASE}/data" "${OB_BASE}/log"; do
  if [ -d "$d" ]; then
    pass "目录存在：$d"
  else
    info "目录不存在（部署时创建）：$d"
  fi
done

# 6. THP / swap / selinux

THP="$(cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null || echo unknown)"
case "${THP}" in
  *"[never]"*) pass "THP 已关闭" ;;
  unknown) warn "无法读取 THP 状态" ;;
  *) warn "THP 未关闭：${THP}（OceanBase 建议 never）" ;;
esac

if [ -r /proc/swaps ]; then
  SWAP_LINES="$(awk 'NR>1' /proc/swaps | wc -l)"
  if [ "${SWAP_LINES}" -eq 0 ]; then
    pass "swap 已关闭"
  else
    warn "swap 未关闭（OceanBase 建议 swapoff）"
  fi
fi

if need_cmd getenforce; then
  SE="$(getenforce 2>/dev/null || echo unknown)"
  case "${SE}" in
    Disabled) pass "SELinux 已关闭" ;;
    unknown) warn "无法读取 SELinux 状态" ;;
    *) warn "SELinux=${SE}（建议 disabled）" ;;
  esac
fi

# 7. 内核参数

check_sysctl() {
  local key="$1" want="$2" cur
  cur="$(sysctl -n "${key}" 2>/dev/null || echo "")"
  if [ -z "${cur}" ]; then
    warn "无法读取 ${key}"
    return
  fi
  if [ "${cur}" = "${want}" ]; then
    pass "${key}=${cur}"
  else
    warn "${key}=${cur}（建议 ${want}）"
  fi
}
check_sysctl vm.swappiness 0
check_sysctl kernel.numa_balancing 0
check_sysctl vm.zone_reclaim_mode 0
check_sysctl vm.max_map_count 655360
check_sysctl fs.aio-max-nr 1048576

if [ -r /proc/sys/net/netfilter/nf_conntrack_count ]; then
  warn "nf_conntrack 已加载（OB 调优场景常卸载，需确认）"
else
  pass "nf_conntrack 未加载"
fi
# 8. ulimit

NOFILE="$(ulimit -n 2>/dev/null || echo 0)"
case "${NOFILE}" in
  unlimited) pass "nofile=unlimited（优于建议下限 655350）" ;;
  '')
    warn "nofile 读取为空（建议 >=655350）"
    ;;
  *)
    if [ "${NOFILE}" -ge 655350 ] 2>/dev/null; then
      pass "nofile=${NOFILE}"
    else
      warn "nofile=${NOFILE}（建议 >=655350）"
    fi
    ;;
esac

# 9. 时间同步

if need_cmd chronyc; then
  if chronyc tracking >/dev/null 2>&1; then
    pass "chrony 可用"
    chronyc tracking 2>/dev/null | sed -n '1,4p' | sed 's/^/INFO  /'
  else
    warn "chronyc 存在但无法连接（三副本必须校时）"
  fi
else
  warn "未安装 chrony（三副本必须校时）"
fi

# 10. 端口

# 端口范围校验：65536 等能过后续 ss 探测但实例无法绑定，先按用法错误拒绝
for p in "${MYSQL_PORT}" "${RPC_PORT}" "${PROXY_PORT}"; do
  case "$p" in ''|*[!0-9]*) echo "FAIL  端口须为数字: $p" >&2; exit 2 ;; esac
  if [ "$p" -lt 1 ] || [ "$p" -gt 65535 ]; then
    echo "FAIL  端口 $p 超出合法范围 1-65535（实例无法绑定，探测无意义）" >&2
    exit 2
  fi
done
# 同机部署时 MySQL/RPC/Proxy 三个服务端口在同一监听地址上必须互异；
# 否则各自检查都可能空闲，但启动时两个监听无法同时绑定（用法错误，exit 2）
if [ "${MYSQL_PORT}" = "${RPC_PORT}" ] || \
   [ "${MYSQL_PORT}" = "${PROXY_PORT}" ] || \
   [ "${RPC_PORT}" = "${PROXY_PORT}" ]; then
  echo "FAIL  同机部署要求 MySQL/RPC/Proxy 端口互异（当前 ${MYSQL_PORT}/${RPC_PORT}/${PROXY_PORT}）" >&2
  exit 2
fi
if need_cmd ss; then
  # 先看 ss 是否成功；读取失败时的空输出不能解释为端口空闲
  if SS_LINES="$(ss -lnt 2>&1)"; then
    for p in "${MYSQL_PORT}" "${RPC_PORT}" "${PROXY_PORT}"; do
      if printf '%s\n' "${SS_LINES}" | awk '{print $4}' | grep -qE "[:.]${p}$"; then
        warn "端口 ${p} 已被占用（确认是否残留进程）"
      else
        pass "端口 ${p} 空闲"
      fi
    done
  else
    warn "ss 读取监听列表失败（rc=$?），无法判断端口占用，不判定为空闲：${SS_LINES}"
  fi
else
  warn "缺少 ss，无法探测端口占用（不判定为空闲）"
fi

# 11. 残留进程

if pgrep -x observer >/dev/null 2>&1; then
  warn "存在 observer 进程：$(pgrep -x observer | tr '\n' ' ')"
else
  pass "无残留 observer 进程"
fi
if pgrep -x obproxy >/dev/null 2>&1; then
  warn "存在 obproxy 进程：$(pgrep -x obproxy | tr '\n' ' ')"
else
  pass "无残留 obproxy 进程"
fi

# 12. 构建依赖（可选）

if [ -n "${OB_SRC}" ]; then
  if [ -d "${OB_SRC}" ]; then
    pass "OB_SRC 存在：${OB_SRC}"
    if [ -x "${OB_SRC}/deps/3rd/usr/local/oceanbase/devtools/bin/cmake" ]; then
      pass "devtools cmake 已安装"
    else
      warn "deps/3rd devtools 未初始化（需 ./build.sh init）"
    fi
    if [ -x "${OB_SRC}/deps/3rd/usr/local/oceanbase/devtools/bin/clang++" ]; then
      pass "devtools clang++ 已安装"
    else
      info "devtools clang++ 未找到（以实际工具链为准）"
    fi
  else
    fail "OB_SRC 不存在：${OB_SRC}"
  fi
else
  info "未提供 OB_SRC，跳过构建依赖检查"
fi

echo "== summary: FAIL=${FAILS} WARN=${WARNS} =="
if [ "${FAILS}" -gt 0 ]; then
  exit 1
fi
exit 0
