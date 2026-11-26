#!/usr/bin/env bash

# MariaDB 部署前只读检查（不修改系统）。

#

# 用法：

# MARIADB_HOME=/opt/mariadb/10.11.16 DATA_ROOT=/data/mariadb PORT=3306 \

  # bash scripts/preflight_check.sh

#

# 输出：PASS / WARN / FAIL 行 + 汇总；存在 FAIL 时退出码非 0。

# 依赖：bash、coreutils；可选 numactl、ss、lscpu。仅 Linux。

set -u

MARIADB_HOME="${MARIADB_HOME:-/opt/mariadb/10.11.16}"
DATA_ROOT="${DATA_ROOT:-/data/mariadb}"
PORT="${PORT:-3306}"
# 端口范围校验：65536 等能过纯数字校验但实例无法绑定，探测无意义——按用法错误拒绝
case "${PORT}" in ''|*[!0-9]*) echo "FAIL  PORT 须为数字: ${PORT}" >&2; exit 2 ;; esac
if [ "${PORT}" -lt 1 ] || [ "${PORT}" -gt 65535 ]; then
  echo "FAIL  PORT 超出合法范围 1-65535: ${PORT}" >&2
  exit 2
fi
EXTRA_PORT="${EXTRA_PORT:-3307}"
# 救援口同样校验范围；端口非法按用法错误拒绝
case "${EXTRA_PORT}" in ''|*[!0-9]*) echo "FAIL  EXTRA_PORT 须为数字: ${EXTRA_PORT}" >&2; exit 2 ;; esac
if [ "${EXTRA_PORT}" -lt 1 ] || [ "${EXTRA_PORT}" -gt 65535 ]; then
  echo "FAIL  EXTRA_PORT 超出合法范围 1-65535: ${EXTRA_PORT}" >&2
  exit 2
fi

PASS=0
WARN=0
FAIL=0

pass() { printf 'PASS  %s\n' "$*"; PASS=$((PASS + 1)); }
warn() { printf 'WARN  %s\n' "$*"; WARN=$((WARN + 1)); }
fail() { printf 'FAIL  %s\n' "$*"; FAIL=$((FAIL + 1)); }
info() { printf 'INFO  %s\n' "$*"; }

section() { printf '\n== %s ==\n' "$*"; }

have() { command -v "$1" >/dev/null 2>&1; }

section "1. 操作系统与架构"

if [ "$(uname -s)" = "Linux" ]; then
  pass "Linux 内核 $(uname -r)"
else
  fail "非 Linux：$(uname -s)（本脚本只支持 Linux）"
fi

ARCH="$(uname -m)"
case "$ARCH" in
  aarch64|arm64) pass "架构 $ARCH（ARM64）" ;;
  x86_64|amd64)  pass "架构 $ARCH（x86_64）" ;;
  *) warn "架构 $ARCH 未在验证列表内" ;;
esac

if have lscpu; then
  THREADS="$(lscpu | awk -F: '$1=="CPU(s)"{gsub(/ /,"",$2);print $2;exit}')"
  SOCKETS="$(lscpu | awk -F: '$1=="Socket(s)"{gsub(/ /,"",$2);print $2;exit}')"
  NUMAS="$(lscpu | awk -F: '$1=="NUMA node(s)"{gsub(/ /,"",$2);print $2;exit}')"
  info "逻辑 CPU=${THREADS:-?} Socket=${SOCKETS:-?} NUMA=${NUMAS:-?}"
  if [ -n "${THREADS:-}" ] && [ "${THREADS:-0}" -ge 8 ] 2>/dev/null; then
    pass "CPU 核数 ${THREADS} 满足最小要求"
  else
    warn "CPU 核数偏少（${THREADS:-未知}），压测结果参考价值有限"
  fi
else
  warn "缺少 lscpu，跳过拓扑检查"
fi

section "2. 内存"

