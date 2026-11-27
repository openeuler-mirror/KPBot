#!/usr/bin/env bash

# 只读校验构建产物：版本、库依赖、aarch64 特性/指令、关键符号、md5。

# 用法：MYSQL_HOME=/opt/mysql-8.0.25 bash scripts/verify_build_opts.sh

# 退出码：0=完成（有 WARN 仍为 0）；1=关键项缺失。

set -u

MYSQL_HOME="${MYSQL_HOME:-}"
MYSQLD="${MYSQLD:-${MYSQL_HOME}/bin/mysqld}"

fail=0
warn=0
pass() { printf '[PASS] %s\n' "$*"; }
note() { printf '[WARN] %s\n' "$*"; warn=$((warn + 1)); }
bad()  { printf '[FAIL] %s\n' "$*"; fail=$((fail + 1)); }

echo "== verify build options (read-only) =="
if [ ! -x "${MYSQLD}" ]; then
  echo "[FAIL] 未找到可执行 mysqld：${MYSQLD}" >&2
  exit 1
fi
echo "MYSQLD=${MYSQLD}"

# 关键检查：既看命令退出码，也看输出；不能把错误当 PASS。
version_out="$("${MYSQLD}" --version 2>&1)"
version_rc=$?
if [ "${version_rc}" -ne 0 ] || [ -z "${version_out}" ]; then
  bad "mysqld --version 执行失败或输出为空（rc=${version_rc}；架构/动态库不匹配？）：$(printf '%s' "${version_out}" | head -n1)"
else
  pass "版本：$(printf '%s\n' "${version_out}" | head -n1)"
fi

md5_out="$(md5sum "${MYSQLD}" 2>/dev/null | awk '{print $1}')"
if [ -n "${md5_out}" ]; then
  pass "md5：${md5_out}"
else
  bad "md5sum 失败或无输出"
fi
pass "大小：$(du -h "${MYSQLD}" 2>/dev/null | awk '{print $1}')"

echo
echo "-- ldd --"
if command -v ldd >/dev/null 2>&1; then
  ldd_out="$(ldd "${MYSQLD}" 2>&1)"
  ldd_rc=$?
  printf '%s\n' "${ldd_out}" | sed -n '1,40p'
  if [ "${ldd_rc}" -ne 0 ]; then
    bad "ldd 执行失败（rc=${ldd_rc}）：$(printf '%s' "${ldd_out}" | head -n1)"
  elif printf '%s\n' "${ldd_out}" | grep -q 'not found'; then
    bad "存在未解析的动态库"
  else
    pass "动态库可解析"
  fi
else
  bad "缺少 ldd，无法校验动态库依赖"
fi

echo
echo "-- aarch64 特性 --"
arch="$(uname -m)"
if [ "${arch}" = "aarch64" ]; then
  if command -v readelf >/dev/null 2>&1; then
    readelf -A "${MYSQLD}" 2>/dev/null | sed -n '1,40p'
  else
    note "缺少 readelf"
  fi
  if command -v objdump >/dev/null 2>&1; then
    lse_count="$(objdump -d "${MYSQLD}" 2>/dev/null | grep -cE '\b(ldadd|ldclr|ldset|ldeor|swp)[a-z]*\b' || true)"
    echo "[INFO] LSE 风格原子指令计数（近似）：${lse_count}"
    if [ "${lse_count:-0}" -gt 0 ] 2>/dev/null; then
      pass "检测到 LSE 风格指令（≠ 证明一定启用 +lse）"
    else
      note "未检测到 LSE 风格指令；可能是基线构建或 objdump 不可用"
    fi
    crc_count="$(objdump -d "${MYSQLD}" 2>/dev/null | grep -cE '\b(crc32|crc32c)[a-z]*\b' || true)"
    echo "[INFO] CRC 指令计数（近似）：${crc_count}"
  else
    note "缺少 objdump"
  fi
else
  note "当前架构 ${arch} 非 aarch64，跳过 ARM 特性检查"
fi

echo
echo "-- 关键符号 --"
if command -v nm >/dev/null 2>&1; then
  for sym in row_search_mvcc btr_cur_search_to_nth_level rec_get_offsets_func; do
    if nm -C "${MYSQLD}" 2>/dev/null | grep -q "${sym}"; then
      pass "符号存在：${sym}"
    else
      note "未找到符号（可能被 strip/内联）：${sym}"
    fi
  done
else
  note "缺少 nm"
fi

echo
echo "-- 调试信息 --"
if command -v readelf >/dev/null 2>&1; then
  if readelf -S "${MYSQLD}" 2>/dev/null | grep -qi '.debug_info'; then
    pass "存在 .debug_info（便于 perf 符号化）"
  else
    note "无 .debug_info；profiling 可能大量 [unknown]"
  fi
fi

echo
echo "== summary: FAIL=${fail} WARN=${warn} =="
[ "$fail" -gt 0 ] && exit 1
echo "完成。编译收益仍需 MTR + 同口径 A/B 复测。"
exit 0
