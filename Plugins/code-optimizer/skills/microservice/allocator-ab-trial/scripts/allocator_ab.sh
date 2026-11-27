#!/usr/bin/env bash
# allocator_ab.sh — glibc / jemalloc / tcmalloc 三方分配器交替对照协议
#
# 用法: bash scripts/allocator_ab.sh -c "<benchmark 命令>" [-r 轮数] [-w warmup]
#   -c  被测命令（引号包裹，如 "taskset -c 288-295 ./futures_benchmark --benchmark_repetitions=1"）
#   -r  每分配器轮数（默认 3；必须为正整数）
#   -w  每轮前 warmup 次数（默认 1，不计时）
#
# glibc 基线体：用 `env -u LD_PRELOAD` 清除继承的环境变量。若被测程序在
#   链接期已选用 jemalloc/tcmalloc（ldd 可见），清除 LD_PRELOAD 后仍由它
#   提供分配函数——此时 glibc 基线不可得，脚本直接报错停止（不拿同一
#   分配器与自身比较）。
# jemalloc / tcmalloc(/_minimal)：ldconfig -p 探测到才参与。
#
# 输出: 每轮每分配器耗时 + 各分配器相对 glibc 的逐轮比值 + 均值与方向结论
# 判定（与 SKILL.md「输出契约」节的方向结论语义一致）:
#   - 比值稳定且无单调漂移，N/N 轮同向 => 可下结论
#   - 任何方向的混向都判定为噪声区，不得作为胜负结论（jemalloc 在小对象
#     高频构造负载实测耗时 +69.5%——真负收益，只应在同向稳定态下结论）
#
# 退出码: 0 = 测量完成；1 = 无可用对照组（未探测到任何替代分配器 so）或
#         测量失败（命令异常退出/无可解析时间值/非正值/被测程序链接期已
#         锁定替代分配器致 glibc 基线不可得）；2 = 参数错误

set -u
CMD=""; ROUNDS=3; WARMUP=1
while getopts "c:r:w:h" opt; do
  case $opt in
    c) CMD="$OPTARG" ;;
    r) ROUNDS="$OPTARG" ;;
    w) WARMUP="$OPTARG" ;;
    h) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "参数错误" >&2; exit 2 ;;
  esac
done
[ -z "$CMD" ] && { echo "用法: bash allocator_ab.sh -c \"<benchmark 命令>\" [-r 轮数]" >&2; exit 2; }

# 参数校验（fail-fast：-r 0 会跳过全部测量并误报成功）
case "$ROUNDS" in
  ''|*[!0-9]*) echo "参数错误: -r 须为正整数，收到 '$ROUNDS'" >&2; exit 2 ;;
esac
case "$WARMUP" in
  ''|*[!0-9]*) echo "参数错误: -w 须为非负整数，收到 '$WARMUP'" >&2; exit 2 ;;
esac
if [ "$ROUNDS" -lt 1 ]; then
  echo "参数错误: -r 至少 1 轮" >&2; exit 2
fi

# ---- 链接期分配器核实：glibc 基线可得性检查（计时前）----
probe_bin() { # 从 CMD 提取被测可执行（跳过 taskset/env/nohup 与选项前缀）
  # 整条命令作为单条记录传入（NF=词数），跨词状态机才能工作——
  # 按词拆行会让 taskset 的 i+=2 跳参在单字段记录上失效（taskset -c LIST 形态静默漏检）
  printf '%s' "$CMD" | awk '{
    for (i = 1; i <= NF; i++) {
      if ($i == "taskset") { if ($(i+1) ~ /^-/) i += 2; else i += 1; continue }
      if ($i == "env" || $i == "nohup" || $i == "time") continue
      if ($i ~ /^-/) { if ($i !~ /=$/ && $(i+1) !~ /^-/) i += 1; continue }
      if ($i ~ /=/) continue
      print $i; exit
    }
  }'
}
BIN=$(probe_bin)
if [ -z "$BIN" ]; then
  echo "WARN: 未能从 -c 命令提取被测可执行，跳过链接期分配器核实（ldd）——建议用可执行路径形式的命令以启用该检查" >&2
