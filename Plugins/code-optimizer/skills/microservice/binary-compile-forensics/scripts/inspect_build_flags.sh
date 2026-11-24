#!/usr/bin/env bash
# inspect_build_flags.sh — 二进制/库编译参数产物反推（构建系统无关）
#
# 用法: bash scripts/inspect_build_flags.sh [-a] <文件1> [文件2...]
#   -a  静态库逐成员全查（默认只查首成员）
#
# 三条证据链:
#   1. 编译器版本  .comment 段（不需要 -g）
#   2. 实际编译参数 DW_AT_producer 字段（GCC 的 -O/-march/-mcpu/-f 等代码生成
#                   开关原样记录，需 -g 构建）——这是"现场实际参数"的权威答案，
#                   不受构建系统（cmake/make/blade...）配置继承链的任何覆盖欺骗。
#                   注意：-gno-record-gcc-switches 下 DW_AT_producer 存在但不
#                   记录待核验的开关；脚本只抽前 5 条，未覆盖的编译单元标为未知。
#   3. SIMD 兜底    无 -g 时采样反汇编统计 SVE(z寄存器)/NEON(v寄存器)指令。
#                   零命中只说明"样本中未见"，不能证伪编译时启用了 SVE
#                   （目标函数可能在采样范围之后，允许使用 SVE 的代码也可能只
#                   生成标量指令）。
#
# 静态库(.a)默认抽取首成员检查（-a 逐成员全查并标出成员名）。
# 完成取证 = 至少一条链产出结论（.comment 非空 / DW_AT_producer 非空 / SIMD 计数有效）。
# 退出码: 0 全部文件完成取证; 4 部分文件完成（部分失败）; 1 无任何文件完成取证;
#         2 用法错误; 3 依赖工具缺失(readelf/objdump/ar)
GREEN='\033[0;32m'; YEL='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

