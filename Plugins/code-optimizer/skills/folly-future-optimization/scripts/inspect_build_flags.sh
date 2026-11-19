#!/usr/bin/env bash
# inspect_build_flags.sh — 查看二进制/库实际使用的编译参数（产物反推，构建系统无关）
#
# 用法: bash scripts/inspect_build_flags.sh <文件1> [文件2...]
#
# 检测项:
#   1. 编译器版本   .comment 段（不需要 -g）
#   2. 实际编译参数 DW_AT_producer 字段（GCC 把 -O/-march/-mcpu/-f 等代码生成
#                   开关原样记录，需 -g 构建）——这是"现场实际参数"的权威答案，
#                   不受构建系统（cmake/make/blade...）配置继承链的任何覆盖欺骗
#   3. SIMD 兜底    无 -g 时采样反汇编统计 SVE(z寄存器)/NEON(v寄存器)指令数
#
# 静态库(.a)自动抽取成员检查。退出码 0。

GREEN='\033[0;32m'; YEL='\033[1;33m'; NC='\033[0m'
[ $# -eq 0 ] && { echo "用法: bash inspect_build_flags.sh <文件...>"; exit 2; }

parse_producer() {
  local p="$1"
  echo -n "    优化等级: "; echo "$p" | grep -oE '[-]O[0-3sgz]' | sort -u | tr '\n' ' '; echo ""
  echo -n "    目标架构: "; echo "$p" | grep -oE '([-]march=[^ ]+|[-]mcpu=[^ ]+|[-]mtune=[^ ]+)' | sort -u | tr '\n' ' '; echo ""
  echo -n "    其他:     "; echo "$p" | grep -oE '([-]g[0-3]?|[-]fno-omit-frame-pointer|[-]ffast-math|[-]flto[a-z0-9=]*)' | sort -u | tr '\n' ' '; echo ""
}

simd_probe() {
  local f="$1"
  echo "  [SIMD 兜底探测] (采样反汇编，无 debug info 时)"
  local sample
  sample=$(timeout 60 objdump -d "$f" 2>/dev/null | head -300000)
  [ -z "$sample" ] && { echo "    无法反汇编"; return; }
  local sve neon
  sve=$(echo "$sample" | grep -cE '\bz([0-9]|[12][0-9]|3[01])\b')
  neon=$(echo "$sample" | grep -cE '\bv([0-9]|[12][0-9]|3[01])\.[0-9]+[bhsd]')
  if [ "$sve" -gt 50 ]; then echo -e "    SVE z寄存器: $sve 处  ${GREEN}=> SVE 已启用${NC}"
  elif [ "$sve" -gt 0 ];  then echo -e "    SVE z寄存器: $sve 处  ${YEL}=> 疑似少量 SVE${NC}"
  else                        echo -e "    SVE z寄存器: 0       ${YEL}=> 未见 SVE${NC}"; fi
  echo "    NEON v寄存器: $neon 处"
}

inspect_one() {
  local f="$1"
  echo "=============================================================="
  echo " 文件: $f"
  [ ! -f "$f" ] && { echo -e " ${RED}不存在${NC}"; return; }
  local target="$f"
  case "$f" in
    *.a)
      local m=$(ar t "$f" 2>/dev/null | head -1)
      [ -z "$m" ] && { echo " 无法读取静态库成员"; return; }
      local tmpf=$(mktemp /tmp/follyobj.XXXXXX.o)
      ar p "$f" "$m" > "$tmpf" 2>/dev/null
      echo " [静态库] 抽取成员 $m 检查"
      target="$tmpf"
      trap "rm -f $tmpf" RETURN ;;
  esac

  echo -n " [编译器版本] "
  readelf -p .comment "$target" 2>/dev/null | grep -oE '(GNU C\+\+|GCC|clang version)[^\\]*' | sort -u | sed 's/^ *//' | tr '\n' '; '
  echo ""

  local prods
  prods=$(readelf --debug-dump=info "$target" 2>/dev/null | grep -m5 'DW_AT_producer' | sed 's/.*: *//' | sort -u)
  if [ -n "$prods" ]; then
    echo " [实际编译参数] (DW_AT_producer 权威指纹)"
    while IFS= read -r p; do
      echo "    $p"
      parse_producer "$p"
    done <<< "$prods"
  else
    echo -e " [实际编译参数] ${YEL}无 debug info —— 用 SIMD 兜底探测判断架构特性${NC}"
    simd_probe "$f"
  fi
}

for f in "$@"; do inspect_one "$f"; done
echo ""
echo "解读: -mcpu 选择应与 compiler-flag-tuning skill 的 canonical 映射一致"
echo "      (0xd01->tsv110, 0xd03/0xd06->native, GCC 12)"
exit 0
