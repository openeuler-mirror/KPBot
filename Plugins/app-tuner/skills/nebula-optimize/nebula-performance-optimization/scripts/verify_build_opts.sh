#!/usr/bin/env bash
# verify_build_opts.sh — 只读校验 Nebula 二进制的编译优化是否生效
#
# 用法:
#   bash verify_build_opts.sh [--bin-dir DIR] [--binary NAME] [--expect-jemalloc VER]
#                             [--expect-lse] [--expect-symbol SYMBOL]
#
# 默认:
#   --bin-dir   ./build/bin
#   --binary    nebula-graphd
#
# 检查:
#   1. 文件存在、类型、大小、md5
#   2. jemalloc 版本串（若可读）
#   3. LSE 指令计数（ldadd/ldset/ldclr/ldeor/cas/swp）
#   4. 关键符号（可选，多次 --expect-symbol）
#   5. 动态依赖（ldd）
#
# 只读；不修改/不运行 Nebula。存在 FAIL 时退出码非零。
set -u

usage() {
  cat <<'EOF'
用法: verify_build_opts.sh [选项]

  --bin-dir DIR            二进制目录，默认 ./build/bin
  --binary NAME            二进制名，默认 nebula-graphd
  --expect-jemalloc VER    期望 jemalloc 版本串包含的字符串（如 5.3.1）
  --expect-lse             要求 LSE 指令数 > 0
  --expect-symbol SYMBOL   要求 nm 中存在该符号（可重复；字面匹配反修饰后的符号名，非正则）
  --lse-min N              LSE 指令数下限，隐含 --expect-lse，默认 1
  -h, --help               显示帮助

示例:
  bash verify_build_opts.sh --bin-dir build_opt/bin --expect-jemalloc 5.3.1 --expect-lse
  bash verify_build_opts.sh --binary nebula-storaged --expect-symbol findFirstNull
EOF
}

BIN_DIR="./build/bin"
BINARY="nebula-graphd"
EXPECT_JEMALLOC=""
EXPECT_LSE=0
LSE_MIN=1
SYMBOLS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --bin-dir|--binary|--expect-jemalloc|--lse-min|--expect-symbol)
      [ $# -ge 2 ] || { echo "ERROR: $1 缺少参数值" >&2; usage >&2; exit 2; }
      case "$1" in
        --bin-dir) BIN_DIR="$2" ;;
        --binary) BINARY="$2" ;;
        --expect-jemalloc) EXPECT_JEMALLOC="$2" ;;
        --lse-min) LSE_MIN="$2"; EXPECT_LSE=1 ;;
        --expect-symbol) SYMBOLS+=("$2") ;;
      esac
      shift 2 ;;
    --expect-lse) EXPECT_LSE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: 未知参数: $1" >&2; usage >&2; exit 2 ;;
  esac
done

# --lse-min 隐含 --expect-lse：须为正整数（0 或负数会让完全无 LSE 的二进制也通过
# “N ≥ 0” 的下限检查），非法值按用法错误退出
if ! printf '%s' "$LSE_MIN" | grep -qE '^[1-9][0-9]*$'; then
  echo "ERROR: --lse-min 须为正整数: ${LSE_MIN}" >&2
  usage >&2
  exit 2
fi

if [ -t 1 ]; then
  C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'; C_RST=$'\033[0m'
else
  C_RED=""; C_GRN=""; C_YEL=""; C_RST=""
fi
PASS_CNT=0; WARN_CNT=0; FAIL_CNT=0
pass() { PASS_CNT=$((PASS_CNT+1)); echo "${C_GRN}[PASS]${C_RST} $*"; }
warn() { WARN_CNT=$((WARN_CNT+1)); echo "${C_YEL}[WARN]${C_RST} $*"; }
fail() { FAIL_CNT=$((FAIL_CNT+1)); echo "${C_RED}[FAIL]${C_RST} $*"; }

BIN="${BIN_DIR}/${BINARY}"
echo "verify_build_opts: ${BIN}"
echo "------------------------------------------------------------"

if [ ! -f "$BIN" ]; then
  fail "二进制不存在: ${BIN}"
  echo "Summary: PASS=${PASS_CNT} WARN=${WARN_CNT} FAIL=${FAIL_CNT}"
  exit 1
fi
pass "二进制存在"

if command -v file >/dev/null 2>&1; then
  INFO=$(file -b "$BIN")
  echo "       file: ${INFO}"
  case "$INFO" in
    *ELF*64-bit*) pass "文件类型为 64-bit ELF" ;;
    *) warn "文件类型异常: ${INFO}" ;;
  esac