ALL_MEMBERS=0
if [ "$1" = "-a" ]; then ALL_MEMBERS=1; shift; fi
[ $# -eq 0 ] && { echo "用法: bash inspect_build_flags.sh [-a] <文件...>"; exit 2; }

for tool in readelf objdump ar; do
  command -v "$tool" >/dev/null 2>&1 || { echo "依赖工具缺失: $tool (binutils)" >&2; exit 3; }
done

OK_FILES=0; FP_FILES=0; TOTAL=0

parse_producer() {
  local p="$1"
  echo -n "    优化等级: "; echo "$p" | grep -oE '[-]O[0-3sgz]' | sort -u | tr '\n' ' '; echo ""
  echo -n "    目标架构: "; echo "$p" | grep -oE '([-]march=[^ ]+|[-]mcpu=[^ ]+|[-]mtune=[^ ]+)' | sort -u | tr '\n' ' '; echo ""
  echo -n "    其他:     "; echo "$p" | grep -oE '([-]g[0-3]?|[-]fno-omit-frame-pointer|[-]ffast-math|[-]flto[a-z0-9=]*)' | sort -u | tr '\n' ' '; echo ""
}

simd_probe() {
  local f="$1"
  local sample
  sample=$(timeout 60 objdump -d "$f" 2>/dev/null | head -300000)
  [ -z "$sample" ] && { echo "    无法反汇编（架构不符或损坏）" >&2; return 1; }
  local sve neon
  sve=$(echo "$sample" | grep -cE '\bz([0-9]|[12][0-9]|3[01])\b')
  neon=$(echo "$sample" | grep -cE '\bv([0-9]|[12][0-9]|3[01])\.[0-9]+[bhsd]')
  if [ "$sve" -gt 50 ]; then echo -e "    SVE z寄存器: $sve 行   ${GREEN}=> SVE 已启用${NC}"
  elif [ "$sve" -gt 0 ];  then echo -e "    SVE z寄存器: $sve 行   ${YEL}=> 疑似少量 SVE${NC}"
  else                        echo -e "    SVE z寄存器: 0        ${YEL}=> 样本中未见 SVE（不证伪启用）${NC}"; fi
  echo "    NEON v寄存器: $neon 行"
  return 0
}

inspect_one() {
  local f="$1"
  local label="${2:-$f}"
  echo "=============================================================="
  echo " 文件: $label"
  if [ ! -f "$f" ]; then echo -e " ${RED}不存在${NC}" >&2; return 1; fi

  local targets=()
  local tmplist=""

  case "$f" in
    *.a)
      if [ "$ALL_MEMBERS" -eq 1 ]; then
        local members
        members=$(ar t "$f" 2>/dev/null)
        [ -z "$members" ] && { echo " 无法读取静态库成员" >&2; return 1; }
        local tmpdir=$(mktemp -d /tmp/bcfarchive.XXXXXX)
        tmplist="$tmpdir"
        local idx=0
        while IFS= read -r m; do
          [ -z "$m" ] && continue
          idx=$((idx+1))
          local t="$tmpdir/$(printf '%03d' $idx)_$(basename "$m")"
          ar p "$f" "$m" > "$t" 2>/dev/null
          if [ -s "$t" ]; then
            targets+=("$t")
          else
            echo "  [warn] member $m extraction failed (empty after ar p)" >&2
            rm -f "$t"
          fi
        done <<< "$members"
        [ ${#targets[@]} -eq 0 ] && { echo " 无法抽取任何成员" >&2; rm -rf "$tmpdir"; return 1; }
        echo " [静态库 -a] 抽取 ${#targets[@]} 个成员逐检查"
      else
        local m=$(ar t "$f" 2>/dev/null | head -1)
        [ -z "$m" ] && { echo " 无法读取静态库成员" >&2; return 1; }
        local tmpf=$(mktemp /tmp/bcfobj.XXXXXX.o)
        tmplist="$tmpf"
        ar p "$f" "$m" > "$tmpf" 2>/dev/null
        [ -s "$tmpf" ] || { echo " 成员抽取失败" >&2; rm -f "$tmpf"; return 1; }
        echo " [静态库] 抽取首成员 $m 检查"
        targets+=("$tmpf")
      fi
      ;;
    *)
      targets+=("$f")
      ;;
  esac

  local any_evidence=0
  local fp_found=0

  for target in "${targets[@]}"; do
    local member_label=""
    [ ${#targets[@]} -gt 1 ] && member_label=" $(basename "$target")"

    # chain 1: compiler version
    local ver
    ver=$(readelf -p .comment "$target" 2>/dev/null | grep -oE '(GNU C\+\+|GCC|clang version)[^\\]*' | sort -u | sed 's/^ *//' | tr '\n' '; ')
    if [ -n "$ver" ]; then
      any_evidence=1
      echo -n " [编译器版本]$member_label $ver"
      echo ""
    else
      echo " [编译器版本]$member_label (无 .comment 数据)"
    fi

    # chain 2: DW_AT_producer
    local prods
    prods=$(readelf --debug-dump=info "$target" 2>/dev/null | grep -m5 'DW_AT_producer' | sed 's/.*: *//' | sort -u)
    if [ -n "$prods" ]; then
      any_evidence=1
      fp_found=1
      echo " [实际编译参数]$member_label (DW_AT_producer 权威指纹，仅前 5 个编译单元；未覆盖单元的参数为未知)"
      while IFS= read -r p; do
        echo "    $p"
        parse_producer "$p"
      done <<< "$prods"
    else
      echo -e " [实际编译参数]$member_label ${YEL}无 DW_AT_producer（未带 -g 构建或已分离 debuginfo——按 build ID 查找分离的 debuginfo 包可获取指纹；需权威指纹时让上游带 -g 重编）${NC}"
    fi

    # chain 3: SIMD fallback
    local simd_out
    if simd_out=$(simd_probe "$target" 2>/dev/null); then
      any_evidence=1
      echo "  [SIMD 兜底探测]$member_label (采样反汇编)"
      echo "$simd_out"
    else
      echo "  [SIMD 兜底探测]$member_label (无法反汇编)" >&2
    fi
  done

  # cleanup
  if [ -n "$tmplist" ]; then
    if [ -d "$tmplist" ]; then rm -rf "$tmplist"; else rm -f "$tmplist"; fi
  fi

  [ "$any_evidence" -eq 1 ] || { echo -e " ${YEL}三条链均无结论——该文件可能不是 ELF 或已损坏${NC}" >&2; return 1; }
  [ "$fp_found" -eq 1 ] && FP_FILES=$((FP_FILES+1))
  return 0
}

for f in "$@"; do
  TOTAL=$((TOTAL+1))
  if inspect_one "$f"; then OK_FILES=$((OK_FILES+1)); fi
done

echo "=============================================================="
echo "结论: $OK_FILES/$TOTAL 个文件完成取证（其中 $FP_FILES 个取得 DW_AT_producer 指纹；降级链如实标注）"
if [ "$OK_FILES" -lt "$TOTAL" ]; then
  echo "注意: $((TOTAL-OK_FILES)) 个文件未完成取证（三条链均无结论）" >&2
fi
if [ "$OK_FILES" -eq "$TOTAL" ]; then
  exit 0
elif [ "$OK_FILES" -gt 0 ]; then
  exit 4  # partial success: some files completed, some failed
else
  exit 1
fi
