#!/usr/bin/env bash

# MySQL 部署前只读检查（PASS/WARN/FAIL）。不修改系统。

# 用法：

# MYSQL_HOME=/opt/mysql-8.0.25 DATA_ROOT=/data/mysql MYSQL_PORT=3306 \

  # bash scripts/preflight_check.sh

# 可选：MYSQL_SRC、BOOST_ROOT、SPEC_MEM_GB、MYSQL_USER、SOCKET

# 退出码：0=无 FAIL；1=存在 FAIL。

set -u

MYSQL_HOME="${MYSQL_HOME:-}"
DATA_ROOT="${DATA_ROOT:-}"
MYSQL_PORT="${MYSQL_PORT:-3306}"
MYSQLX_PORT="${MYSQLX_PORT:-33060}"
MYSQLX_ENABLED="${MYSQLX_ENABLED:-0}"

# 端口范围校验：65536 等能过纯数字校验但实例无法绑定，探测无意义——按用法错误拒绝
validate_port() {
  name="$1"; value="$2"
  case "${value}" in ''|*[!0-9]*) echo "FAIL  ${name} 须为数字: ${value}" >&2; exit 2 ;; esac
  if [ "${value}" -lt 1 ] || [ "${value}" -gt 65535 ]; then
    echo "FAIL  ${name} 超出合法范围 1-65535: ${value}" >&2
    exit 2
  fi
}
case "${MYSQLX_ENABLED}" in
  0|1) ;;
  *) echo "FAIL  MYSQLX_ENABLED 须为 0 或 1: ${MYSQLX_ENABLED}" >&2; exit 2 ;;
esac
validate_port MYSQL_PORT "${MYSQL_PORT}"
if [ "${MYSQLX_ENABLED}" = "1" ]; then
  validate_port MYSQLX_PORT "${MYSQLX_PORT}"
  if [ "${MYSQLX_PORT}" = "${MYSQL_PORT}" ]; then
    echo "FAIL  MYSQLX_PORT 与 MYSQL_PORT 不能相同: ${MYSQLX_PORT}" >&2
    exit 2
  fi
fi
MYSQL_SRC="${MYSQL_SRC:-}"
BOOST_ROOT="${BOOST_ROOT:-}"
MYSQL_USER="${MYSQL_USER:-mysql}"
SOCKET="${SOCKET:-${DATA_ROOT}/mysql.sock}"
SPEC_MEM_GB="${SPEC_MEM_GB:-0}"

fail=0
warn=0

pass() { printf '[PASS] %s\n' "$*"; }
note() { printf '[WARN] %s\n' "$*"; warn=$((warn + 1)); }
bad()  { printf '[FAIL] %s\n' "$*"; fail=$((fail + 1)); }

echo "== MySQL preflight (read-only) =="
echo "MYSQL_HOME=${MYSQL_HOME:-<unset>}"
echo "DATA_ROOT=${DATA_ROOT:-<unset>}"
echo "MYSQL_PORT=${MYSQL_PORT}"
echo "MYSQLX_ENABLED=${MYSQLX_ENABLED}"
if [ "${MYSQLX_ENABLED}" = "1" ]; then echo "MYSQLX_PORT=${MYSQLX_PORT}"; fi
echo "MYSQL_USER=${MYSQL_USER}"
echo "SOCKET=${SOCKET:-<unset>}"
echo

# 1. 架构与 CPU

arch="$(uname -m 2>/dev/null || echo unknown)"
case "$arch" in
  aarch64|x86_64) pass "架构 ${arch}" ;;
  *) note "架构 ${arch} 非 aarch64/x86_64，编译参数需重新评估" ;;
esac

cores="$(nproc 2>/dev/null || echo 0)"
if [ "${cores:-0}" -ge 8 ]; then
  pass "逻辑 CPU ${cores}"
else
  note "逻辑 CPU ${cores}，小规格需按 §规格与目录 重新规划线程/绑核"
fi

# 2. 内存

mem_kb="$(awk '/MemTotal/{print $2}' /proc/meminfo 2>/dev/null || echo 0)"
mem_gb=$((mem_kb / 1024 / 1024))
if [ "$mem_gb" -ge 8 ]; then
  pass "内存 ${mem_gb} GiB"
else
  note "内存 ${mem_gb} GiB，低于常用 POC 规格"
fi
if [ "${SPEC_MEM_GB}" -gt 0 ] && [ "$mem_gb" -lt "${SPEC_MEM_GB}" ]; then
  bad "内存 ${mem_gb} GiB 小于目标规格 ${SPEC_MEM_GB} GiB（目标规格基线不可信）"
fi

# 3. 磁盘

if [ -n "${DATA_ROOT}" ]; then
  if [ -d "${DATA_ROOT}" ]; then
    avail_gb="$(df -Pk "${DATA_ROOT}" 2>/dev/null | awk 'NR==2{print int($4/1024/1024)}')"
    if [ "${avail_gb:-0}" -ge 50 ]; then
      pass "DATA_ROOT 可用空间 ${avail_gb} GiB"
    else
      note "DATA_ROOT 可用空间 ${avail_gb} GiB，小于 50 GiB"
    fi
  else
    note "DATA_ROOT 不存在（部署时创建）：${DATA_ROOT}"
  fi
else
  note "未设置 DATA_ROOT，跳过磁盘检查"
fi

# 4. 工具链

for t in gcc g++ cmake make tar; do
  if command -v "$t" >/dev/null 2>&1; then
    pass "工具存在：$t"
  else
    bad "缺少工具：$t"
  fi
