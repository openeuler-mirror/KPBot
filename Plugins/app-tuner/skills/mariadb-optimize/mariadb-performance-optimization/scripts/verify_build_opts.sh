#!/usr/bin/env bash

# MariaDB 构建与运行参数只读校验。

#

# 用法：

# MARIADB_HOME=/opt/mariadb/10.11.16 bash scripts/verify_build_opts.sh

# bash scripts/verify_build_opts.sh --binary /path/to/mariadbd --socket /tmp/mysql.sock

#

# 只读：不修改配置、不写数据库。输出 PASS/WARN/INFO；不因 WARN 失败。

# 依赖：coreutils、binutils（objdump）、可选 ldd/lsof/numastat、mariadb 客户端。

set -u

MARIADB_HOME="${MARIADB_HOME:-/opt/mariadb/10.11.16}"
BINARY="${MARIADB_HOME}/bin/mariadbd"
SOCKET="${SOCKET:-}"
MARIADB_BIN="${MARIADB_BIN:-${MARIADB_HOME}/bin/mariadb}"
MARIADB_DEFAULTS_FILE="${MARIADB_DEFAULTS_FILE:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --binary) shift; BINARY="${1:-}" ;;
    --socket) shift; SOCKET="${1:-}" ;;
    -h|--help)
      echo "用法: verify_build_opts.sh [--binary PATH] [--socket PATH]"
      echo "环境变量: MARIADB_HOME / MARIADB_BIN / MARIADB_DEFAULTS_FILE / SOCKET"
      exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
  shift
done

PASS=0; WARN=0; INFO=0
pass() { printf 'PASS  %s\n' "$*"; PASS=$((PASS + 1)); }
warn() { printf 'WARN  %s\n' "$*"; WARN=$((WARN + 1)); }
info() { printf 'INFO  %s\n' "$*"; INFO=$((INFO + 1)); }
section() { printf '\n== %s ==\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

# 客户端连接参数：--socket 指向目标实例，多实例时不再用 pgrep 猜 PID
ARGS=()
[ -n "$MARIADB_DEFAULTS_FILE" ] && ARGS+=(--defaults-extra-file="$MARIADB_DEFAULTS_FILE")
ARGS+=(--batch --skip-column-names)
[ -n "$SOCKET" ] && ARGS+=(--socket="$SOCKET")

# 只读查询：查询结果与错误分开保存，错误绝不冒充成空值
Q_OUT=""; Q_ERR=""; Q_RC=0
query() {
  local errfile
  errfile="$(mktemp 2>/dev/null)" || errfile=""
  if [ -n "$errfile" ]; then
    Q_OUT="$("$MARIADB_BIN" "${ARGS[@]}" -e "$1" 2>"$errfile")"
    Q_RC=$?
    Q_ERR="$(tr '\n' ' ' < "$errfile" 2>/dev/null || true)"
    rm -f "$errfile"
  else
    Q_OUT="$("$MARIADB_BIN" "${ARGS[@]}" -e "$1" 2>/dev/null)"
    Q_RC=$?
    Q_ERR=""
  fi
  return "$Q_RC"
}

# 普通/数值型系统变量：查询失败单独报告，不把空值当成 0 或“非 1”
VVAR=""
qvar() {
  local label="$2"
  query "SELECT @@$1"
  if [ "$Q_RC" -ne 0 ] || [ -z "$Q_OUT" ]; then
    warn "$label 查询失败（未据此判定）：$Q_ERR"
    return 1
  fi
  VVAR="$(printf '%s\n' "$Q_OUT" | head -1)"
  info "$label = $VVAR"
  return 0
}

# 10.11 已移除的变量：区分“未知变量（版本不支持）”与“查询失败”
qv_removed() {
  local label="$2"
  query "SELECT @@$1"
  if [ "$Q_RC" -eq 0 ] && [ -n "$Q_OUT" ]; then
    info "$label = $(printf '%s\n' "$Q_OUT" | head -1)（本版本仍支持；10.11+ 已移除，勿写入新 my.cnf）"
  elif printf '%s' "$Q_ERR" | grep -qi 'unknown system variable'; then
    info "$label = 本版本已移除（仅 10.5.1 及更早的功能版本支持；10.5.2 起弃用并忽略，10.6.0 起移除，勿写入 my.cnf）"
  else
    warn "$label 查询失败（无法判定是否支持）：$Q_ERR"
  fi
}

DB_OK=0
if [ -x "$MARIADB_BIN" ]; then
  query 'SELECT 1'
  if [ "$Q_RC" -eq 0 ] && [ "$(printf '%s\n' "$Q_OUT" | head -1)" = "1" ]; then
    DB_OK=1
  else
    info "无法连接指定实例：$(printf '%s' "$Q_ERR" | sed 's/[[:space:]]*$//')；运行期 PID 与参数检查将标注失败原因"
  fi
fi

# 从指定连接确认目标 PID；多实例且无法确认时返回空，调用方跳过对应检查
TARGET_PID=""; MULTI_INSTANCE=0
resolve_target_pid() {
  local procs n pid pidfile comm line cand found srv_sock
  [ "$DB_OK" -eq 1 ] || return 0
  procs="$(pgrep -x mariadbd 2>/dev/null || true)"
  n="$(printf '%s\n' "$procs" | awk 'NF{n++} END{print n+0}')"
  [ "$n" -gt 1 ] && MULTI_INSTANCE=1
  [ "$n" -eq 0 ] && return 0

  # 指定 socket 时先与服务端 @@socket 交叉核对，避免连到别的实例
  if [ -n "$SOCKET" ]; then
    query 'SELECT @@socket'
    [ "$Q_RC" -eq 0 ] && [ -n "$Q_OUT" ] || return 0
    srv_sock="$(printf '%s\n' "$Q_OUT" | head -1)"
    if [ "$srv_sock" != "$SOCKET" ] && \
       [ "$(basename "$srv_sock")" != "$(basename "$SOCKET")" ]; then
      return 0
    fi
  fi

  # 优先读该连接的 pid_file；不可读时按 socket 反查 /proc，要求唯一候选
  query 'SELECT @@pid_file'
  pidfile="$(printf '%s\n' "$Q_OUT" | head -1)"
  if [ -n "$pidfile" ] && [ -r "$pidfile" ]; then
    pid="$(head -1 "$pidfile" 2>/dev/null || true)"
    case "$pid" in ''|*[!0-9]*) pid="" ;; esac
    comm="$(cat "/proc/$pid/comm" 2>/dev/null || true)"
    if [ -n "$pid" ] && [ "$comm" = "mariadbd" ]; then
      TARGET_PID="$pid"
      return 0
    fi
  fi
  found=""
  for cand in /proc/[0-9]*; do
    [ -r "$cand/comm" ] || continue
    [ "$(cat "$cand/comm" 2>/dev/null || true)" = "mariadbd" ] || continue
    line="$(tr '\0' ' ' < "$cand/cmdline" 2>/dev/null || true)"
    if [ -n "$SOCKET" ] && ! printf '%s' "$line" | grep -Fq -- "$SOCKET"; then
      continue
    fi
    [ -z "$found" ] || return 0   # 多个候选，无法确认目标实例
    found="${cand#/proc/}"
  done
  TARGET_PID="$found"
}
resolve_target_pid