MEM_TOTAL_KB="$(awk '/^MemTotal:/{print $2}' /proc/meminfo 2>/dev/null || echo 0)"
MEM_TOTAL_GB=$((MEM_TOTAL_KB / 1024 / 1024))
MEM_AVAIL_GB=$(( $(awk '/^MemAvailable:/{print $2}' /proc/meminfo 2>/dev/null || echo 0) / 1024 / 1024 ))
if [ "$MEM_TOTAL_GB" -ge 8 ]; then
  pass "内存 ${MEM_TOTAL_GB}GB（可用 ${MEM_AVAIL_GB}GB）"
else
  warn "内存 ${MEM_TOTAL_GB}GB 偏小，buffer pool 规划需保守"
fi
info "建议：buffer pool 占内存 50%-70%，并为连接/临时表/OS 预留余量"

SWAPPINESS="$(cat /proc/sys/vm/swappiness 2>/dev/null || echo unknown)"
if [ "$SWAPPINESS" = "unknown" ]; then
  warn "无法读取 vm.swappiness"
elif [ "$SWAPPINESS" -le 10 ] 2>/dev/null; then
  pass "vm.swappiness=${SWAPPINESS}（数据库场景合理）"
else
  warn "vm.swappiness=${SWAPPINESS} 偏高，可能引发换页（变更需评估）"
fi

if [ -r /proc/sys/vm/nr_hugepages ]; then
  HP="$(cat /proc/sys/vm/nr_hugepages)"
  info "vm.nr_hugepages=${HP}（启用 large_pages 时需按 NUMA 预留）"
fi

section "3. 透明大页与 THP"

THP_FILE="/sys/kernel/mm/transparent_hugepage/enabled"
if [ -r "$THP_FILE" ]; then
  THP="$(cat "$THP_FILE")"
  case "$THP" in
    *'[never]'*) pass "THP=never" ;;
    *'[madvise]'*) warn "THP=madvise（需确认应用行为）" ;;
    *) warn "THP 未关闭：$THP（数据库建议显式大页或 never）" ;;
  esac
else
  info "无 THP 接口（可能为容器/旧内核）"
fi

section "4. 磁盘与文件系统"

if [ -d "$DATA_ROOT" ]; then
  pass "数据目录存在：$DATA_ROOT"
  FS="$(stat -f -c '%T' "$DATA_ROOT" 2>/dev/null || echo unknown)"
  case "$FS" in
    xfs|ext4) pass "文件系统 $FS" ;;
    overlay|tmpfs|unknown) warn "文件系统 $FS，性能测试建议 XFS/ext4 + 本地 NVMe" ;;
    *) warn "文件系统 $FS 未在推荐列表" ;;
  esac
  AVAIL_GB="$(df -Pk "$DATA_ROOT" 2>/dev/null | awk 'NR==2{print int($4/1024/1024)}')"
  if [ -n "${AVAIL_GB:-}" ] && [ "$AVAIL_GB" -ge 50 ]; then
    pass "可用空间 ${AVAIL_GB}GB"
  else
    warn "可用空间 ${AVAIL_GB:-未知}GB，装载前确认容量"
  fi
  if [ -w "$DATA_ROOT" ]; then
    pass "数据目录可写"
  else
    fail "数据目录不可写（权限/SELinux/只读挂载）"
  fi
else
  fail "数据目录不存在：$DATA_ROOT"
fi

MOUNT_OPTS="$(findmnt -no OPTIONS "$DATA_ROOT" 2>/dev/null || echo '')"
case "$MOUNT_OPTS" in
  *noatime*) pass "挂载含 noatime" ;;
  '') info "无法读取挂载参数" ;;
  *) warn "挂载未见 noatime：$MOUNT_OPTS" ;;
esac

section "5. 依赖与二进制"

if [ -x "${MARIADB_HOME}/bin/mariadbd" ]; then
  pass "mariadbd 存在：${MARIADB_HOME}/bin/mariadbd"
  V="$("${MARIADB_HOME}/bin/mariadbd" --version 2>/dev/null || true)"
  [ -n "$V" ] && info "$V"