done
if command -v sha256sum >/dev/null 2>&1; then pass "工具存在：sha256sum"; else note "缺少 sha256sum，无法做包校验"; fi

# 5. 源码与 Boost

if [ -n "${MYSQL_SRC}" ] && [ -d "${MYSQL_SRC}" ]; then
  pass "MySQL 源码目录存在"
else
  note "未找到 MYSQL_SRC（若只做运行态检查可忽略）"
fi
if [ -n "${BOOST_ROOT}" ] && [ -d "${BOOST_ROOT}" ]; then
  pass "Boost 目录存在"
else
  note "未找到 BOOST_ROOT（源码构建需要 Boost 1.73.0）"
fi

# 6. 安装前缀与二进制

if [ -n "${MYSQL_HOME}" ] && [ -x "${MYSQL_HOME}/bin/mysqld" ]; then
  pass "已安装 mysqld：$("${MYSQL_HOME}/bin/mysqld" --version 2>/dev/null | head -n1)"
else
  note "MYSQL_HOME/bin/mysqld 不存在（首次部署预期）"
fi

# 7. 端口与 socket

check_listen_port() {
  p="$1"
  if ss -lnt 2>/dev/null | awk '{print $4}' | grep -q ":${p}$"; then
    bad "端口 ${p} 已被监听"
  else
    pass "端口 ${p} 空闲"
  fi
}
if command -v ss >/dev/null 2>&1; then
  check_listen_port "${MYSQL_PORT}"
  if [ "${MYSQLX_ENABLED}" = "1" ]; then
    check_listen_port "${MYSQLX_PORT}"
  fi
else
  note "缺少 ss，无法检查端口"
fi
if [ -e "${SOCKET}" ]; then
  bad "socket 已存在，可能残留：${SOCKET}"
else
  pass "socket 路径未被占用"
fi
# 只查 socket 文件是否存在不够：须按运行账号核验父目录可写
SOCKET_DIR="$(dirname "${SOCKET}")"
if [ -d "${SOCKET_DIR}" ]; then
  if [ "$(id -u)" -eq 0 ] && id "${MYSQL_USER}" >/dev/null 2>&1; then
    if command -v runuser >/dev/null 2>&1; then
      runuser -u "${MYSQL_USER}" -- test -w "${SOCKET_DIR}" 2>/dev/null
    else
      su -s /bin/sh "${MYSQL_USER}" -c 'test -w "$1"' sh "${SOCKET_DIR}" 2>/dev/null
    fi
    if [ $? -eq 0 ]; then
      pass "socket 父目录对运行账号 ${MYSQL_USER} 可写：${SOCKET_DIR}"
    else
      bad "socket 父目录对运行账号 ${MYSQL_USER} 不可写：${SOCKET_DIR}"
    fi
  elif [ "$(id -un)" = "${MYSQL_USER}" ]; then
    if [ -w "${SOCKET_DIR}" ]; then
      pass "socket 父目录对运行账号 ${MYSQL_USER} 可写：${SOCKET_DIR}"
    else
      bad "socket 父目录对运行账号 ${MYSQL_USER} 不可写：${SOCKET_DIR}"
    fi
  else
    note "无法以 ${MYSQL_USER} 核验 socket 父目录写权限，请在部署机以运行账号复核：${SOCKET_DIR}"
  fi
else
  note "socket 父目录不存在（部署时创建并授权运行账号）：${SOCKET_DIR}"
fi

# 8. 残留进程

if pgrep -x mysqld >/dev/null 2>&1; then
  note "检测到运行中的 mysqld，请确认是否为本实例"
else
  pass "无运行中的 mysqld"
fi

# 9. 关键系统参数（只读提示）

thp="$(cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null || echo unknown)"
echo "[INFO] THP=${thp}"
case "$thp" in
  *"[never]"*) pass "THP 已关闭" ;;
  *) note "THP 未关闭；MySQL 裸机通常建议 never，虚机/大页场景按 §大页 决策" ;;
esac

hp="$(cat /proc/sys/vm/nr_hugepages 2>/dev/null || echo unknown)"
echo "[INFO] nr_hugepages=${hp}"

ulimit_n="$(ulimit -n 2>/dev/null || echo unknown)"
case "$ulimit_n" in
  unlimited|*[0-9]*) pass "ulimit -n=${ulimit_n}" ;;
  *) note "无法读取 ulimit -n" ;;
esac
if [ "${ulimit_n}" != "unlimited" ]; then
  if [ "${ulimit_n:-0}" -lt 102400 ] 2>/dev/null; then
    note "ulimit -n=${ulimit_n} < 102400，高并发可能不足"
  fi
fi

if command -v getenforce >/dev/null 2>&1; then
  se="$(getenforce 2>/dev/null || echo Unknown)"
  echo "[INFO] SELinux=${se}"
  [ "$se" = "Disabled" ] && pass "SELinux 已关闭" || note "SELinux=${se}；MySQL 场景通常建议关闭（需确认策略）"
fi

# 10. 运行用户

if id "${MYSQL_USER}" >/dev/null 2>&1; then
  pass "运行用户存在：${MYSQL_USER}"
else
  note "运行用户不存在，部署时需创建：${MYSQL_USER}"
fi

echo
echo "== summary: FAIL=${fail} WARN=${warn} =="
if [ "$fail" -gt 0 ]; then
  echo "存在 FAIL，禁止开始部署。修复后重跑。"
  exit 1
fi
echo "无 FAIL，可进入部署；请逐项确认 WARN 影响。"
exit 0
