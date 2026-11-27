#!/usr/bin/env bash
# alternating_ab.sh — 单变量 A/B 交替基准协议
#
# 用法: bash scripts/alternating_ab.sh -b "<base 命令>" -o "<opt 命令>" [-r 轮数] [-w warmup]
#   -b  基线命令（引号包裹，含绑核，如 "taskset -c 288-295 ./bench --repetitions=1"）
#   -o  优化版命令（须与 base 同参数口径，仅二进制/库不同）
#   -r  交替轮数（默认 5；3-4 轮只输出趋势参考并以 exit 3 退出，不可下结论）
#   -w  每轮 warmup 次数（默认 1，不计时）
#
# 协议纪律（与 SKILL.md 三级健康度一致）:
#   - 交替而非分组：base -> opt -> base -> opt ... 共享机器的慢漂移
#     （温漂/邻居负载/页缓存）会伪装成差异，先跑完一组再跑另一组必错
#   - 三级健康度自动判定:
#       跨轮离散度: base 各轮值的变异系数 CV <= 5% = 数据可用（硬门禁，
#                   超限 exit 1 禁止下结论；典型安静微基准 CV 在 1~3%）
#       逐轮比值:   opt[i]/base[i] 序列持续单调漂移 => 报警（环境漂移嫌疑）
#       方向一致性: N/N 同向（一致加快或一致变慢均为同向，对称判定）
#                   => 可下结论；混向 => 噪声区禁止下结论
#   - 输出含逐轮原始值、比值序列、代表值（base/opt 均值与变化百分比）
#   - fail-closed：任一侧命令异常退出、或输出无可解析的正时间值（含负值与
#     非法表示法）时，打印该侧原始输出尾部并立即中止——绝不把缺失测量当作
#     0ns 计入方向统计；ns/us/ms/s 与科学计数法（如 1e-3 s）自动换算到 ns
#
# 退出码: 0 = 方向一致可下结论；1 = 噪声区/测量失败（命令异常退出/无可解析
#         时间值/非正值/跨轮离散度超限）；2 = 参数错误（缺 -b/-o、-r 非正
#         整数、-r < 3、-w 负数）；3 = 轮数 <5 的趋势参考（不可下结论）

set -u
B=""; O=""; ROUNDS=5; WARMUP=1
while getopts "b:o:r:w:h" opt; do
  case $opt in
    b) B="$OPTARG" ;;
    o) O="$OPTARG" ;;
    r) ROUNDS="$OPTARG" ;;
    w) WARMUP="$OPTARG" ;;
    h) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "参数错误" >&2; exit 2 ;;
  esac
done
[ -z "$B" ] || [ -z "$O" ] && { echo "用法: alternating_ab.sh -b \"<base 命令>\" -o \"<opt 命令>\"" >&2; exit 2; }

# 参数校验（fail-fast：防 -r 0/非法值零轮执行后输出误导性"可下结论"）
case "$ROUNDS" in
  ''|*[!0-9]*) echo "参数错误: -r 须为正整数，收到 '$ROUNDS'" >&2; exit 2 ;;
esac
case "$WARMUP" in
  ''|*[!0-9]*) echo "参数错误: -w 须为非负整数，收到 '$WARMUP'" >&2; exit 2 ;;
esac
if [ "$ROUNDS" -lt 3 ]; then
  echo "参数错误: -r 最小 3 轮（3-4 轮仅趋势参考）；收官判定须 >=5 轮" >&2
  exit 2
fi

