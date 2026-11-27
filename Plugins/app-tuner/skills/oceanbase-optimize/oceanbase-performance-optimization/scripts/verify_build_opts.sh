#!/usr/bin/env bash

# 只读校验 observer 二进制的编译身份：编译器、LSE、符号/段、md5/大小。

#

# 用法：

# bash verify_build_opts.sh <observer_binary> [<baseline_binary>]

#

# 可选环境变量：

# LSE_PATTERN='ldadd|ldclr|ldset|swp|cas'   # AArch64 LSE 助记符（[a-z]* 覆盖 casal/ldaddal 等内存序后缀）

# TOP_N=20                                  # 热点符号展示数量

#

# 不修改二进制，不上传任何内容。

# 注意：LSE 指令计数与指令分布只在 AArch64 上有意义；x86-64 等架构的 objdump
# 第 3 列是十六进制字节而非助记符，脚本在入口探测架构后跳过这两项。

set -u
set -o pipefail

if [ "$#" -gt 2 ]; then
  echo "FAIL 多余参数：$3（本脚本仅接受 <observer_binary> [<baseline_binary>]）" >&2
  exit 2
fi

BIN="${1:-}"
BASE="${2:-}"

if [ -z "${BIN}" ]; then
  echo "用法：bash verify_build_opts.sh <observer_binary> [<baseline_binary>]" >&2
  exit 2
fi
if [ ! -f "${BIN}" ]; then
  echo "FAIL 文件不存在：${BIN}" >&2
  exit 2
fi

# 显式传入基线时必须存在且可读；否则不能把单文件记录当成已完成的 A/B 校验
BASE_EXPLICIT=0
if [ "$#" -ge 2 ]; then
  BASE_EXPLICIT=1
  if [ -z "${BASE}" ] || [ ! -f "${BASE}" ]; then
    echo "FAIL 基线文件不存在：${BASE:-<空>}" >&2
    exit 2
  fi
  if [ ! -r "${BASE}" ]; then
    echo "FAIL 基线文件不可读：${BASE}" >&2
    exit 2
  fi
fi

TOP_N="${TOP_N:-20}"
LSE_PATTERN="${LSE_PATTERN:-ldadd|ldclr|ldset|swp|cas}"

need_cmd() { command -v "$1" >/dev/null 2>&1; }

# 入口架构探测：LSE/指令分布是 AArch64-only 指标；非 AArch64 不做助记符列统计。
ARCH="unknown"
if need_cmd readelf; then
  MACHINE="$(readelf -h "${BIN}" 2>/dev/null | awk -F: '/Machine:/{gsub(/[[:space:]]/,"",$2); print $2}')" || MACHINE=""
  case "${MACHINE}" in
    *aarch64*|*AArch64*) ARCH="aarch64" ;;
    *X86-64*|*x86-64*|*AMD*) ARCH="x86_64" ;;
  esac
fi
if [ "${ARCH}" = "unknown" ] && need_cmd file; then
  FILE_DESC="$(file -b "${BIN}" 2>/dev/null)" || FILE_DESC=""
  case "${FILE_DESC}" in
    *aarch64*|*AArch64*) ARCH="aarch64" ;;
    *x86-64*|*x86_64*|*Intel*|*AMD*) ARCH="x86_64" ;;
  esac
fi
if [ "${ARCH}" = "aarch64" ]; then
  AARCH64=1
  echo "INFO  arch=aarch64（LSE/指令分布统计适用）"
else
  AARCH64=0
  echo "INFO  arch=${ARCH}（非 AArch64：LSE 计数与指令分布仅 AArch64 适用，跳过）"
fi

# 一次流式 objdump 同时产出聚合指标：内联 LSE 指令数 + 助记符频次。
# 只输出聚合行，不把整份反汇编（可能数百 MB）捕获进 shell 变量。
# 排除 __aarch64_*_relax 桩：GCC 在 -march=armv8-a（基线 ISA）下用这些运行时桩
# 实现原子操作，桩体里的 ldadd 等指令不是内联 LSE，会把 NOLSE 误判成 LSE。
# 配合 pipefail：反汇编失败时返回非零，不能把失败的计数当成 0 命中/NOLSE。
disassemble_metrics() {
  local target="$1"
  objdump -d "${target}" 2>/dev/null | awk -v pat="^(${LSE_PATTERN})[a-z]*$" '
    /<[^>]+>:/ { skip = ($0 ~ /__aarch64_[^>]*_relax/); next }
    /^[[:space:]]+[0-9a-f]+:/ {
      if (!skip && $3 ~ pat) lse++
      mnemonic[$3]++
    }
    END {
      printf "LSE %d\n", lse+0
      for (m in mnemonic) printf "OP %s %d\n", m, mnemonic[m]
    }'
}

echo "== OceanBase binary identity (read-only) =="
echo "binary : ${BIN}"
if need_cmd ls; then ls -lh "${BIN}" | sed 's/^/INFO  /'; fi

if need_cmd md5sum; then
  md5sum "${BIN}" | sed 's/^/INFO  md5 /'
elif need_cmd shasum; then
  shasum -a 256 "${BIN}" | sed 's/^/INFO  sha256 /'
fi

if need_cmd file; then
  file "${BIN}" | sed 's/^/INFO  /'
fi

if need_cmd readelf; then
  echo "-- compiler (.comment) --"
  readelf -p .comment "${BIN}" 2>/dev/null | sed 's/^/INFO  /' || echo "INFO  no .comment"
  echo "-- build-id --"
  readelf -n "${BIN}" 2>/dev/null | awk '/Build ID/{print "INFO  " $0}'
