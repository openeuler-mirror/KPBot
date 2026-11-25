#!/usr/bin/env bash
# preflight_check.sh — NebulaGraph 部署前置检查（只读，无副作用）
#
# 用法: preflight_check.sh [--help]
#       在每台参与部署的节点上执行；只查询，不修改配置、不安装、不启停服务。
#
# 环境变量（均可选）:
#   NEBULA_HOME       源码/运行根目录（用于检查时区文件、二进制、cluster.id）
#   THIRD_PARTY       third-party 安装前缀（检查 .a 数量）
#   DATA_ROOT         meta/storage 数据根（检查磁盘空间与目录）
#   EXPECT_ARCH       期望架构，默认 aarch64
#   MIN_CPUS          最少逻辑 CPU 数，默认 8
#   MIN_MEM_GB        最少内存 GB，默认 32
#   MIN_DISK_GB       数据盘最少可用 GB，默认 100
#   CHECK_PORTS       应空闲的端口列表，默认覆盖参考拓扑 4 graphd/4 storaged（含 HTTP）
#   SKIP_PORTS=1      跳过端口检查（已运行集群巡检）
#   SKIP_PROC=1       跳过残留进程检查
#   NODE_ROLE         all|meta|graph|storage（仅影响提示，不改变检查项）
#
# 输出: 逐项 [PASS]/[WARN]/[FAIL] + 汇总；存在 FAIL 时退出码非零。
set -u

usage() {
  cat <<'EOF'
用法: preflight_check.sh [--help]

NebulaGraph 部署前置检查（只读、无副作用）。
输出逐项 PASS/WARN/FAIL 与汇总；存在 FAIL 时退出码非零。

可选环境变量:
  NEBULA_HOME    源码/运行根目录
  THIRD_PARTY    third-party 安装前缀
  DATA_ROOT      数据根目录
  EXPECT_ARCH    默认 aarch64
  MIN_CPUS       默认 8
  MIN_MEM_GB     默认 32
  MIN_DISK_GB    默认 100
  CHECK_PORTS    默认 "9559 9670 9671 9672 9673 9778 9779 9780 9781 9782 9783 9784 9785 9786 9787 9788 9789 19559 19669 19671 19672 19673 19779 19782 19785 19788"
  SKIP_PORTS=1   跳过端口检查（已运行集群）
  SKIP_PROC=1    跳过残留进程检查
  NODE_ROLE      all|meta|graph|storage（仅提示）

示例:
  NEBULA_HOME=/opt/nebula/nebula-3.8.0 THIRD_PARTY=/opt/vesoft/third-party/3.3 \
    DATA_ROOT=/data/nebula bash preflight_check.sh
  SKIP_PORTS=1 SKIP_PROC=1 bash preflight_check.sh    # 已运行集群巡检
EOF
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  "") ;;
  *) echo "ERROR: 未知参数: $1" >&2; usage >&2; exit 2 ;;
esac

NEBULA_HOME="${NEBULA_HOME:-}"
THIRD_PARTY="${THIRD_PARTY:-}"
DATA_ROOT="${DATA_ROOT:-}"
EXPECT_ARCH="${EXPECT_ARCH:-aarch64}"
MIN_CPUS="${MIN_CPUS:-8}"
MIN_MEM_GB="${MIN_MEM_GB:-32}"
MIN_DISK_GB="${MIN_DISK_GB:-100}"
CHECK_PORTS="${CHECK_PORTS:-9559 9670 9671 9672 9673 9778 9779 9780 9781 9782 9783 9784 9785 9786 9787 9788 9789 19559 19669 19671 19672 19673 19779 19782 19785 19788}"
SKIP_PORTS="${SKIP_PORTS:-0}"
SKIP_PROC="${SKIP_PROC:-0}"
NODE_ROLE="${NODE_ROLE:-all}"