extract_ns() { # 首个时间值 -> ns；带符号捕获（负值返回空）、支持科学计数法与千分位
  # 左边界 (^|[^0-9.,])：拒绝逗号/数字后的尾段（防 "1,234 ns" 被截成 "234 ns"、
  # "123,45 ns" 的 "45 ns" 尾配）；([,][0-9]{3})* 只认 3 位一组的千分位写法
  grep -oE -- '(^|[^0-9.,])-?[0-9]+([,][0-9]{3})*([.][0-9]+)?([eE][-+]?[0-9]+)?[[:space:]]+(ns|us|ms|s)([^[:alnum:]]|$)' \
    | head -1 | awk '
    { line = $0
      sub(/^[^0-9-]/, "", line)   # 去掉左边界捕获字符（如有）
      gsub(/,/, "", line)          # 千分位归一
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

run() { # stdout = 合并后的原始输出；返回被测命令的退出码
  bash -c "$1" 2>&1
}

measure() { # $1=侧标签，$2=命令；stdout=换算后的 ns 值；异常退出/无有效值时 fail-closed
  local out rc val
  out=$(run "$2"); rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "错误: $1 侧命令异常退出 (exit $rc)，测量中止。原始输出尾部:" >&2
    printf '%s\n' "$out" | tail -5 >&2
    exit 1
  fi
  val=$(printf '%s\n' "$out" | extract_ns)
  if ! awk -v x="$val" 'BEGIN{exit !(x>0)}'; then
    echo "错误: $1 侧无有效的正时间值（未匹配 ns/us/ms/s，或为负值/零），测量中止。原始输出尾部:" >&2
    printf '%s\n' "$out" | tail -5 >&2
    exit 1
  fi
  printf '%s\n' "$val"
}

declare -a BASEV OPTV
for round in $(seq 1 "$ROUNDS"); do
  for w in $(seq 1 "$WARMUP"); do
    run "$B" > /dev/null || { echo "错误: base 侧 warmup 异常退出 (exit $?)" >&2; exit 1; }
    run "$O" > /dev/null || { echo "错误: opt 侧 warmup 异常退出 (exit $?)" >&2; exit 1; }
  done
  BASEV[$round]=$(measure base "$B") || exit 1
  OPTV[$round]=$(measure opt "$O") || exit 1
  echo "round $round: base=${BASEV[$round]}ns  opt=${OPTV[$round]}ns  ratio=$(awk -v a="${OPTV[$round]}" -v b="${BASEV[$round]}" 'BEGIN{if(b>0) printf "%.4f", a/b; else print "n/a"}')"
done

# 数据完整性断言（缺数据即 fail-closed，不进入结论判定）
if [ "${#BASEV[@]}" -ne "$ROUNDS" ] || [ "${#OPTV[@]}" -ne "$ROUNDS" ]; then
  echo "错误: 测量数据不完整（base ${#BASEV[@]}/${ROUNDS} 轮, opt ${#OPTV[@]}/${ROUNDS} 轮），禁止下结论" >&2
  exit 1
fi

echo "---------------------------------------------"
BMEAN=$(printf '%s\n' "${BASEV[@]}" | awk '{s+=$1} END{printf "%.3f", s/NR}')
OMEAN=$(printf '%s\n' "${OPTV[@]}" | awk '{s+=$1} END{printf "%.3f", s/NR}')
echo "代表值: base=${BMEAN}ns opt=${OMEAN}ns delta=$(awk -v o="$OMEAN" -v b="$BMEAN" 'BEGIN{if(b>0) printf "%+.2f%%", (o-b)/b*100; else print "n/a"}')"
CV=$(printf '%s\n' "${BASEV[@]}" | awk '{v[NR]=$1; s+=$1} END{m=s/NR; for(i=1;i<=NR;i++) d+=(v[i]-m)^2; printf "%.3f", sqrt(d/NR)/m*100}')
echo "base 跨轮离散度(CV): ${CV}%"
echo "比值序列: $(for i in $(seq 1 $ROUNDS); do awk -v a="${OPTV[$i]}" -v b="${BASEV[$i]}" 'BEGIN{if(b>0) printf "%.3f ", a/b}'; done)"

faster=0; slower=0; equal=0
for i in $(seq 1 "$ROUNDS"); do
  d=$(awk -v a="${OPTV[$i]}" -v b="${BASEV[$i]}" 'BEGIN{print (a<b) ? "faster" : ((a>b) ? "slower" : "equal")}')
  case "$d" in
    faster) faster=$((faster+1)) ;;
    slower) slower=$((slower+1)) ;;
    *) equal=$((equal+1)) ;;
  esac
done
same_dir=$(( faster > slower ? faster : slower ))
other_dir=$((ROUNDS - same_dir))
drift=0; prev=""
for i in $(seq 2 "$ROUNDS"); do
  r1=$(awk -v a="${OPTV[$((i-1))]}" -v b="${BASEV[$((i-1))]}" 'BEGIN{printf "%.4f", a/b}')
  r2=$(awk -v a="${OPTV[$i]}" -v b="${BASEV[$i]}" 'BEGIN{printf "%.4f", a/b}')
  d=$(awk -v x=$r1 -v y=$r2 'BEGIN{print (y-x>0.0001) ? "up" : ((x-y>0.0001) ? "down" : "flat")}')
  [ "$d" != "flat" ] && [ "$d" = "$prev" ] && drift=$((drift+1))
  prev=$d
done

if ! awk -v c="$CV" 'BEGIN{exit !(c+0 <= 5)}'; then
  echo "结论: base 跨轮离散度 ${CV}% 超过 5% 硬门禁——数据不可用，禁止下结论（排查环境噪声源或增加轮数后重测）" >&2
  exit 1
fi
if [ "$drift" -ge 3 ]; then
  echo "结论: 逐轮比值持续单调漂移（${drift} 次）——环境漂移嫌疑，结果不可信，禁止下结论（换时间窗重测）" >&2
  exit 1
fi
if [ "$faster" -eq "$ROUNDS" ] || [ "$slower" -eq "$ROUNDS" ] || [ "$equal" -eq "$ROUNDS" ]; then
  if [ "$faster" -eq "$ROUNDS" ]; then DIRECTION="一致加快"
  elif [ "$slower" -eq "$ROUNDS" ]; then DIRECTION="一致变慢"
  else DIRECTION="无差异（逐轮比值恒 1）"
  fi
  if [ "$ROUNDS" -lt 5 ]; then
    echo "结论: ${ROUNDS}/${ROUNDS} 同向（${DIRECTION}）且无持续漂移——但轮数 ${ROUNDS}<5，仅趋势参考，不可下结论（加测至 >=5 轮）"
    exit 3
  fi
  echo "结论: ${ROUNDS}/${ROUNDS} 同向（${DIRECTION}）且无持续漂移 —— 可下结论（代表值见上）"
  exit 0
else
  echo "结论: ${same_dir}/${ROUNDS} 同向（漂移计数 $drift）——噪声区，禁止下结论（增轮 -r 7 或排查环境噪声源）" >&2
  exit 1
fi
