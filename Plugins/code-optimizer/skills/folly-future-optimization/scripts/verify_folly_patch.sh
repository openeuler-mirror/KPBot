#!/usr/bin/env bash
# verify_folly_patch.sh — 产物级验证 folly 优化是否编入业务二进制
#
# 用法: bash scripts/verify_folly_patch.sh <二进制或库文件>
#
# 检查项（产物反推，与构建系统无关）:
#   F1  CoreCallbackState 瘦身     debug info 中的类型标记（需 -g 构建）
#   F2  CoreBase 平凡构造内联      out-of-line 符号应消失（nm，stripped 时 SKIP）
#   F3  分配器/线程池 flag 移植    gflag 字符串常量（不需要 -g，最可靠；
#                                   注: 二进制未链接 CPUThreadPoolExecutor
#                                   编译单元时此检查为假阴性，见脚本内说明）
#
# 退出码 = FAIL 数（0 = 全过或全跳过），CI 可直接消费。
# 依赖: bash / readelf / nm / strings（binutils）

set -u
RED='\033[0;31m'; GREEN='\033[0;32m'; YEL='\033[1;33m'; NC='\033[0m'
PASS=0; FAIL=0; SKIP=0
ok()   { echo -e "  [${GREEN}PASS${NC}] $1"; PASS=$((PASS+1)); }
bad()  { echo -e "  [${RED}FAIL${NC}] $1"; FAIL=$((FAIL+1)); }
skip() { echo -e "  [${YEL}SKIP${NC}] $1"; SKIP=$((SKIP+1)); }

BIN="${1:-}"
if [ -z "$BIN" ] || [ ! -f "$BIN" ]; then
  echo "用法: bash verify_folly_patch.sh <二进制或库文件>" >&2
  exit 2
fi

# ---- 定位 folly 载体（二进制本体 or 动态库） ----
TARGET="$BIN"
LIB=$(ldd "$BIN" 2>/dev/null | awk '/libfolly/{print $3; exit}')
if [ -n "$LIB" ] && [ -f "$LIB" ]; then
  TARGET="$LIB"
  echo "folly 动态链接: $LIB"
else
  echo "folly 静态链接（检查二进制本体）"
fi

# F3: gflag 字符串常量（不需要 -g）
N=$(strings -a "$TARGET" 2>/dev/null | grep -c "folly_cputhreadpoolexecutor_use_throttled_lifo_sem")
if [ "$N" -ge 1 ]; then
  ok "F3 flag 字符串: 命中 $N 处"
else
  bad "F3 flag 字符串: 未命中（若该二进制未引用 CPUThreadPoolExecutor 编译单元，链接器不拉入此 .o，属假阴性可忽略；业务服务真用线程池则不会发生）"
fi

# F1: 类型标记（需 debug info）
HAS_DBG=$(readelf -S "$TARGET" 2>/dev/null | grep -c debug_info)
N=$(strings -a "$TARGET" 2>/dev/null | grep -c "MakeRetrievedFromStolenCoreTag")
if [ "$N" -ge 1 ]; then
  ok "F1 瘦身标记: 命中 $N 处"
elif [ "$HAS_DBG" -ge 1 ]; then
  bad "F1 瘦身标记: 有 debug info 但未见标记 —— 未编入"
else
  skip "F1 瘦身标记: 无 debug info，无法产物级判断（用 -g 构建后复查）"
fi

# F2: out-of-line 构造符号应消失
if [ -n "$(nm "$TARGET" 2>/dev/null)" ]; then
  N=$(nm -C "$TARGET" 2>/dev/null | grep -c "CoreBase::CoreBase(folly::futures::detail::State")
  M=$(nm -C "$TARGET" 2>/dev/null | grep -c "KeepAliveOrDeferred::KeepAliveOrDeferred()")
  if [ "$N" -eq 0 ]; then ok "F2 CoreBase 内联: out-of-line 符号已消失"
  else                     bad "F2 CoreBase 内联: 符号仍存在($N) —— 未编入"; fi
  if [ "$M" -eq 0 ]; then ok "F2b KeepAliveOrDeferred 内联: 符号已消失"
  else                     bad "F2b KeepAliveOrDeferred: 符号仍存在($M)"; fi
else
  skip "F2/F2b 内联: 符号表 stripped，无法判断"
fi

echo "--------------------------------------------"
echo "结果: PASS=$PASS FAIL=$FAIL SKIP=$SKIP"
if [ $FAIL -eq 0 ]; then
  echo -e "${GREEN}已检项全部通过${NC}"
else
  echo -e "${RED}存在未编入项${NC}——常见原因: 1) patch 未应用 2) 重编未重链 3) 加载了另一份库副本(ldd 核对)"
fi
exit $FAIL