if [ -t 1 ]; then
  C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'; C_RST=$'\033[0m'
else
  C_RED=""; C_GRN=""; C_YEL=""; C_RST=""
fi
PASS_CNT=0; WARN_CNT=0; FAIL_CNT=0
pass() { PASS_CNT=$((PASS_CNT+1)); echo "${C_GRN}[PASS]${C_RST} $*"; }
warn() { WARN_CNT=$((WARN_CNT+1)); echo "${C_YEL}[WARN]${C_RST} $*"; }
fail() { FAIL_CNT=$((FAIL_CNT+1)); echo "${C_RED}[FAIL]${C_RST} $*"; }

have() { command -v "$1" >/dev/null 2>&1; }

echo "NebulaGraph preflight check — node role: ${NODE_ROLE}"
echo "host=$(hostname 2>/dev/null || echo unknown) date=$(date '+%F %T')"
echo "------------------------------------------------------------"

# 1. 架构
ARCH=$(uname -m 2>/dev/null || echo unknown)
if [ "$ARCH" = "$EXPECT_ARCH" ]; then
  pass "架构 ${ARCH} 与期望一致"
else
  warn "架构 ${ARCH} 与期望 ${EXPECT_ARCH} 不一致（x86 需使用对应 third-party）"
fi

# 2. 逻辑 CPU
if have nproc; then
  CPUS=$(nproc)
  if [ "$CPUS" -ge "$MIN_CPUS" ]; then
    pass "逻辑 CPU ${CPUS} ≥ ${MIN_CPUS}"
  else
    fail "逻辑 CPU ${CPUS} < ${MIN_CPUS}"
  fi
else
  warn "nproc 不可用，跳过 CPU 检查"
fi

# 3. NUMA
if have numactl; then
  NUMA_NODES=$(numactl -H 2>/dev/null | awk '/^available: [0-9]+ nodes/{print $2}')
  pass "numactl 可用，NUMA nodes=${NUMA_NODES:-unknown}"
  if [ "${NUMA_NODES:-0}" -gt 1 ] 2>/dev/null; then
    warn "多 NUMA：启动时必须用 numactl -C/-m 绑定，避免跨节点访存"
  fi
else
  fail "numactl 未安装（dnf install -y numactl numactl-devel）"
fi

# 4. 内存
if have free; then
  MEM_GB=$(free -g 2>/dev/null | awk '/^Mem:/{print $2}')
  if [ -n "${MEM_GB:-}" ] && [ "$MEM_GB" -ge "$MIN_MEM_GB" ]; then
    pass "内存 ${MEM_GB}GB ≥ ${MIN_MEM_GB}GB"
  else
    warn "内存 ${MEM_GB:-unknown}GB < ${MIN_MEM_GB}GB（按实际数据集评估）"
  fi
  SWAP_GB=$(free -g 2>/dev/null | awk '/^Swap:/{print $2}')
  if [ "${SWAP_GB:-0}" -gt 0 ] 2>/dev/null; then
    warn "Swap 已启用 ${SWAP_GB}GB：数据库场景建议关闭或限制，避免抖动"
  else
    pass "Swap 未启用"
  fi
else
  warn "free 不可用，跳过内存检查"
fi

# 5. 数据盘
if [ -n "$DATA_ROOT" ]; then
  if [ -d "$DATA_ROOT" ]; then
    AVAIL_GB=$(df -BG --output=avail "$DATA_ROOT" 2>/dev/null | tail -1 | tr -dc '0-9')
    if [ -n "${AVAIL_GB:-}" ] && [ "$AVAIL_GB" -ge "$MIN_DISK_GB" ]; then
      pass "数据目录 ${DATA_ROOT} 可用 ${AVAIL_GB}GB ≥ ${MIN_DISK_GB}GB"
    else
      fail "数据目录 ${DATA_ROOT} 可用 ${AVAIL_GB:-unknown}GB < ${MIN_DISK_GB}GB"
    fi
  else
    warn "数据目录 ${DATA_ROOT} 不存在（部署时创建）"
  fi
else
  warn "未设置 DATA_ROOT，跳过数据盘检查"
fi

# 6. 构建/运行依赖
for cmd in cmake ninja gcc g++ make python3; do
  if have "$cmd"; then
    pass "命令存在: ${cmd}"
  else
    warn "命令缺失: ${cmd}（从零编译时需要）"
  fi
done

if have rpm; then
  if rpm -q libstdc++-static >/dev/null 2>&1; then
    pass "libstdc++-static 已安装"
  else
    warn "libstdc++-static 未安装：链接可能报 cannot find -lstdc++"
  fi
fi

# 7. third-party
if [ -n "$THIRD_PARTY" ]; then
  if [ -d "$THIRD_PARTY/lib" ]; then
    A_CNT=$(find "$THIRD_PARTY/lib" -maxdepth 1 -name '*.a' 2>/dev/null | wc -l)
    if [ "$A_CNT" -ge 50 ]; then
      pass "third-party ${THIRD_PARTY} 存在，静态库 ${A_CNT} 个"
    else
      fail "third-party ${THIRD_PARTY} 静态库仅 ${A_CNT} 个（预期约 80+）"
    fi
  else
    fail "third-party 路径不存在或缺少 lib/: ${THIRD_PARTY}"
  fi
else
  warn "未设置 THIRD_PARTY，跳过 third-party 检查"
fi

# 8. 源码/运行目录
if [ -n "$NEBULA_HOME" ]; then
  if [ -d "$NEBULA_HOME" ]; then
    pass "NEBULA_HOME 存在: ${NEBULA_HOME}"
    TZ_FILE="$NEBULA_HOME/share/resources/date_time_zonespec.csv"
    if [ -f "$TZ_FILE" ]; then
      pass "时区文件存在: ${TZ_FILE}"
    else
      warn "时区文件缺失: ${TZ_FILE}（从 resources/ 复制，否则启动可能崩溃）"
    fi
    CLUSTER_ID="$NEBULA_HOME/cluster.id"
    if [ -f "$CLUSTER_ID" ]; then
      warn "存在 cluster.id：清库/恢复数据后启动前必须删除"
    else
      pass "未发现残留 cluster.id"
    fi
    if [ -d "$NEBULA_HOME/build/bin" ]; then
      pass "构建产物目录存在: ${NEBULA_HOME}/build/bin"
    else
      warn "未发现 build/bin：尚未编译或构建目录不同"
    fi
  else
    fail "NEBULA_HOME 不存在: ${NEBULA_HOME}"
  fi
else
  warn "未设置 NEBULA_HOME，跳源码目录检查"
fi

# 9. 端口占用
if [ "$SKIP_PORTS" = "1" ]; then
  warn "SKIP_PORTS=1，跳过端口空闲检查"
elif have ss; then
  BUSY=""
  for p in $CHECK_PORTS; do
    if ss -lnt 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${p}$"; then
      BUSY="${BUSY} ${p}"
    fi
  done
  if [ -z "$BUSY" ]; then
    pass "检查的 ${CHECK_PORTS} 端口全部空闲"
  else
    fail "端口已被占用:${BUSY}（确认是否旧进程/其他服务；storaged 每实例占 3 个连续端口）"
  fi
else
  warn "ss 不可用，跳过端口检查"
fi

# 10. 残留 Nebula 进程
if [ "$SKIP_PROC" = "1" ]; then
  warn "SKIP_PROC=1，跳过残留进程检查"
else
  PROCS=$(pgrep -a -f 'nebula-(metad|graphd|storaged|standalone)' 2>/dev/null || true)
  if [ -z "$PROCS" ]; then
    pass "未发现残留 Nebula 进程"
  else
    warn "发现 Nebula 进程，部署/替换二进制前先停止："
    echo "$PROCS" | sed 's/^/       /'
  fi
fi

# 11. 系统调优提示（只读）
if [ -r /sys/kernel/mm/transparent_hugepage/enabled ]; then
  THP=$(cat /sys/kernel/mm/transparent_hugepage/enabled)
  case "$THP" in
    *"[always]"*) warn "THP=always：数据库场景通常建议 madvise/never，按团队基线确认" ;;
    *) pass "THP 当前: ${THP}" ;;
  esac
fi
if [ -r /proc/sys/kernel/numa_balancing ]; then
  NB=$(cat /proc/sys/kernel/numa_balancing 2>/dev/null || echo unknown)
  if [ "$NB" = "1" ]; then
    warn "numa_balancing=1：可能引入自动迁移抖动，压测前按基线确认"
  else
    pass "numa_balancing=${NB}"
  fi
fi
ULIMIT_NOFILE=$(ulimit -n 2>/dev/null || echo unknown)
if [ "$ULIMIT_NOFILE" != "unknown" ] && [ "$ULIMIT_NOFILE" -lt 65535 ] 2>/dev/null; then
  warn "ulimit -n=${ULIMIT_NOFILE} < 65535：高并发下可能耗尽 fd"
else
  pass "ulimit -n=${ULIMIT_NOFILE}"
fi

echo "------------------------------------------------------------"
echo "Summary: PASS=${PASS_CNT} WARN=${WARN_CNT} FAIL=${FAIL_CNT}"
if [ "$FAIL_CNT" -gt 0 ]; then
  echo "结果: FAIL — 修复后重跑，不得在存在 FAIL 时开始部署。"
  exit 1
fi
echo "结果: PASS/WARN — WARN 项确认影响后可继续。"
exit 0