else
  BP=$(command -v "$BIN" 2>/dev/null || printf '%s' "$BIN")
  # LDD_PROBE：可注入替换的 ldd 命令（默认 ldd 不变）。tier-A 契约测试在
  # PATH 无法遮蔽系统 ldd 的环境（msys bash 把 /usr/bin 无条件前置）用它注入 mock。
  LDD_PROBE="${LDD_PROBE:-ldd}"
  if [ -f "$BP" ] && [ -x "$BP" ] && command -v "$LDD_PROBE" >/dev/null 2>&1; then
    LINKED=$("$LDD_PROBE" "$BP" 2>/dev/null | grep -Eo 'libjemalloc[^ ]*|libtcmalloc[^ ]*' | sort -u | head -2 | tr '\n' ' ')
    if [ -n "$LINKED" ]; then
      echo "错误: 被测程序链接期已使用: $LINKED —— 清除 LD_PRELOAD 后该轮仍由它提供分配函数，glibc 基线不可得（会拿同一分配器与自身比较）。请改用未链接替代分配器的构建，或只比较已链接分配器的 LD_PRELOAD 变体。" >&2
      exit 1
    fi
  else
    echo "WARN: 被测可执行 '$BIN' 不可解析（非文件/不可执行或无 ldd），跳过链接期分配器核实" >&2
  fi
fi

# ---- 探测可用分配器（glibc 必在；替代 so 探测失败不阻断）----
declare -A HAVE=( [glibc]=1 )
for cand in jemalloc tcmalloc tcmalloc_minimal; do
  p=$(ldconfig -p 2>/dev/null | awk -v l="lib${cand}.so" '$1 ~ "^"l {print $NF; exit}')
  [ -n "$p" ] && HAVE[$cand]="$p"
