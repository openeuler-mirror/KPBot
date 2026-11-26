#!/usr/bin/env bash
# verify_build_opts.sh — PostgreSQL 二进制产物身份核验（只读）
#
# 核对：pg_config 解析与 configure 串令牌、LSE 指令计数、nm 符号存在性（字面匹配）。
# 不做性能判断；身份证据以 pg_config --configure / objdump / nm 输出为准。
#
# 信任前提（会执行外部程序）：本脚本以调用方权限执行 pg_config（只执行命令行
# 显式传入的 --pg-config、目标二进制同目录的 pg_config 或 PATH 中的 pg_config；
# PG_CONFIG 环境变量不得单独指定其他可执行文件——多租户/CI 环境下防任意执行）。
#
# 用法:
#   bash verify_build_opts.sh --binary "${PG_BIN}/postgres" \
#     [--pg-config PATH] [--expect-configure-token TOKEN ...] \
#     [--expect-lse] [--lse-min N] [--expect-symbol SYMBOL ...]
#
# 退出码: 0=无 FAIL; 1=存在 FAIL; 2=用法错误（含 PG_CONFIG 指向非同目录程序）

set -u

BINARY=""
PG_CONFIG_ARG=""
EXPECT_LSE=0
LSE_MIN=1
declare -a CONFIG_TOKENS=()
declare -a SYMBOLS=()

usage() { cat <<'EOF'
用法: verify_build_opts.sh --binary <postgres二进制> [选项]

选项:
  --pg-config PATH            pg_config 路径（会作为外部程序执行——跨目录/对照版指定
                              用本参数显式传入；缺省定位二进制同目录，其次 PATH；
                              PG_CONFIG 环境变量仅当恰指向二进制同目录 pg_config
                              时生效，其余取值按用法错误退出 2，不静默回退）
  --expect-configure-token T  configure 串须包含的令牌（可重复，如 flto、--with-icu）。
                              按分词后的完整选项匹配：`flto` 命中 -flto/-flto=N、
                              不命中 -flto-partition=one；后写的 -fno-<flag> 按关闭计
  --expect-lse                要求 LSE 指令计数 ≥ --lse-min
  --lse-min N                 LSE 指令数下限，须正整数（默认 1）
  --expect-symbol SYMBOL      要求 nm -C 输出含该符号（可重复；字面匹配，非正则）
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --binary|--pg-config|--lse-min)
      [ $# -ge 2 ] || { echo "ERROR: $1 需要参数" >&2; usage >&2; exit 2; }
      case "$1" in
        --binary) BINARY="$2" ;;
        --pg-config) PG_CONFIG_ARG="$2" ;;
        --lse-min) LSE_MIN="$2" ;;
      esac
      shift 2 ;;
    --expect-configure-token)
      [ $# -ge 2 ] || { echo "ERROR: $1 需要参数" >&2; usage >&2; exit 2; }
      CONFIG_TOKENS+=("$2"); shift 2 ;;
    --expect-symbol)
      [ $# -ge 2 ] || { echo "ERROR: $1 需要参数" >&2; usage >&2; exit 2; }
      SYMBOLS+=("$2"); shift 2 ;;
    --expect-lse) EXPECT_LSE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: 未知参数: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[ -n "$BINARY" ] || { echo "ERROR: 须指定 --binary" >&2; usage >&2; exit 2; }
[ -x "$BINARY" ] || { echo "ERROR: 二进制不存在或不可执行: ${BINARY}" >&2; exit 2; }
# --lse-min 须正整数：0/负数会让无 LSE 的二进制通过 0≥0 空检查
if ! printf '%s' "$LSE_MIN" | grep -qE '^[1-9][0-9]*$'; then
  echo "ERROR: --lse-min 须为正整数: ${LSE_MIN}" >&2
  usage >&2
  exit 2
fi

PASS_CNT=0; WARN_CNT=0; FAIL_CNT=0
pass() { PASS_CNT=$((PASS_CNT+1)); echo "[PASS] $*"; }
warn() { WARN_CNT=$((WARN_CNT+1)); echo "[WARN] $*"; }
fail() { FAIL_CNT=$((FAIL_CNT+1)); echo "[FAIL] $*"; }

# ---- pg_config 与 configure 串 ----
# pg_config 默认从目标二进制同目录定位：--binary 指向实验版而 PATH 里 pg_config
# 是对照版时，会把对照版 configure 与实验版 md5 记在一起
# 定位优先级（执行外部程序的信任边界）：
#   --pg-config（与调用方同一条命令行的显式意图，直接执行）
#   > PG_CONFIG 环境变量（仅当指向目标二进制同目录的 pg_config 时作为定位提示
#     采用——环境变量不得单独决定被执行的文件：多租户/CI 环境下可能被指向任意
#     可执行文件，含按 CWD 解析的相对路径；A/B 轮要跨目录指定请用 --pg-config）
#   > 二进制同目录 > PATH
if [ -n "${PG_CONFIG_ARG}" ]; then
  PG_CONFIG="${PG_CONFIG_ARG}"   # 命令行显式传入：调用方显式意图，直接采用
elif [ -n "${PG_CONFIG:-}" ]; then
  # 环境变量只接受「目标二进制同目录下的 pg_config」这一种形态（定位提示）；
  # 显式设置了却指向别处/不可用时直接报错退出——静默回退会读到另一套构建的
  # configure 串并与实验版身份记在一起
  if [ "$(basename "${PG_CONFIG}")" = "pg_config" ] \
     && [ "$(dirname "${PG_CONFIG}")" = "$(dirname "${BINARY}")" ] \
     && [ -x "${PG_CONFIG}" ]; then
    :   # 恰为二进制同目录的 pg_config：采用（与未设环境变量的定位结果一致）
  else
    echo "ERROR: PG_CONFIG 环境变量不得指定二进制同目录之外的程序（防任意执行），当前: ${PG_CONFIG}；跨目录显式指定请用 --pg-config" >&2
    exit 2
  fi
