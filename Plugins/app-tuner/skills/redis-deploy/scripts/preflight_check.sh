#!/usr/bin/env bash
#===============================================================
# preflight_check.sh — Redis 部署前置检查（只读，无副作用）
#
# 用法:
#   bash scripts/preflight_check.sh [options]
#
# 检查项:
#   1. root 权限
#   2. 操作系统与 CPU 架构
#   3. 内存、CPU 核数、目标目录磁盘空间
#   4. 基础工具（tar/awk/sed/sha256sum/curl 或 wget）与构建工具
#   5. 目标端口占用
#   6. 既有 Redis 安装 / 服务 / 配置文件
#   7. 下载源可达性（离线时用 --tarball 可跳过）
#   8. 运行参数提示（overcommit、THP、somaxconn、nofile）
#   9. 安全环境提示（SELinux / firewalld / 默认 bind）
#  10. 本地源码包 sha256 校验（提供 --tarball 时）
#
# 输出逐项 [PASS]/[WARN]/[FAIL] + 汇总；存在 FAIL 时退出码非零。
# 本脚本不创建、不修改任何文件，也不启动服务。
#===============================================================
set -u

SKILL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/common.sh
source "$SKILL_ROOT/scripts/lib/common.sh"

VERSION=7.0.15
PORT=6379
PREFIX=""
INSTANCE=redis
BIND=127.0.0.1
MIRROR=https://download.redis.io/releases
TARBALL=""
EXPECTED_SHA=""

usage() {
  cat <<'EOF'
用法: preflight_check.sh [options]

只读检查 Redis 部署前置条件，不修改任何配置。

选项:
  --version VER        计划安装的 Redis 版本（默认 7.0.15）
  --prefix DIR        计划安装前缀（默认 /opt/redis-<version>）
  --instance-name NAME 实例名（默认 redis；决定配置文件与服务名）
  --port PORT         计划监听端口（默认 6379）
  --bind ADDR         计划 bind 地址（默认 127.0.0.1，仅用于提示）
  --mirror URL        下载源（默认 https://download.redis.io/releases）
  --tarball FILE      使用本地源码包（离线部署）
  --sha256 HEX        源码包期望 sha256（否则查 assets/checksums.txt）
  -h, --help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION=${2:?--version 缺少参数}; shift 2 ;;
    --prefix) PREFIX=${2:?--prefix 缺少参数}; shift 2 ;;
    --instance-name) INSTANCE=${2:?--instance-name 缺少参数}; shift 2 ;;
    --port) PORT=${2:?--port 缺少参数}; shift 2 ;;
    --bind) BIND=${2:?--bind 缺少参数}; shift 2 ;;
    --mirror) MIRROR=${2:?--mirror 缺少参数}; shift 2 ;;
    --tarball) TARBALL=${2:?--tarball 缺少参数}; shift 2 ;;
    --sha256) EXPECTED_SHA=${2:?--sha256 缺少参数}; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数: $1（-h 查看用法）" ;;
  esac
done

[[ -n "$PREFIX" ]] || PREFIX="/opt/redis-$VERSION"

PASS_CNT=0
WARN_CNT=0
FAIL_CNT=0

pass() { PASS_CNT=$((PASS_CNT + 1)); printf '%s[PASS]%s %s\n' "$C_GRN" "$C_RST" "$*"; }
warn() { WARN_CNT=$((WARN_CNT + 1)); printf '%s[WARN]%s %s\n' "$C_YEL" "$C_RST" "$*"; }
fail() { FAIL_CNT=$((FAIL_CNT + 1)); printf '%s[FAIL]%s %s\n' "$C_RED" "$C_RST" "$*"; }
info() { printf '[INFO] %s\n' "$*"; }
sect() { printf '\n===== %s =====\n' "$*"; }

# ---------- 1. root ----------
sect "1. 权限"
if [[ "$(id -u)" -eq 0 ]]; then
  pass "以 root 运行"
else
  fail "未以 root 运行（安装依赖、写 /etc、创建用户和 systemd 服务都需要 root）"
