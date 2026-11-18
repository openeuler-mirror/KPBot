#!/usr/bin/env bash
# verify_pb_patch.sh — 产物级验证 protobuf 优化 patch 是否编入业务二进制
#
# 用法: bash scripts/verify_pb_patch.sh <业务可执行文件> [protobuf 源码根目录]
#
# 原理: 优化 patch 新增的函数名（基线零命中特征）只存在于 debug info(-g 构建)
#       中 —— strings 扫描产物即判定"编入/未编入"，与构建系统无关。
#       无 -g 且给了源码根时降级为源码级特征行检查。
#
# 检查项:
#   P1  5-patch 系列(字段查找内联)   FindFieldEntryInline   字符串特征
#   P2  UTF-8 ASCII 快路径          IsStructurallyValidFast 字符串特征
#   P4  map merge 拷贝构造+预扩容     InternalMergeFrom       字符串特征
#   （三个特征任一命中 = 对应系列已编入；整包 patch 一次应用，通常同现）
#
# 依赖: bash / readelf / strings；静态库(.a) 自动抽取成员。
# 退出码 = FAIL 数（CI 可直接消费）。

set -u
RED='\033[0;31m'; GREEN='\033[0;32m'; YEL='\033[1;33m'; NC='\033[0m'
PASS=0; FAIL=0; SKIP=0
ok()   { echo -e "  [${GREEN}PASS${NC}] $1"; PASS=$((PASS+1)); }
bad()  { echo -e "  [${RED}FAIL${NC}] $1"; FAIL=$((FAIL+1)); }
skip() { echo -e "  [${YEL}SKIP${NC}] $1"; SKIP=$((SKIP+1)); }

BIN="${1:-}"; PBSRC="${2:-}"
[ -z "$BIN" ] || [ ! -f "$BIN" ] && { echo "用法: verify_pb_patch.sh <二进制或库> [源码根]" >&2; exit 2; }
STAGING=""   # set -u 安全：非 .a 路径不赋值，清理处用 ${STAGING:-} 双保险

TARGET="$BIN"
IS_A=0
case "$TARGET" in
  *.a)
    # 静态库：ldd 不适用（archive 无动态依赖），strings 直接扫整个 .a
    # （覆盖全部成员，不单抽第一个成员——特征函数在特定成员如 tctable_lite.o 中）
    IS_A=1
    ar t "$TARGET" > /dev/null 2>&1 || { echo "无法读取静态库成员表" >&2; exit 2; }
    echo "[静态库] strings 全库扫描（含全部 $(ar t "$TARGET" | wc -l) 个成员）" ;;
  *)
    PBLIB=$(ldd "$BIN" 2>/dev/null | awk '/libprotobuf/{print $3; exit}')
    [ -n "$PBLIB" ] && [ -f "$PBLIB" ] && TARGET="$PBLIB" && echo "protobuf 动态链接: $PBLIB" ;;
esac
# debug info 判定：.a 抽一个成员探 -g（构建口径信号）；ELF 直接看段
if [ "$IS_A" -eq 1 ]; then
  m=$(ar t "$TARGET" | head -1)
  STAGING=$(mktemp /tmp/pbobj.XXXXXX.o)
  ar p "$TARGET" "$m" > "$STAGING" 2>/dev/null
  HAS_DBG=$(readelf -S "$STAGING" 2>/dev/null | grep -c debug_info)
else
  HAS_DBG=$(readelf -S "$TARGET" 2>/dev/null | grep -c debug_info)
fi

check_str() { # $1=特征名 $2=显示名；不命中时按有无 debug info 区分 FAIL/SKIP
  N=$(strings -a "$TARGET" 2>/dev/null | grep -c "$1")
  if [ "$N" -ge 1 ]; then
    ok "$2: 命中 $N 处"
  elif [ "$HAS_DBG" -ge 1 ]; then
    bad "$2: 未命中（构建含 debug info 而特征缺失）—— 未编入"
  else
    skip "$2: 未命中且无 debug info（-g 未开，特征名不进产物）—— 无法产物级判断"
  fi
}

if [ "$HAS_DBG" -ge 1 ] || [ "$IS_A" -eq 1 ]; then
  check_str "FindFieldEntryInline"  "P1 5-patch 字段查找内联"
  check_str "IsStructurallyValidFast" "P2 utf8fast ASCII 快路径"
  check_str "InternalMergeFrom"      "P4 mapmerge 拷贝构造"
else
  skip "产物无 debug info（-g 未开），产物级不可行"
  if [ -n "$PBSRC" ] && [ -d "$PBSRC/src/google/protobuf" ]; then
    echo "  -- 降级源码级检查 ($PBSRC) --"
    T="$PBSRC/src/google/protobuf"
    N=$(grep -c "FindFieldEntryInline" "$T/generated_message_tctable_lite.cc" 2>/dev/null); N=${N:-0}
    [ "$N" -ge 3 ] && ok "P1 源码级: 命中 $N 处" || bad "P1 源码级: 命中 $N（需≥3）"
    N=$(grep -c "UTF8FAST" "$T/generated_message_tctable_lite.cc" 2>/dev/null); N=${N:-0}
    [ "$N" -ge 1 ] && ok "P2 源码级: 命中" || bad "P2 源码级: 未命中"
    N=$(grep -c "InternalMergeFrom(const Map& src)" "$T/map.h" 2>/dev/null); N=${N:-0}
    [ "$N" -ge 1 ] && ok "P4 源码级: 命中" || bad "P4 源码级: 未命中"
  else
    echo "  未提供源码根（第二参数），无法降级检查"
  fi
fi
[ -n "${STAGING:-}" ] && rm -f "$STAGING"

echo "--------------------------------------------"
echo "结果: PASS=$PASS FAIL=$FAIL SKIP=$SKIP"
if [ $FAIL -eq 0 ] && [ $PASS -ge 2 ]; then
  echo -e "${GREEN}结论: patch 已使能${NC}（部署后建议采火焰图做第二层双向验证）"
elif [ $FAIL -eq 0 ]; then
  echo -e "${YEL}已检项通过，但有跳过项${NC}（P 系列需 -g 构建才能产物级判断）"
else
  echo -e "${RED}存在未编入项${NC}——常见原因: 1) patch 未应用 2) 重编未重链 3) 加载了另一份库副本(ldd 核对)"
fi
exit $FAIL