fi

if need_cmd ldd; then
  echo "-- dynamic deps --"
  ldd "${BIN}" 2>/dev/null | sed 's/^/INFO  /' || echo "INFO  static or ldd failed"
fi

METRICS=""
COUNT=""
if need_cmd objdump; then
  if [ "${AARCH64}" -eq 1 ]; then
    echo "-- LSE/atomic instruction scan (AArch64 only, outline-atomics stubs excluded) --"
    # BIN 只反汇编一次：LSE 计数与指令分布复用同一份流式聚合结果。
    if METRICS="$(disassemble_metrics "${BIN}")"; then
      COUNT="$(printf '%s\n' "${METRICS}" | awk '$1=="LSE"{print $2}')"
      COUNT="${COUNT:-0}"
      echo "INFO  inline_lse_insn=${COUNT}"
      if [ "${COUNT}" -ge 1000 ] 2>/dev/null; then
        echo "INFO  hint: 千级以上命中通常表示内联 LSE 构建；仍需结合 -march 构建记录确认"
      else
        echo "INFO  hint: 命中很少（可能为个位数桩残留/未匹配到）：不要仅凭 >0 判为 LSE"
      fi
      echo "-- instruction mix (top opcodes, AArch64 only) --"
      printf '%s\n' "${METRICS}" | awk '$1=="OP"{print $2, $3}' \
        | sort -k2,2nr | head -n 10 | sed 's/^/INFO  /'
    else
      echo "WARN  反汇编失败：无法统计原子指令，不据此判断 NOLSE（确认 objdump 支持目标架构）"
    fi
  else
    echo "-- LSE/atomic instruction scan: SKIPPED（非 AArch64，该指标不适用） --"
    echo "-- instruction mix: SKIPPED（非 AArch64，objdump 第 3 列不是助记符） --"
  fi
fi

if need_cmd nm; then
  echo "-- key symbols --"
  # nm -C 会输出 oceanbase::observer::ObServer::… 这类限定名：按 C++ 限定名边界匹配，
  # 不要求符号名前是空格（::ObServer 也应算命中）；各符号复用同一份 nm 输出交叉核对
  NM_OUT="$(nm -C "${BIN}" 2>/dev/null || true)"
  for s in ObServer ObMultiTenant ObTenant ObLogApplyService ObPlanCache ObMemtable ObSSTable; do
    if printf '%s\n' "${NM_OUT}" | grep -Eq "(^|[^[:alnum:]_])${s}([^[:alnum:]_]|$)"; then
      echo "INFO  found: ${s}"
    else
      echo "INFO  missing: ${s}（可能被 strip/内联/版本差异）"
    fi
  done
  echo "-- largest symbols (top ${TOP_N}) --"
  nm -S --size-sort "${BIN}" 2>/dev/null | tail -n "${TOP_N}" | sed 's/^/INFO  /' || true
fi

if need_cmd readelf; then
  echo "-- sections --"
  readelf -S "${BIN}" 2>/dev/null | awk '/[[ 0-9]+]/{print $2, $3}' | sed 's/^/INFO  /' | head -n 40
  if readelf -S "${BIN}" 2>/dev/null | grep -q ".bolt"; then
    echo "INFO  BOLT section detected"
  else
    echo "INFO  no .bolt section（BOLT 也可能不留下同名段，以构建记录为准）"
  fi
fi

if [ "${BASE_EXPLICIT}" -eq 1 ]; then
  echo "== compare with baseline =="
  echo "baseline: ${BASE}"
  if need_cmd md5sum; then md5sum "${BASE}" 2>/dev/null | sed 's/^/INFO  /'; fi
  B1="$(stat -c %s "${BIN}" 2>/dev/null || echo 0)"
  B2="$(stat -c %s "${BASE}" 2>/dev/null || echo 0)"
  echo "INFO  size current=${B1} baseline=${B2}"
  if need_cmd objdump; then
    if [ "${AARCH64}" -eq 1 ]; then
      # 复用当前二进制已缓存的 COUNT，不再重复反汇编 BIN；基线只反汇编一次。
      if [ -n "${COUNT}" ]; then
        if BASE_METRICS="$(disassemble_metrics "${BASE}")"; then
          C2="$(printf '%s\n' "${BASE_METRICS}" | awk '$1=="LSE"{print $2}')"
          echo "INFO  inline_lse_insn current=${COUNT} baseline=${C2:-0}（均已排除 outline 桩）"
        else
          echo "WARN  基线反汇编失败：跳过原子指令对比，不能据此判断两者身份一致"
        fi
      else
        echo "WARN  当前二进制反汇编失败/未统计：跳过原子指令对比"
      fi
    else
      echo "INFO  atomic_insn 对比跳过（非 AArch64，指标不适用）"
    fi
  fi
fi

cat <<'NOTE'
== 记录模板 ==
source/commit :
deps manifest :
compiler      : (看 .comment)
-march / LSE  : (看构建记录 + inline_lse_insn 数量级，排除 outline 桩后判定)
LTO/AutoFDO   : (构建记录)
HOTFUNC/BOLT  : (构建记录；BOLT 开源树还需 OB_BUILD_OPENSOURCE=OFF)
md5/size      : (本脚本输出；md5 与基线一致即说明构建开关未生效)
NOTE
exit 0