fi

# ---------- 2. OS / arch ----------
sect "2. 操作系统与架构"
if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  info "发行版: ${PRETTY_NAME:-unknown}"
  case "${ID:-}" in
    openEuler|centos|rhel|fedora|rocky|almalinux|debian|ubuntu|kylin|anolis)
      pass "发行版在支持列表内（${ID}）" ;;
    *)
      warn "未在已验证发行版列表内（${ID:-unknown}），脚本按 yum/apt 通用路径尝试" ;;
  esac
else
  warn "/etc/os-release 不可读，无法识别发行版"
fi
ARCH=$(uname -m)
case "$ARCH" in
  x86_64|aarch64) pass "CPU 架构: $ARCH" ;;
  *) warn "未验证的 CPU 架构: $ARCH（Redis 源码理论上可构建，需自行验证）" ;;
esac
info "内核: $(uname -r)"

# ---------- 3. 资源 ----------
sect "3. 资源"
NPROC=$(nproc 2>/dev/null || echo 1)
pass "可用 CPU 核数: $NPROC"
MEM_MB=$(awk '/^MemTotal:/ {printf "%d", $2 / 1024}' /proc/meminfo 2>/dev/null || echo 0)
if [[ "${MEM_MB:-0}" -ge 1024 ]]; then
  pass "内存: ${MEM_MB} MiB"
else
  warn "内存偏小: ${MEM_MB} MiB（建议至少 1 GiB，构建与运行都需要余量）"
fi

disk_target=$PREFIX
while [[ ! -d "$disk_target" && "$disk_target" != "/" ]]; do
  disk_target=$(dirname "$disk_target")
done
DISK_AVAIL_MB=$(df -Pk "$disk_target" 2>/dev/null | awk 'NR == 2 {printf "%d", $4 / 1024}')
if [[ "${DISK_AVAIL_MB:-0}" -ge 1024 ]]; then
  pass "目标路径可用空间: ${DISK_AVAIL_MB} MiB（$disk_target）"
else
  warn "目标路径可用空间不足: ${DISK_AVAIL_MB:-unknown} MiB（$disk_target，建议 >= 1 GiB）"
fi

# ---------- 4. 工具 ----------
sect "4. 工具依赖"
for cmd in tar awk sed gcc make; do
  if command -v "$cmd" >/dev/null 2>&1; then
    pass "命令存在: $cmd"
  else
    case "$cmd" in
      gcc|make) warn "缺少构建工具 $cmd（install_deps.sh 会自动安装，或加 --skip-deps 自行准备）" ;;
      *) fail "缺少基础命令: $cmd" ;;
    esac
  fi
done
if command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1; then
  pass "sha256 校验工具可用"
else
  fail "缺少 sha256sum/shasum，无法校验源码包"
fi
if command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1; then
  pass "下载工具可用（curl 或 wget）"
else
  warn "缺少 curl 与 wget；在线部署无法下载，离线请用 --tarball"
fi
if command -v useradd >/dev/null 2>&1; then
  pass "useradd 可用（用于创建 redis 运行用户）"
else
  warn "缺少 useradd（install_deps 会安装 shadow-utils/passwd；--skip-deps 时需自备）"
fi

# ---------- 5. 端口 ----------
sect "5. 端口占用"
if ! [[ "$PORT" =~ ^[0-9]+$ ]] || ((PORT < 1 || PORT > 65535)); then
  fail "端口非法: $PORT"
elif command -v ss >/dev/null 2>&1; then
  if ss -lntH "sport = :$PORT" 2>/dev/null | grep -q .; then
    fail "端口 $PORT 已被监听（先确认并停止旧实例，或改用 --port 选择空闲端口）"
    ss -lntp "sport = :$PORT" 2>/dev/null | sed 's/^/       /'
  else
    pass "端口 $PORT 空闲"
  fi
else
  warn "ss 不可用，无法检查端口 $PORT 占用"
fi