section "1. 二进制与版本"

if [ -x "$BINARY" ]; then
  pass "二进制存在：$BINARY"
  V="$("$BINARY" --version 2>/dev/null || true)"
  [ -n "$V" ] && info "$(printf '%s' "$V" | tr '\n' ' ')"
  # --version 形如 "<程序名>  Ver 15.1 Distrib 10.11.16-MariaDB, ..."：
  # 必须取 Distrib 后的服务端版本，不能对整行匹配 10.11.*
  VSERVER="$(printf '%s\n' "$V" | sed -n 's/.*Distrib[[:space:]]\{1,\}\([0-9][0-9.]*\).*/\1/p' | head -1)"
  case "$VSERVER" in
    10.11.*) pass "服务端版本匹配 10.11 基线（$VSERVER）" ;;
    '') warn "无法从 --version 提取服务端版本（请人工核对）" ;;
    *) warn "服务端版本不是 10.11（$VSERVER），结论需按目标版本校准" ;;
  esac
  info "大小：$(du -h "$BINARY" | awk '{print $1}')"
  info "md5：$(md5sum "$BINARY" | awk '{print $1}')"
else
  warn "未找到二进制：$BINARY"
fi

section "2. 架构与指令集"

ARCH="$(uname -m)"
info "运行架构：$ARCH"

if [ -x "$BINARY" ] && have objdump; then
  # 与 references/compiler-thirdparty-and-source.md §1.2 同一检查入口：
  # 按反汇编助记符列（$3）匹配，覆盖 casal/ldaddal/swpal 等内存序后缀，
  # 并覆盖 ldset/ldclr/ldeor/ldsmax/ldsmin/ldumax/ldumin 及 st 前缀族等完整 LSE 家族；
  # 先判 objdump 退出码，失败不能计成“零命中”。
  DIS="$(mktemp 2>/dev/null)" || DIS=""
  if [ -n "$DIS" ] && objdump -d "$BINARY" >"$DIS" 2>/dev/null; then
    LSE_RE='^(cas|ldadd|ldclr|ldeor|ldset|ldsmax|ldsmin|ldumax|ldumin|swp|stadd|stclr|steor|stset|stsmax|stsmin|stumax|stumin)[a-z]*$'
    CAS="$(awk -v re="$LSE_RE" '$3 ~ re' "$DIS" | wc -l)"
    info "objdump 中 LSE 指令族（cas/ldadd/ldclr/ldeor/ldset/ldsmax/ldsmin/ldumax/ldumin/swp/st*，含后缀）命中：${CAS:-0}"
    if [ "${CAS:-0}" -gt 0 ]; then
      info "提示：指令存在不等于热点命中；LSE 收益需目标负载 A/B"
    else
      info "扫描成功但零命中（可能未启用 -march=... +lse，或工具链未生成）"
    fi
  else
    warn "objdump 扫描失败（无法反汇编），不能按零命中判定；请检查二进制/工具链"
  fi
  [ -n "$DIS" ] && rm -f "$DIS"