else
  warn "未找到 ${MARIADB_HOME}/bin/mariadbd（源码部署前属正常）"
fi

for bin in mariadb mariadb-admin mariadb-install-db mariadb-backup; do
  if [ -x "${MARIADB_HOME}/bin/${bin}" ]; then
    pass "工具存在：${bin}"
  else
    warn "工具缺失：${MARIADB_HOME}/bin/${bin}"
  fi
done

if have numactl; then
  pass "numactl 可用"
else
  warn "缺少 numactl，无法做 NUMA 绑核/内存交错"
fi

if have python3 && have perl; then
  pass "python3/perl 可用（mtr 需要）"
else
  warn "缺少 python3 或 perl，mysql-test/mtr 可能不可用"
fi

section "6. 资源限制"

NOFILE="$(ulimit -n 2>/dev/null || echo 0)"
if [ "${NOFILE:-0}" -ge 65535 ] 2>/dev/null; then
  pass "ulimit -n = ${NOFILE}"
else
  warn "ulimit -n = ${NOFILE} 偏低（高连接数需 ≥65535）"
fi

STACK="$(ulimit -s 2>/dev/null || echo unknown)"
info "ulimit -s = ${STACK}"

section "7. 端口与残留进程"

if have ss; then
  for p in "$PORT" "$EXTRA_PORT"; do
    # 先检查 ss 自身退出状态：查询失败时“没有 LISTEN”不能当成端口空闲
    LISTEN_OUT="$(ss -lnt "sport = :$p" 2>&1)"
    SS_RC=$?
    if [ "$SS_RC" -ne 0 ]; then
      warn "端口 $p 无法判定（ss 查询失败 rc=${SS_RC}）：${LISTEN_OUT:-无详细错误}"
    elif printf '%s\n' "$LISTEN_OUT" | grep -q LISTEN; then
      if [ "$p" = "$EXTRA_PORT" ] && [ "$EXTRA_PORT" != "$PORT" ]; then
        warn "救援口 $EXTRA_PORT 已被监听（多实例常见）：显式设置 EXTRA_PORT=<空闲端口> 或确认不需要救援口"
      else
        fail "端口 $p 已被监听（部署会冲突）"
      fi
    else
      pass "端口 $p 未占用"
    fi
  done
else
  warn "缺少 ss，跳过端口检查"
fi

if pgrep -x mariadbd >/dev/null 2>&1 || pgrep -x mysqld >/dev/null 2>&1; then
  warn "检测到已运行的 mariadbd/mysqld：$(pgrep -x mariadbd 2>/dev/null | tr '\n' ' ')$(pgrep -x mysqld 2>/dev/null | tr '\n' ' ')"
else
  pass "无残留 mariadbd/mysqld 进程"
fi

section "8. 时间同步"

if have timedatectl; then
  if timedatectl status 2>/dev/null | grep -qi 'System clock synchronized: yes'; then
    pass "系统时钟已同步"
  else
    warn "系统时钟未确认同步（复制/分布式依赖时钟）"
  fi
else
  info "无 timedatectl，跳过时钟检查"
fi

section "9. 内核与 IO 提示（仅信息）"

for dev in $(df -P "$DATA_ROOT" 2>/dev/null | awk 'NR==2{print $1}'); do
  base="$(basename "$dev")"
  sched="/sys/block/${base}/queue/scheduler"
  [ -r "$sched" ] && info "块设备 ${base} 调度器：$(cat "$sched")"
  nr="/sys/block/${base}/queue/nr_requests"
  [ -r "$nr" ] && info "块设备 ${base} nr_requests：$(cat "$nr")"
done

section "汇总"

printf 'PASS=%d WARN=%d FAIL=%d\n' "$PASS" "$WARN" "$FAIL"
if [ "$FAIL" -gt 0 ]; then
  echo "结果：不通过（存在 FAIL，修复后重跑）"
  exit 1
fi
echo "结果：通过（WARN 项请确认影响后再部署）"
exit 0