fi

SIZE=$(stat -c '%s' "$BIN" 2>/dev/null || echo unknown)
echo "       size: ${SIZE} bytes"
if [ "$SIZE" != "unknown" ] && [ "$SIZE" -gt 1000000 ] 2>/dev/null; then
  pass "二进制大小 > 1MB"
else
  warn "二进制小于 1MB，确认是否完整"
fi

if command -v md5sum >/dev/null 2>&1; then
  echo "       md5: $(md5sum "$BIN" | awk '{print $1}')"
fi

# jemalloc 用可归属的证据核对：优先二进制内 jemalloc 自身的版本 banner——真实 banner
# 无 "jemalloc-" 前缀（jemalloc.h:111 形如 5.3.1-0-g81034ce…，经 ctl.c/prof_log.c 引用
# JEMALLOC_VERSION 进入二进制；能命中 "jemalloc-5.3.1-…" 的只有 DWARF 构建目录路径，
# 非 banner），辅以 ldd 的 libjemalloc 链接证据与 nm 符号证据。
# 对任意 "x.y.z" 串 grep -m1 可能命中其他依赖（RocksDB/boost 等）的版本串或
# 普通符号名，造成误报，不用作归属证据。
JEM_LINKED=0
if command -v ldd >/dev/null 2>&1; then
  ldd "$BIN" 2>/dev/null | grep -qi 'libjemalloc' && JEM_LINKED=1
fi
if command -v strings >/dev/null 2>&1; then
  # jemalloc 真实 banner 无 "jemalloc-" 前缀（jemalloc.h:111 形如 5.3.1-0-g81034ce…，
  # 经 ctl.c/prof_log.c 引用进入二进制）；能命中 "jemalloc-5.3.1-…" 的只有 DWARF 构建目录路径，非 banner
  JEM=$(strings "$BIN" | grep -oE '\b[0-9]+\.[0-9]+\.[0-9]+-[0-9]+-g[0-9a-f]{8,40}\b' | head -1 || true)
  # 静态链接（本 skill §4 --disable-shared）时 ldd 无 libjemalloc，用 nm 定义符号作归属证据：
  # 无 --with-jemalloc-prefix 构建（third-party v3.3 configure 不带前缀）下 jemalloc_rename.h
  # 把 je_malloc/je_free #define 成 malloc/free，公共符号以 malloc/free 进入二进制、库内符号
  # 为 je_malloc_default/je_free_default；带前缀构建才是 je_malloc/je_free。四类均计入。
  JEM_SYM=$(nm "$BIN" 2>/dev/null | grep -cE ' (T|W) (malloc|free|je_malloc|je_free|je_malloc_default|je_free_default)$' || true)
  # 归属先行：版本 banner（形如 5.3.1-0-g…）只是格式特征，任意依赖的 git-describe
  # 版本串同样匹配——必须先有可归属到 jemalloc 的通道（ldd 的 libjemalloc 条目，
  # 或 nm 的 malloc/free/je_* 定义符号），banner 才能作为版本证据
  ATTR_OK=0
  [ "${JEM_LINKED}" = "1" ] && ATTR_OK=1
  [ "${JEM_SYM:-0}" -gt 0 ] && ATTR_OK=1
  if [ -n "$JEM" ] && [ "$ATTR_OK" = "1" ]; then
    echo "       jemalloc banner: ${JEM}（归属通道：ldd=${JEM_LINKED} nm=${JEM_SYM:-0}）"
    if [ -n "$EXPECT_JEMALLOC" ]; then
      if printf '%s' "$JEM" | grep -q "${EXPECT_JEMALLOC}"; then
        pass "jemalloc banner 包含期望值 ${EXPECT_JEMALLOC}（已核归属）"
      else
        fail "jemalloc banner 不包含期望值 ${EXPECT_JEMALLOC}（实际 ${JEM}）"
      fi
    else
      pass "可读取 jemalloc 版本 banner（已核归属）"
    fi
  elif [ -n "$JEM" ] && [ "$ATTR_OK" = "0" ]; then
    echo "       形如版本 banner 的串: ${JEM}——但无 jemalloc 归属通道（ldd/nm 均无证据），不作为 jemalloc 版本"
    if [ -n "$EXPECT_JEMALLOC" ]; then
      fail "读到格式相似串 ${JEM} 但无归属证据，--expect-jemalloc ${EXPECT_JEMALLOC} 未验证"
    else
      warn "存在格式相似串但无法归属到 jemalloc，版本未验证"
    fi
  elif [ "${JEM_SYM:-0}" -gt 0 ]; then
    if [ -n "$EXPECT_JEMALLOC" ]; then
      fail "存在 nm 定义符号 malloc/free/je_*（静态链接 jemalloc，无前缀构建公共符号为 malloc/free）但读不到版本 banner，未验证 --expect-jemalloc ${EXPECT_JEMALLOC}"
    else
      warn "静态链接 jemalloc（nm 见 malloc/free/je_* 定义符号）但读不到版本 banner（可能已 strip），版本未验证"
    fi
  elif [ "$JEM_LINKED" = 1 ]; then
    if [ -n "$EXPECT_JEMALLOC" ]; then
      fail "已链接 libjemalloc 但读不到可归属的版本 banner，未验证 --expect-jemalloc ${EXPECT_JEMALLOC}"
    else
      warn "已链接 libjemalloc 但读不到版本 banner（可能已 strip），版本未验证"
    fi
  else
    if [ -n "$EXPECT_JEMALLOC" ]; then
      fail "无 libjemalloc 链接证据且无版本 banner，无法验证 --expect-jemalloc ${EXPECT_JEMALLOC}"
    else
      warn "未发现 jemalloc 链接与版本 banner（可能未链接 jemalloc 或已 strip）"
    fi
  fi
