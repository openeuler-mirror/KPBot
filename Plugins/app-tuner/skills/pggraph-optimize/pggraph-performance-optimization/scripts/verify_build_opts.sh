#!/usr/bin/env bash
# Read-only check of pgGraph build options and resulting binary features.
# Usage:
#   PGGraph_SRC=/opt/pggraph/pgGraph-0.1.8 PGSQL_PREFIX=/opt/pgsql/18.3 \
#     bash verify_build_opts.sh            # 只读巡检：环境缺失时优雅降级提示，恒 exit 0
#   bash verify_build_opts.sh --strict     # 门禁：pg_config/graph.so/objdump 任一缺失 → exit 1
set -u

PGGraph_SRC="${PGGraph_SRC:-.}"
PGSQL_PREFIX="${PGSQL_PREFIX:-}"
PG_CONFIG="${PG_CONFIG:-${PGSQL_PREFIX}/bin/pg_config}"

say() { printf '%s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

# 参数解析：仅接受 --strict / -h / --help，未知或多余参数一律 exit 2——
# 防笔误（如 --strcit）、错位参数（foo --strict）或 --strict 后跟多余参数
# （--strict foo）无声绕过/污染门禁
STRICT=0
case "${1:-}" in
  "") ;;
  --strict)
    if [ -n "${2:-}" ]; then
      echo "ERROR: --strict 不接受额外参数: $2" >&2; exit 2
    fi
    STRICT=1 ;;
  -h|--help)
    sed -n '2,10p' "$0"; exit 0 ;;
  *) echo "ERROR: 未知参数: $1（仅支持 --strict）" >&2; exit 2 ;;
esac

say "== pgGraph build options =="

# 直接校验解析出的 PG_CONFIG：只设 PG_CONFIG（PGSQL_PREFIX 留空）时也应判可用
if [ -x "${PG_CONFIG}" ]; then
  say "pg_config: $("${PG_CONFIG}" --version 2>/dev/null || true)"
  LIBDIR="$("${PG_CONFIG}" --pkglibdir 2>/dev/null || true)"
else
  say "pg_config: NOT FOUND (set PGSQL_PREFIX or PG_CONFIG; current PG_CONFIG=${PG_CONFIG})"
  LIBDIR=""
fi

say ""
say "-- source config --"
for f in "${PGGraph_SRC}/graph/Cargo.toml" "${PGGraph_SRC}/graph/.cargo/config.toml" "${PGGraph_SRC}/graph/rust-toolchain.toml"; do
  if [ -f "${f}" ]; then
    say "# ${f}"
    grep -nE 'target-cpu|target-feature|rustflags|lto|codegen-units|opt-level|channel|rust-version|pgrx' "${f}" 2>/dev/null || true
  else
    say "# missing: ${f}"
  fi
done

say ""
say "-- rustc --"
if have rustc; then
  rustc --version
  rustc --print target-features 2>/dev/null | grep -E 'sve|sve2|neon|lse|crc|fp16' | head -20 || true
else
  say "rustc not found"
fi

say ""
say "-- binary --"
SO=""
if [ -n "${LIBDIR}" ] && [ -f "${LIBDIR}/graph.so" ]; then
  SO="${LIBDIR}/graph.so"
elif [ -f "${PGGraph_SRC}/graph/target/release/libgraph.so" ]; then
  SO="${PGGraph_SRC}/graph/target/release/libgraph.so"
fi

if [ -n "${SO}" ]; then
  say "so: ${SO}"
  have sha256sum && sha256sum "${SO}" || true
  if have llvm-objdump; then
    DUMP="llvm-objdump"
  elif have objdump; then
    DUMP="objdump"
  else
    DUMP=""
  fi
  if [ -n "${DUMP}" ]; then
    say "disassembler: ${DUMP}"
    # 先确认反汇编可识别目标架构：宿主 objdump 遇到不识别架构时退出非零且输出为空，
    # 此时 grep 计到 0 会被误报成“无 SVE/NEON 指令”
    DISASM_OK=0
    if dis_asm="$("${DUMP}" -d "${SO}" 2>&1)"; then
      DISASM_OK=1
      # Counts are heuristic; instruction naming differs by disassembler.
      sve="$(printf '%s\n' "${dis_asm}" | grep -cE '\b(ld1w|st1w|ptrue|whilelo|uaddv|smaxv|sminv|sel)\b' || true)"
      neon="$(printf '%s\n' "${dis_asm}" | grep -cE '\b(ld1|st1|addv|umaxv|uminv|tbl)\b' || true)"
      say "heuristic SVE-ish instruction count: ${sve}"
      say "heuristic NEON-ish instruction count: ${neon}"
      say "NOTE: release LTO may inline; counts are for orientation only."
    else
      say "cannot disassemble ${SO} with ${DUMP}（架构不受支持？）；指令计数无法判定，不输出 0 冒充结论"
    fi
  else
    say "no objdump/llvm-objdump; skip instruction counts"
  fi
else
  say "graph.so not found; build first or set PGSQL_PREFIX"
fi

say ""
say "== done (read-only) =="

# --strict 门禁语义：显式要求的环境证据缺失（pg_config/graph.so 不可用、无法反汇编）时
# 以非零退出，供 CI/agent 当 A/B 门禁使用；缺省保持只读巡检（恒 0，缺失降级为提示）。
if [ "${STRICT}" = 1 ]; then
  if [ -z "${PG_CONFIG}" ] || [ ! -x "${PG_CONFIG}" ]; then
    say "STRICT-FAIL: pg_config 不可用，无法核验构建选项"
    exit 1
  fi
  if [ "${DISASM_OK:-0}" != 1 ] && [ -n "${SO:-}" ] && [ -n "${DUMP:-}" ]; then
    say "STRICT-FAIL: ${DUMP} 无法反汇编 ${SO}（架构不受支持/退出非零），指令计数证据缺失"
    exit 1
  fi
  if [ -z "${LIBDIR:-}" ] || [ ! -f "${LIBDIR}/graph.so" ]; then
    say "STRICT-FAIL: graph.so 缺失（build first or set PGSQL_PREFIX）"
    exit 1
  fi
  if ! have objdump && ! have llvm-objdump; then
    say "STRICT-FAIL: 无 objdump/llvm-objdump，指令计数证据缺失"
    exit 1
  fi
  say "STRICT-PASS"
fi
exit 0
