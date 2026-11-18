#!/usr/bin/env bash
# alternating_ab.sh — 单变量 A/B 5 轮交替基准协议
#
# 用法: bash scripts/alternating_ab.sh -b "<base 命令>" -o "<opt 命令>" [-r 轮数] [-w warmup]
#   -b  基线命令（引号包裹，含绑核，如 "taskset -c 288-295 ./bench --repetitions=1"）
#   -o  优化版命令（与 base 同参数口径，仅二进制/库不同）
#   -r  交替轮数（默认 5，收官协议）
#   -w  每轮前 warmup 次数（默认 1，不计时）
#
# 协议纪律（与 SKILL.md 步骤 4 一致）:
#   - 交替而非分组：base -> opt -> base -> opt ... 共享机器的慢漂移
#     （温度/邻居负载/页缓存）会伪装成差异，先跑完一组再跑另一组必错
#   - 三级健康度自动判定:
#       轮内方差  <=0.5%（5 轮 base 自身）
#       逐轮比值  opt[i]/base[i] 序列单调漂移 => 报警（环境漂移嫌疑）
#       方向一致性 5/5 同向 => 可下结论；4/1 混向 => 噪声区禁止下结论
#   - 输出含逐轮原始值与比值序列；代表值 = 5 轮算术平均
#   - fail-closed：任一侧命令异常退出、或输出无可解析的正时间值时，
#     打印该侧原始输出尾部并立即中止——绝不把缺失测量当作 0ns 计入
#     方向统计；ns/us/ms/s 自动换算为 ns（防基准框架切换单位后误判）
#
# 退出码: 0 = 方向一致可下结论；1 = 噪声区或测量失败（命令异常退出/
#         无可解析时间值/非正值）；2 = 参数错误

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

extract_ns() { # ns/us/ms/s 统一换算为 ns；单位后须跟非字母数字字符（防 "seconds" 类误配）
  grep -oE '[0-9]+(\.[0-9]+)?[[:space:]]*(ns|us|ms|s)([^[:alnum:]]|$)' | head -1 | awk '
    { if (match($0, /[0-9]+(\.[0-9]+)?/)) {
        v = substr($0, RSTART, RLENGTH) + 0
        rest = substr($0, RSTART + RLENGTH); sub(/^[ \t]*/, "", rest)
        u = substr(rest, 1, 1)
        if (u == "n")      print v
        else if (u == "u") printf "%.3f", v * 1000
        else if (u == "m") printf "%.3f", v * 1000000
        else if (u == "s") printf "%.3f", v * 1000000000
        else print ""
      } else print "" }'
}

run() { # stdout = 合并后的原始输出；返回被测命令的退出码
  bash -c "$1" 2>&1
}

measure() { # $1=侧标签 $2=命令；stdout=换算后的 ns 值；异常退出/无有效值时报错并 fail-closed
  local out rc val
  out=$(run "$2"); rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "错误: $1 侧命令异常退出 (exit $rc)，测量中止。原始输出尾部:" >&2
    printf '%s\n' "$out" | tail -5 >&2
    exit 1
  fi
  val=$(printf '%s\n' "$out" | extract_ns)
  if ! awk -v x="$val" 'BEGIN{exit !(x>0)}'; then
    echo "错误: $1 侧无可解析的正时间值 (ns/us/ms/s 均未匹配)，测量中止。原始输出尾部:" >&2
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

echo "---------------------------------------------"
echo "base 轮内方差: $(printf '%s\n' "${BASEV[@]}" | awk '{v[NR]=$1; s+=$1} END{m=s/NR; for(i=1;i<=NR;i++) d+=(v[i]-m)^2; printf "%.2f%%", sqrt(d/NR)/m*100}')"
echo "比值序列: $(for i in $(seq 1 $ROUNDS); do awk -v a="${OPTV[$i]}" -v b="${BASEV[$i]}" 'BEGIN{if(b>0) printf "%.3f ", a/b}'; done)"

same_dir=0; other_dir=0; prev=""
for i in $(seq 1 "$ROUNDS"); do
  d=$(awk -v a="${OPTV[$i]}" -v b="${BASEV[$i]}" 'BEGIN{print (a<b) ? "faster" : "slower"}')
  if [ "$d" = "faster" ]; then same_dir=$((same_dir+1)); else other_dir=$((other_dir+1)); fi
done
drift=0
for i in $(seq 2 "$ROUNDS"); do
  r1=$(awk -v a="${OPTV[$((i-1))]}" -v b="${BASEV[$((i-1))]}" 'BEGIN{printf "%.4f", a/b}')
  r2=$(awk -v a="${OPTV[$i]}" -v b="${BASEV[$i]}" 'BEGIN{printf "%.4f", a/b}')
  d=$(awk -v x=$r1 -v y=$r2 'BEGIN{print (y-x>0.0001) ? "up" : ((x-y>0.0001) ? "down" : "flat")}')
  [ "$d" != "flat" ] && [ "$d" = "$prev" ] && drift=$((drift+1))
  prev=$d
done
if [ $other_dir -eq 0 ] && [ $drift -lt 3 ]; then
  echo "结论: ${same_dir}/${ROUNDS} 同向且无持续漂移 —— 可下结论（代表值见上）"; exit 0
else
  echo "结论: ${same_dir}/${ROUNDS} 同向（漂移计数 $drift）——噪声区，禁止下结论" >&2; exit 1
fi