else
  if [ -n "$EXPECT_JEMALLOC" ]; then
    fail "strings 不可用，无法校验 --expect-jemalloc ${EXPECT_JEMALLOC}"
  else
    warn "strings 不可用，跳过 jemalloc 检查"
  fi
fi

if command -v objdump >/dev/null 2>&1; then
  LSE_CNT=$(objdump -d "$BIN" 2>/dev/null \
    | grep -cE '\b(ldadd|ldset|ldclr|ldeor|cas|swp)[a-z]*\s' || true)
  echo "       LSE instructions: ${LSE_CNT}"
  if [ "$EXPECT_LSE" = "1" ]; then
    if [ "${LSE_CNT:-0}" -ge "$LSE_MIN" ] 2>/dev/null; then
      pass "LSE 指令数 ${LSE_CNT} ≥ ${LSE_MIN}"
    else
      fail "LSE 指令数 ${LSE_CNT} < ${LSE_MIN}（检查 -march=armv8-a+lse）"
    fi
  else
    if [ "${LSE_CNT:-0}" -gt 0 ] 2>/dev/null; then
      pass "检测到 LSE 指令 ${LSE_CNT} 条"
    else
      warn "未检测到 LSE 指令（若期望 LSE，请检查 -march）"
    fi
  fi
else
  if [ "$EXPECT_LSE" = "1" ]; then
    fail "objdump 不可用，无法执行必需的 LSE 检查（--expect-lse 已请求）"
  else
    warn "objdump 不可用，跳过 LSE 检查"
  fi
fi

if [ "${#SYMBOLS[@]}" -gt 0 ]; then
  if command -v nm >/dev/null 2>&1; then
    for sym in "${SYMBOLS[@]}"; do
      # 字面匹配（grep -F）反修饰（nm -C）后的符号名：--expect-symbol 传的是符号名、
      # 不是正则——operator[] 这类合法 C++ 符号作为正则会被解析成空字符类直接报错，
      # nm 已列出也会被误报缺失
      if nm -C "$BIN" 2>/dev/null | grep -qF -- "$sym"; then
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

if command -v ldd >/dev/null 2>&1; then
  LDD=$(ldd "$BIN" 2>/dev/null || true)
  if [ -n "$LDD" ]; then
    if echo "$LDD" | grep -qi 'not found'; then
      fail "存在未解析的动态库依赖"
      echo "$LDD" | grep -i 'not found' | sed 's/^/       /'
    else
      pass "动态库依赖全部可解析"
    fi
  else
    warn "ldd 无输出（可能为静态链接）"
  fi
fi

echo "------------------------------------------------------------"
echo "Summary: PASS=${PASS_CNT} WARN=${WARN_CNT} FAIL=${FAIL_CNT}"
if [ "$FAIL_CNT" -gt 0 ]; then
  exit 1
fi
exit 0