# ---------- 6. 既有安装 ----------
sect "6. 既有 Redis 安装"
if [[ -x "$PREFIX/bin/redis-server" ]]; then
  existing=$("$PREFIX/bin/redis-server" --version 2>/dev/null | awk '{print $3}')
  if [[ "$existing" == "v=$VERSION" ]]; then
    warn "$PREFIX 已存在同版本 Redis（$existing），重跑将复用构建产物并重写配置"
  else
    warn "$PREFIX 已存在其他版本 Redis（${existing:-unknown}），确认是否需要并存或先清理"
  fi
else
  pass "$PREFIX 下无既有 redis-server"
fi
if [[ -f "/etc/systemd/system/$INSTANCE.service" ]]; then
  warn "systemd 单元已存在: /etc/systemd/system/$INSTANCE.service（重跑会先备份再覆盖）"
fi
if [[ -f "/etc/redis/$INSTANCE.conf" ]]; then
  warn "配置文件已存在: /etc/redis/$INSTANCE.conf（重跑会先备份；--keep-conf 可保留现状）"
fi
if [[ -f "/etc/redis/$INSTANCE.conf" ]] && grep -q '^[[:space:]]*requirepass[[:space:]]' "/etc/redis/$INSTANCE.conf" 2>/dev/null; then
  info "现有配置包含 requirepass（不读取、不打印密码）"
fi
if command -v pgrep >/dev/null 2>&1; then
  procs=$(pgrep -af redis-server 2>/dev/null | head -n 3 || true)
  if [[ -n "$procs" ]]; then
    info "当前 redis-server 进程（最多 3 条）:"
    printf '%s\n' "$procs" | sed 's/^/       /'
  fi
fi

# ---------- 7. 下载源 ----------
sect "7. 下载源可达性"
if [[ -n "$TARBALL" ]]; then
  pass "使用本地源码包，跳过下载源检查"