else
  warn "缺少 objdump 或二进制，跳过指令检查"
fi

section "3. 分配器与动态库"

if [ -x "$BINARY" ] && have ldd; then
  ALLOC="$(ldd "$BINARY" 2>/dev/null | grep -Ei 'jemalloc|tcmalloc' || true)"
  if [ -n "$ALLOC" ]; then
    pass "链接分配器：$(printf '%s' "$ALLOC" | tr '\n' ' ')"
  else
    info "未链接第三方分配器（使用系统 malloc）"
  fi
  ldd "$BINARY" 2>/dev/null | grep -Ei 'ssl|crypto' | sed 's/^/INFO  /' || true
else
  warn "缺少 ldd 或二进制，跳过库检查"
fi

if [ -z "$TARGET_PID" ] && [ "$MULTI_INSTANCE" -eq 1 ]; then
  info "多个 mariadbd 实例且无法从指定连接确认目标 PID，跳过运行期分配器检查（请传 --socket 并保证 @@pid_file 可读）"
elif [ -n "$TARGET_PID" ] && have lsof; then
  PRELOAD="$(lsof -p "$TARGET_PID" 2>/dev/null | grep -Ei 'jemalloc|tcmalloc' || true)"
  if [ -n "$PRELOAD" ]; then
    info "运行期实际加载（PID=$TARGET_PID）：$(printf '%s' "$PRELOAD" | awk '{print $NF}' | sort -u | tr '\n' ' ')"
    if printf '%s' "$PRELOAD" | grep -qi jemalloc && printf '%s' "$PRELOAD" | grep -qi tcmalloc; then
      warn "同时出现 jemalloc 与 tcmalloc（应只启用一种）"
    fi
  else
    info "运行期未见第三方分配器（PID=$TARGET_PID）"
  fi
else
  info "未确认目标实例 PID（或缺少 lsof），跳过运行期分配器检查"
fi

section "4. 关键运行参数（只读）"

if [ "$DB_OK" -eq 1 ]; then
  pass "可连接数据库"
  query 'SELECT VERSION()'
  [ "$Q_RC" -eq 0 ] && info "version = $(printf '%s\n' "$Q_OUT" | head -1)"
  qvar innodb_buffer_pool_size buffer_pool_size
  qv_removed innodb_buffer_pool_instances buffer_pool_instances
  qvar innodb_log_file_size log_file_size
  qvar innodb_flush_method flush_method
  qvar innodb_io_capacity io_capacity
  qv_removed innodb_page_cleaners page_cleaners
  qvar thread_handling thread_handling
  qvar thread_pool_size thread_pool_size
  qvar table_open_cache table_open_cache
  qvar aria_pagecache_buffer_size aria_pagecache
  qvar binlog_format binlog_format
  qvar performance_schema performance_schema
  qvar innodb_numa_interleave numa_interleave

  # 目标版本 innodb_doublewrite 返回数值 0(OFF)/1(ON)，不是 OFF/ON 字符串；
  # 查询失败单独报告，不按 0 或“非 1”误判。
  if qvar innodb_doublewrite doublewrite; then
    case "$VVAR" in
      0) warn "doublewrite=0(OFF)：写性能可能提升但崩溃恢复风险增加" ;;
      1) : ;;
      *) warn "doublewrite=$VVAR（非 0/1），请按目标版本核对取值" ;;
    esac
  fi
  if qvar innodb_flush_log_at_trx_commit flush_log_at_trx_commit; then
    [ "$VVAR" = "1" ] || warn "innodb_flush_log_at_trx_commit=$VVAR：可能丢失最近事务"
  fi
  if qvar sync_binlog sync_binlog; then
    [ "$VVAR" = "1" ] || warn "sync_binlog=$VVAR：崩溃可能丢 binlog"
  fi
elif [ -x "$MARIADB_BIN" ]; then
  info "无法连接（未运行或缺少凭据），跳过参数读取"
else
  warn "客户端不存在：$MARIADB_BIN"
fi

section "5. OS 相关（只读）"

info "governor: $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo unknown)"
info "THP: $(cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null || echo unknown)"
info "swappiness: $(cat /proc/sys/vm/swappiness 2>/dev/null || echo unknown)"
info "dirty_ratio: $(cat /proc/sys/vm/dirty_ratio 2>/dev/null || echo unknown)"
info "nr_hugepages: $(cat /proc/sys/vm/nr_hugepages 2>/dev/null || echo unknown)"
if [ -n "$TARGET_PID" ] && have numastat; then
  info "numastat（目标 PID=$TARGET_PID）："
  numastat -p "$TARGET_PID" 2>/dev/null | sed 's/^/INFO  /' || true
elif [ "$MULTI_INSTANCE" -eq 1 ]; then
  info "多个 mariadbd 实例且目标 PID 无法确认，跳过 numastat（避免取错实例）"
fi

section "汇总"
printf 'PASS=%d WARN=%d INFO=%d\n' "$PASS" "$WARN" "$INFO"
echo "提示：本脚本只做只读核对，不代表性能结论；收益必须由同口径 A/B 证明。"
exit 0
