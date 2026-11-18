#!/usr/bin/env bash
# allocator_ab.sh — glibc / jemalloc / tcmalloc 三方分配器交替对照协议
#
# 用法: bash scripts/allocator_ab.sh -c "<benchmark 命令>" [-r 轮数] [-w warmup]
#   -c  被测命令（引号包裹，如 "taskset -c 288-295 ./futures_benchmark --benchmark_filter=twoThens --benchmark_repetitions=1"）
#   -r  交替轮数（默认 3；收官建议 5）
#   -w  每轮前 warmup 次数（默认 1，不计时）
#
# glibc 基线语义：不设 LD_PRELOAD 且用 `env -u LD_PRELOAD` 清除继承值
#   （父进程若已预加载某分配器，空串 LD_PRELOAD 并不能还原 glibc）。
# jemalloc / tcmalloc(/_minimal)：ldconfig -p 探测到才纳入对照。
#
# 输出: 每轮每分配器的耗时，以及各分配器相对 glibc 的逐轮比值
# 判定纪律（与 SKILL.md 步骤 3 的三级健康度一致）:
#   - 逐轮比值稳定无单调漂移，且 N/N 轮同方向，才允许下结论
#   - 任何分配器结论都不可跨负载形态搬运（jemalloc 在小对象高频构造负载
#     实测耗时 +69.5%，但该数字只在同形态下成立）
#
# 退出码: 0 = 正常完成；1 = 环境不可对照（未探测到任何替代分配器 so）或
#         测量失败（命令异常退出/无可解析的正时间值，fail-closed，绝不把
#         缺失测量当 0ns 写入逐轮数值）；2 = 参数错误

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

# ---- 探测可用分配器（glibc 恒在；替代 so 探测失败不纳入）----
declare -A HAVE=( [glibc]=1 )
for cand in jemalloc tcmalloc tcmalloc_minimal; do
  p=$(ldconfig -p 2>/dev/null | awk -v l="lib${cand}.so" '$1 ~ "^"l {print $NF; exit}')
  [ -n "$p" ] && HAVE[$cand]="$p"
done
ALT=$((${#HAVE[@]} - 1))
if [ "$ALT" -eq 0 ]; then
  echo "WARN: ldconfig -p 未探测到 jemalloc/tcmalloc —— 仅 glibc 可用，三方对照无意义" >&2
  echo "（安装 libjemalloc/libtcmalloc 后重试，或仅作 glibc 基线测量）" >&2
  exit 1
fi
echo "可用分配器: $(echo "${!HAVE[@]}" | tr ' ' ',')  顺序: 每轮 glibc -> jemalloc -> tcmalloc 交替"
echo "命令: $CMD"
echo "轮数: $ROUNDS (warmup x$WARMUP 不计)"
echo "---------------------------------------------"

run_once() { # $1 = so 路径（"1" = glibc：清除继承的 LD_PRELOAD 后裸跑）
  if [ "$1" = "1" ]; then
    env -u LD_PRELOAD bash -c "$CMD" 2>&1
  else
    env LD_PRELOAD="$1" bash -c "$CMD" 2>&1
  fi
}

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
      echo "错误: $alloc 无可解析的正时间值 (ns/us/ms/s 均未匹配)，测量中止。原始输出尾部:" >&2
      printf '%s\n' "$out" | tail -5 >&2
      exit 1
    fi
    echo "round $round  $alloc: $ns ns"
    RESULTS[$alloc]="${RESULTS[$alloc]:-} $ns"
  done
done

echo "---------------------------------------------"
echo "逐轮数值（判定: 比值稳定 + 同方向; glibc 为基线）:"
for alloc in glibc jemalloc tcmalloc tcmalloc_minimal; do
  [ -z "${RESULTS[$alloc]:-}" ] && continue
  echo "  $alloc:$(echo "${RESULTS[$alloc]}" | sed 's/^ //')"
done
echo "提醒: 部署前先 cat /proc/<pid>/maps | grep -E 'jemalloc|tcmalloc' 确认现状"
exit 0