else
  if command -v getent >/dev/null 2>&1; then
    rest=${MIRROR#*://}
    hostport=${rest%%/*}
    host=${hostport%%:*}
    scheme=${MIRROR%%://*}
    port=${hostport#*:}
    if [[ "$port" == "$hostport" ]]; then
      [[ "$scheme" == "http" ]] && port=80 || port=443
    fi
    if getent hosts "$host" >/dev/null 2>&1; then
      # 下载源连通性检查：只读 HTTP HEAD 或 TCP 端口探测，不执行远端命令。
      reachable=0
      probe_tool=""
      if command -v curl >/dev/null 2>&1; then
        probe_tool=curl
        if curl -sI --connect-timeout 5 --max-time 10 -o /dev/null "$MIRROR" 2>/dev/null; then
          reachable=1
        fi
      elif command -v nc >/dev/null 2>&1; then
        probe_tool=nc
        if nc -z -w 5 "$host" "$port" >/dev/null 2>&1; then
          reachable=1
        fi
      elif command -v wget >/dev/null 2>&1; then
        probe_tool=wget
        if wget --spider -T 5 -t 1 -q "$MIRROR" >/dev/null 2>&1; then
          reachable=1
        fi
      fi
      if ((reachable == 1)); then
        pass "下载源可达: $MIRROR（探测: $probe_tool）"
      elif [[ -z "$probe_tool" ]]; then
        warn "缺少 curl/nc/wget，跳过下载源连通性检查"
      else
        warn "无法连接 $host:$port；在线下载可能失败，离线请用 --tarball FILE"
      fi
    else
      warn "无法解析下载源域名: $host；离线请用 --tarball FILE"
    fi
  else
    warn "getent 不可用，跳过下载源检查"
  fi
fi

# ---------- 8. 运行参数 ----------
sect "8. 运行参数提示（不自动修改）"
overcommit=$(cat /proc/sys/vm/overcommit_memory 2>/dev/null || echo unknown)
if [[ "$overcommit" == "1" ]]; then
  pass "vm.overcommit_memory=1"
elif [[ "$overcommit" == "unknown" ]]; then
  warn "无法读取 vm.overcommit_memory"
else
  warn "vm.overcommit_memory=$overcommit（建议 1，避免 BGSAVE fork 失败；修改前需确认）"
fi

thp_file=/sys/kernel/mm/transparent_hugepage/enabled
if [[ -r "$thp_file" ]]; then
  thp=$(cat "$thp_file")
  if [[ "$thp" == *"[never]"* ]]; then
    pass "THP 已关闭"
  else
    warn "THP 未关闭（当前: $thp），Redis 启动会告警；建议 never/madvise，需与其它业务确认"
  fi
else
  warn "无法读取 THP 状态"
fi

somaxconn=$(cat /proc/sys/net/core/somaxconn 2>/dev/null || echo unknown)
if [[ "$somaxconn" =~ ^[0-9]+$ ]] && ((somaxconn >= 511)); then
  pass "net.core.somaxconn=$somaxconn"
else
  warn "net.core.somaxconn=$somaxconn（建议 >= 511，与 redis.conf 的 tcp-backlog 匹配）"
fi

nofile=$(ulimit -Hn 2>/dev/null || echo unknown)
if [[ "$nofile" == "unlimited" ]] || { [[ "$nofile" =~ ^[0-9]+$ ]] && ((nofile >= 10032)); }; then
  pass "硬 nofile 限制: $nofile"
else
  warn "硬 nofile 限制偏小: $nofile（systemd 单元会设置 LimitNOFILE=1000000）"
fi

# ---------- 9. 安全环境 ----------
sect "9. 安全环境提示"
if command -v getenforce >/dev/null 2>&1; then
  info "SELinux: $(getenforce 2>/dev/null || echo unknown)"
fi
if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet firewalld 2>/dev/null; then
  info "firewalld 正在运行；若需跨机访问，请按需放行端口 $PORT"
fi
case "$BIND" in
  127.*|*::1*|*' -'*) info "计划 bind: $BIND（默认仅本机访问，符合最小暴露原则）" ;;
  *) info "计划 bind: $BIND（非回环地址，部署时需提供 --password-file 或显式 --allow-insecure-remote）" ;;
esac
info "连接验证请使用 REDISCLI_AUTH 环境变量传密码，禁止 redis-cli -a 在命令行暴露密码"

# ---------- 10. 源码包校验 ----------
sect "10. 源码包校验"
if [[ -n "$TARBALL" ]]; then
  if [[ ! -f "$TARBALL" ]]; then
    fail "源码包不存在: $TARBALL"
  else
    if [[ -z "$EXPECTED_SHA" && -f "$SKILL_ROOT/assets/checksums.txt" ]]; then
      EXPECTED_SHA=$(awk -v v="$VERSION" '$1 == v {print $2; exit}' "$SKILL_ROOT/assets/checksums.txt")
    fi
    if [[ -z "$EXPECTED_SHA" ]]; then
      warn "版本 $VERSION 没有内置校验值；部署时需 --sha256 HEX，或显式 --allow-unverified"
    elif ! is_sha256_hex "$EXPECTED_SHA"; then
      fail "sha256 格式非法: $EXPECTED_SHA"
    else
      actual=$(sha256_of "$TARBALL")
      if [[ "$actual" == "${EXPECTED_SHA,,}" ]]; then
        pass "源码包 sha256 校验通过"
      else
        fail "源码包 sha256 不匹配（期望 ${EXPECTED_SHA,,}，实际 $actual）"
      fi
    fi
  fi
else
  info "未提供 --tarball，部署时将在线下载并校验（默认版本 7.0.15 内置校验值）"
fi

# ---------- 汇总 ----------
printf '\n===== 汇总 =====\n'
printf 'PASS=%d  WARN=%d  FAIL=%d\n' "$PASS_CNT" "$WARN_CNT" "$FAIL_CNT"
if ((FAIL_CNT > 0)); then
  printf '存在 FAIL 项，修复后重跑本脚本再开始部署。\n'
  exit 1
fi
printf '前置检查通过（WARN 项确认影响后可继续）。\n'
exit 0