else
  PG_CONFIG="$(dirname "${BINARY}")/pg_config"
  [ -x "$PG_CONFIG" ] || PG_CONFIG="$(command -v pg_config || true)"
fi
CONF_STR=""
if command -v "$PG_CONFIG" >/dev/null 2>&1; then
  ver=$("$PG_CONFIG" --version 2>/dev/null || true)
  [ -n "$ver" ] && pass "pg_config: ${ver}" || warn "pg_config --version 无输出"
  CONF_STR=$("$PG_CONFIG" --configure 2>/dev/null || true)
  if [ -n "$CONF_STR" ]; then
    pass "configure 串已读取（$(printf '%s' "$CONF_STR" | wc -c) 字节）"
  else
    warn "pg_config --configure 无输出（令牌核对降级跳过）"
  fi
else
  warn "pg_config 不可用: ${PG_CONFIG}（令牌核对跳过）"
fi
# 令牌核对：先剥 PG configure 串的引号包裹、按空白**分词**，再做完整选项匹配
# （不是子串）——防子串误命中（如要求 flto 时 `-flto-partition=one` 命中而
# -flto 本身未启用）。命中形态：与令牌完全相等，或「令牌=取值」（--prefix=/x、
# -flto=4 均判为启用）；编译器标志的否定形式（如 -fno-lto）按「后写胜」覆盖
# 先前的肯定形式——CFLAGS 里 `-flto ... -fno-lto` 的最终状态是关闭
CONF_WORDS="$(printf '%s' "$CONF_STR" | tr -d "\"'")"
for tok in "${CONFIG_TOKENS[@]}"; do
  base="${tok#-}"
  verdict="missing"
  if [ -n "$base" ]; then
    for w in $CONF_WORDS; do
      case "$w" in
        "$tok"|"-${base}"|"$tok="*|"-${base}="*)  verdict="ok" ;;
      esac
      case "$base" in
        f*)
          case "$w" in
            "fno-${base#f}"|"-fno-${base#f}"|"fno-${base#f}="*|"-fno-${base#f}="*)  verdict="missing" ;;
          esac ;;
      esac
    done
  fi
  if [ "$verdict" = "ok" ]; then
    pass "configure 令牌存在: ${tok}"
  elif [ -z "$CONF_STR" ]; then
    # 显式要求的令牌在无证据时必须 FAIL（否则 A/B 门禁会放行未核实的构建）
    fail "configure 串不可读，显式要求的令牌未核验: ${tok}"
  else
    fail "configure 令牌缺失: ${tok}"
  fi
done

# ---- LSE 指令计数 ----
if [ "$EXPECT_LSE" -eq 1 ]; then
  if command -v objdump >/dev/null 2>&1; then
    # 助记符列匹配：覆盖带内存序后缀的变体（casal/ldaddal/swpal/...），词界锚定助记符
    # 按助记符列（第 2 列，--no-show-raw-insn 格式为 地址:	助记符	操作数）匹配：
    # objdump 行首是十六进制地址，第 1 列不可能匹配助记符——改用 $2 匹配并支持内存序后缀
    LSE_CNT=$(objdump -d --no-show-raw-insn "$BINARY" 2>/dev/null | awk '$2 ~ /^(ldadd|cas|swp|ldclr|ldeor)[a-z]*$/ {c++} END {print c+0}')
    if [ "${LSE_CNT:-0}" -ge "$LSE_MIN" ]; then
      pass "LSE 指令数 ${LSE_CNT} ≥ ${LSE_MIN}"
    else
      fail "LSE 指令数 ${LSE_CNT:-0} < ${LSE_MIN}（检查 -march/lse 或 outline-atomics 口径）"
    fi
  else
    fail "objdump 不可用，无法执行必需的 LSE 检查（--expect-lse 已请求）"
  fi
fi

# ---- 符号存在性（字面匹配）----
if [ "${#SYMBOLS[@]}" -gt 0 ]; then
  if command -v nm >/dev/null 2>&1; then
    for sym in "${SYMBOLS[@]}"; do
      # 提取符号列做子串字面匹配（grep -F）：Foo 命中 FooBar 但不命中独立 Foo 行——
      # operator[] 含正则特殊字符也不报错；含空格的未定义符号列提取需保留完整符号名
      if nm -C "$BINARY" 2>/dev/null \
          | awk '{ if (NF>=3) print substr($0, index($0,$3)); else if (NF==2) print $2 }' \
          | grep -qF -- "$sym"; then
        pass "符号存在: ${sym}"
      else
        fail "符号缺失: ${sym}"
      fi
    done
  else
    for sym in "${SYMBOLS[@]}"; do
      fail "nm 不可用，无法执行必需的符号检查: ${sym}"
    done
  fi
fi

# ---- 身份记录 ----
if command -v md5sum >/dev/null 2>&1; then
  echo "       md5: $(md5sum "$BINARY" | awk '{print $1}')"
fi

echo "------------------------------------------------------------"
echo "Summary: PASS=${PASS_CNT} WARN=${WARN_CNT} FAIL=${FAIL_CNT}"
[ "$FAIL_CNT" -eq 0 ] && exit 0 || exit 1