done
ALT=$((${#HAVE[@]} - 1))
if [ "$ALT" -eq 0 ]; then
  echo "WARN: ldconfig -p 未探测到 jemalloc/tcmalloc —— 仅 glibc 可用，无对照组" >&2
  echo "请先安装 libjemalloc/libtcmalloc 再测，或仅做 glibc 单基线测量" >&2
  exit 1
fi
echo "可用分配器: $(echo "${!HAVE[@]}" | tr ' ' ',')  顺序: 每轮 glibc -> jemalloc -> tcmalloc 交替"
echo "命令: $CMD"
echo "轮数: $ROUNDS (warmup x$WARMUP 不计时)"
echo "---------------------------------------------"

run_once() { # $1 = so 路径（"1" = glibc，清掉继承的 LD_PRELOAD 再跑）
  if [ "$1" = "1" ]; then
    env -u LD_PRELOAD bash -c "$CMD" 2>&1
  else
    env LD_PRELOAD="$1" bash -c "$CMD" 2>&1
  fi
}

extract_ns() { # 首个时间值 -> ns；带符号捕获（负值返回空）、支持科学计数法
  grep -oE -- '-?[0-9]+([.][0-9]+)?([eE][-+]?[0-9]+)?[[:space:]]+(ns|us|ms|s)([^[:alnum:]]|$)' \
    | head -1 | awk '
    { line = $0
      sub(/^[[:space:]]+/, "", line)
      neg = (line ~ /^-/)
      sub(/^-/, "", line)
      n = split(line, a, /[[:space:]]+/)
      v = a[1] + 0
      u = substr(a[2], 1, 1)
      if (neg || v <= 0 || u == "") { print ""; exit }
      if (u == "n")      printf "%.3f", v
      else if (u == "u") printf "%.3f", v * 1000
      else if (u == "m") printf "%.3f", v * 1000000
      else if (u == "s") printf "%.3f", v * 1000000000
      else print ""
    }'
}

declare -A RESULTS
for round in $(seq 1 "$ROUNDS"); do
  for alloc in glibc jemalloc tcmalloc tcmalloc_minimal; do
    [ -z "${HAVE[$alloc]:-}" ] && continue
    for w in $(seq 1 "$WARMUP"); do
      run_once "${HAVE[$alloc]}" > /dev/null || { echo "错误: $alloc warmup 异常退出 (exit $?)" >&2; exit 1; }
    done
    out=$(run_once "${HAVE[$alloc]}"); rc=$?
    if [ "$rc" -ne 0 ]; then
      echo "错误: $alloc 侧命令异常退出 (exit $rc)，测量中止。原始输出尾部:" >&2
      printf '%s\n' "$out" | tail -5 >&2
      exit 1
    fi
    ns=$(printf '%s\n' "$out" | extract_ns)
    if ! awk -v x="$ns" 'BEGIN{exit !(x>0)}'; then
      echo "错误: $alloc 侧无有效的正时间值（未匹配 ns/us/ms/s，或为负值/零），测量中止。原始输出尾部:" >&2
      printf '%s\n' "$out" | tail -5 >&2
      exit 1
    fi
    echo "round $round  $alloc: $ns ns"
    RESULTS[$alloc]="${RESULTS[$alloc]:-} $ns"
  done
done

# 数据完整性断言（缺数据即 fail-closed）
for alloc in "${!RESULTS[@]}"; do
  n=$(printf '%s\n' ${RESULTS[$alloc]} | wc -w)
  if [ "$n" -ne "$ROUNDS" ]; then
    echo "错误: $alloc 测量数据不完整（${n}/${ROUNDS} 轮），禁止汇总" >&2
    exit 1
  fi
done

echo "---------------------------------------------"
echo "各分配器逐轮原始值 (ns):"
for alloc in glibc jemalloc tcmalloc tcmalloc_minimal; do
  [ -z "${RESULTS[$alloc]:-}" ] && continue
  echo "  $alloc:$(echo "${RESULTS[$alloc]}" | sed 's/^ //')"
done
echo "各分配器相对 glibc 的逐轮比值与结论:"
for alloc in jemalloc tcmalloc tcmalloc_minimal; do
  [ -z "${RESULTS[$alloc]:-}" ] && continue
  RATIOS=""
  for r in $(seq 1 "$ROUNDS"); do
    g=$(echo ${RESULTS[glibc]} | awk -v i=$r '{print $i}')
    a=$(echo ${RESULTS[$alloc]} | awk -v i=$r '{print $i}')
    RATIOS="$RATIOS$(awk -v a=$a -v b=$g 'BEGIN{if(b>0) printf "%.3f ", a/b; else printf "n/a "}')"
  done
  # RESULTS[$alloc] 是空格分隔的单行多轮值：awk 单记录下 $1 只取首个词（首轮）——
  # 必须按 NF 逐词累加求均值（姊妹脚本 alternating_ab.sh 用逐行传参，等价）
  AVE=$(echo ${RESULTS[$alloc]} | awk '{for(i=1;i<=NF;i++) s+=$i} END{if(NF>0) printf "%.3f", s/NF; else print "n/a"}')
  GVE=$(echo ${RESULTS[glibc]} | awk '{for(i=1;i<=NF;i++) s+=$i} END{if(NF>0) printf "%.3f", s/NF; else print "n/a"}')
  DELTA=$(awk -v a="$AVE" -v b="$GVE" 'BEGIN{if(b+0>0) printf "%+.1f%%", (a-b)/b*100; else print "n/a"}')
  D=$(echo "$RATIOS" | awk '{f=0; s=0; e=0; for(i=1;i<=NF;i++){if($i>1.0+1e-9)s++; else if($i<1.0-1e-9)f++; else e++} if(s+e==NF && s>0) print "slower"; else if(f+e==NF && f>0) print "faster"; else if(e==NF) print "equal"; else print "mixed"}')
  case "$D" in
    slower) echo "  $alloc: 比值[$RATIOS] 均值 $AVE vs glibc $GVE (${DELTA}) —— ${ROUNDS}/${ROUNDS} 轮一致慢于 glibc，可下结论" ;;
    faster) echo "  $alloc: 比值[$RATIOS] 均值 $AVE vs glibc $GVE (${DELTA}) —— ${ROUNDS}/${ROUNDS} 轮一致快于 glibc，可下结论" ;;
    equal)  echo "  $alloc: 比值[$RATIOS] 均值 $AVE vs glibc $GVE (${DELTA}) —— 逐轮比值恒 1，无可测差异" ;;
    *)      echo "  $alloc: 比值[$RATIOS] 均值 $AVE vs glibc $GVE (${DELTA}) —— 轮间混向，噪声区，禁止下结论（增轮 -r 5 或排查环境）" ;;
  esac
done
echo "提醒: 动态链接形态下，计时前后可用 cat /proc/<pid>/maps | grep -E 'jemalloc|tcmalloc' 复核实际生效分配器"
exit 0
