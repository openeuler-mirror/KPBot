#!/usr/bin/env bash
# fetch_logs.sh — 在 P/D/proxy 宿主机侧定位 TTFT 打点日志并校验/暂存。
#
# 用法（在宿主机上执行；/data 是共享挂载，容器内日志宿主机可见）：
#   bash fetch_logs.sh                          # 未给 --root 时才用默认根 /data
#   bash fetch_logs.sh --root /data/user1       # 首个 --root 替换默认根（不扫整个 /data）
#   bash fetch_logs.sh --root /a --root /b      # 多根追加
#   bash fetch_logs.sh --stage /tmp/ttft_fetch  # 暂存待下载（文件名含完整来源路径，防同名覆盖）
#   bash fetch_logs.sh --expect-req 100         # P 侧 BREAKDOWN 总行数（多 P 求和）低于门槛 → 退出码非 0
#
# 定位目标：
#   P   日志   prefill.log                     （[TTFT][BREAKDOWN] + KVXFER + pool + shm + draft）
#   D   日志   decode.log                      （[TTFT_DECODE]）
#   proxy    load_balance.log                  （[LB_DISPATCH]）
#   压测     manual_*_dumpjsonl.json           （--save-detailed 结果）
#
# 注意：本脚本不内置任何节点/路径列表——路径一律由 --root 或位置参数传入；
#       打点标记统计用 grep -oF 逐次计数（-c 只数行，单行多记录会漏计）；
#       同一文件（realpath 相同）只统计/暂存/计入 found_* 一次，防重复计数与覆盖。

set -u

DEFAULT_ROOT="/data"
ROOTS=()
ROOTS_EXPLICIT=0
STAGE=""
EXPECT_REQ=0
FAIL=0
declare -A SEEN     # realpath → 1，去重
P_BREAKDOWN_COUNTS=()
BENCH_LINE_TOTAL=0

add_root() {
  if [ "$ROOTS_EXPLICIT" -eq 0 ]; then
    ROOTS=("$1")          # 首个显式根替换默认 /data，避免扫描整个共享目录
    ROOTS_EXPLICIT=1
  else
    ROOTS+=("$1")
  fi
}

while [ $# -gt 0 ]; do
  case "$1" in
    --root)   add_root "$2"; shift 2 ;;
    --stage)  STAGE="$2"; shift 2 ;;
    --expect-req) EXPECT_REQ="$2"; shift 2 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) add_root "$1"; shift ;;
  esac
done
if [ "${#ROOTS[@]}" -eq 0 ]; then
  ROOTS=("$DEFAULT_ROOT")
fi

case "$EXPECT_REQ" in
  ''|*[!0-9]*) echo "!! --expect-req 需要非负整数，收到：$EXPECT_REQ"; exit 2 ;;
esac

count_marker() {  # count_marker FILE MARKER → 标记出现次数（grep -oF 逐次计数，
                   # 单行多记录（如单行 JSON 数组）也数对；不存在输出 -1）
  if [ -f "$1" ]; then
    local n
    # tr -d '[:space:]'：BSD/macOS 的 wc -l 输出右填充空格（如 "       0"），
    # 不剥掉会让 [ "$ns" != "0" ] 字符串比较失效、误打印 0 值标记行
    n=$(grep -oF -- "$2" "$1" 2>/dev/null | wc -l | tr -d '[:space:]')
    echo "${n:-0}"
  else
    echo -1
  fi
}

count_bench_records() {  # count_bench_records FILE → 压测记录条数（按 JSON 解析计）
  # 与 analyze_ttft.py load_bench 同口径：'[' 开头整文件按 JSON 数组解析，
  # 否则按 jsonl 逐行解析；每条 dict 记录计 1。不数字符串出现次数——
  # 单条记录的字段值恰含 "client_send_ts" 字样时会误计（检视复核意见）。
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$1" <<'PYEOF'
import json, sys
try:
    text = open(sys.argv[1], "r", encoding="utf-8", errors="replace").read().strip()
except OSError:
    print(-1); sys.exit(0)
if not text:
    print(0); sys.exit(0)
def count(obj):
    if isinstance(obj, dict):
        return 1
    if isinstance(obj, list):
        return sum(count(x) for x in obj)
    return 0
n, bad = 0, 0
if text.startswith("["):
    try:
        n = count(json.loads(text))
    except ValueError:
        bad = 1  # 整文件解析失败 → 退回逐行（解析器侧会显式报错）
    else:
        print(n); sys.exit(0)
for line in text.splitlines():
    s = line.strip().rstrip(",").strip()
    if not s or s in ("[", "]"):
        continue
    try:
        n += count(json.loads(s))
    except ValueError:
        bad += 1
if bad:
    print("[warn] %s 存在无法按 JSON 解析的内容（本地 analyze_ttft.py 解析时会显式报错）"
          % sys.argv[1], file=sys.stderr)
print(n)
PYEOF
    return
  fi
  # 宿主机无 python3 时的兜底：按 JSON 键字面量计数（“键”: 才计入，纯值不匹配；
  # 仍可能漏计罕见转义形态，完整口径以本地 analyze_ttft.py 解析为准）
  echo "[warn] 宿主机无 python3，压测请求数退化为键字面量计数，可能存在偏差" >&2
  grep -oE '"client_send_ts"[[:space:]]*:' -- "$1" 2>/dev/null | wc -l
}

found_prefill=0; found_decode=0; found_lb=0; found_bench=0

scan_one() {  # scan_one PATH KIND MARKER ROOT
  local f="$1" kind="$2" marker="$3" root="$4"
  [ -f "$f" ] || return 1
  # realpath 去重：同一文件被多个 root 覆盖时只统计/暂存一次
  local rp
  rp=$(realpath "$f" 2>/dev/null || readlink -f "$f" 2>/dev/null || echo "$f")
  if [ -n "${SEEN[$rp]:-}" ]; then
    return 2  # 重复命中：外层 && 依赖退出码 0 才累加 found_*，非 0 即不重复计数
  fi
  SEEN["$rp"]=1
  local n size mtime
  n=$(count_marker "$f" "$marker")
  if [ "$kind" = BENCH ]; then
    # 压测请求数按解析出的请求记录计数（与 analyze_ttft.py load_bench 同口径），
    # 不数字符串出现次数——jsonl / 单行多行 JSON 数组均按记录条数计
    n=$(count_bench_records "$f")
    [ "$n" -lt 0 ] && n=0
  fi
  size=$(du -h "$f" 2>/dev/null | cut -f1)
  mtime=$(date -r "$f" "+%F %T" 2>/dev/null || stat -c '%y' "$f" 2>/dev/null)
  printf '%-8s %-60s %8s  %s  %s=%s\n' "$kind" "$f" "$size" "$mtime" "$marker" "$n"
  if [ "$kind" = P ]; then
    P_BREAKDOWN_COUNTS+=("$n")
    local ns
    ns=$(count_marker "$f" "[SHM_DEQUEUE_BATCH]")
    if [ "$ns" != "0" ] && [ "$ns" != "-1" ]; then
      echo "        [SHM_DEQUEUE_BATCH]=$ns（0 则 dequeue 列为 0，检查 VLLM_TTFT_SHM_LOG=1）"
    fi
    ns=$(count_marker "$f" "[TTFT_DRAFT]")
    if [ "$ns" != "0" ] && [ "$ns" != "-1" ]; then
      echo "        [TTFT_DRAFT]=$ns（0 则 mtp 列为 0）"
    fi
  fi
  if [ "$kind" = BENCH ]; then
    # n 已是解析出的记录条数（见上），直接累计为压测请求数
    BENCH_LINE_TOTAL=$(( BENCH_LINE_TOTAL + n ))
  fi
  if [ -n "$STAGE" ]; then
    # 唯一目标名：完整路径的 / 换成 __（':' 一并换掉，防 Windows 盘符字符），
    # 多 P 同名 prefill.log 不会互相覆盖
    local dest rel
    rel="${f#/}"
    dest="$STAGE/${rel//[\/:]/__}"
    if [ -e "$dest" ]; then
      echo "!! 暂存冲突：$dest 已存在（来源 $f）" >&2
      FAIL=1
      return 0
    fi
    mkdir -p "$STAGE"
    if ! cp -f "$f" "$dest"; then
      echo "!! 暂存失败：$f → $dest" >&2
      FAIL=1
    else
      echo "        → staged: $dest"
    fi
  fi
  return 0
}

echo "== TTFT 日志扫描 roots: ${ROOTS[*]} =="
echo "（四份必须是同一次压测；breakdown 行数应 ≈ 压测请求数）"
echo

for root in ${ROOTS[@]+"${ROOTS[@]}"}; do
  [ -d "$root" ] || { echo "!! 目录不存在：$root"; continue; }
  # P 日志：所有 prefill.log（多 P 各一份）
  while IFS= read -r -d '' f; do
    scan_one "$f" P "[TTFT][BREAKDOWN]" "$root" && found_prefill=$((found_prefill+1)) || true
  done < <(find "$root" -name 'prefill.log' -type f -print0 2>/dev/null | sort -z)
  # D 日志
  while IFS= read -r -d '' f; do
    scan_one "$f" D "[TTFT_DECODE]" "$root" && found_decode=$((found_decode+1)) || true
  done < <(find "$root" -name 'decode.log' -type f -print0 2>/dev/null | sort -z)
  # proxy 日志
  while IFS= read -r -d '' f; do
    scan_one "$f" PROXY "[LB_DISPATCH]" "$root" && found_lb=$((found_lb+1)) || true
  done < <(find "$root" -name 'load_balance.log' -type f -print0 2>/dev/null | sort -z)
  # 压测结果（必须 --save-detailed）
  while IFS= read -r -d '' f; do
    scan_one "$f" BENCH "client_send_ts" "$root" && found_bench=$((found_bench+1)) || true
  done < <(find "$root" -name 'manual_*_dumpjsonl.json' -type f -print0 2>/dev/null | sort -z)
done

echo
echo "== 汇总：P=${found_prefill} D=${found_decode} PROXY=${found_lb} BENCH=${found_bench} =="
[ "$found_prefill" -eq 0 ] && echo "!! 未找到 prefill.log（检查 /data 路径或容器日志目录名）"
[ "$found_decode" -eq 0 ] && echo "!! 未找到 decode.log"
[ "$found_lb"   -eq 0 ] && echo "!! 未找到 load_balance.log"
[ "$found_bench" -eq 0 ] && echo "!! 未找到 manual_*_dumpjsonl.json（压测需 --save-detailed）"

# breakdown 总数与压测请求数偏差告警（软告警，不改变退出码）
P_TOTAL=0
for n in ${P_BREAKDOWN_COUNTS[@]+"${P_BREAKDOWN_COUNTS[@]}"}; do
  P_TOTAL=$(( P_TOTAL + n ))
done
if [ "$found_prefill" -gt 0 ] && [ "$found_bench" -gt 0 ] && [ "$BENCH_LINE_TOTAL" -gt 0 ]; then
  if [ "$P_TOTAL" -ne "$BENCH_LINE_TOTAL" ]; then
    echo "!! 告警：P 侧 BREAKDOWN 总行数 $P_TOTAL ≠ 压测请求数 $BENCH_LINE_TOTAL（请确认四份日志是否同一次压测）"
  fi
fi

# --expect-req 门槛校验（与上方偏差告警同口径）：按各 P 日志 BREAKDOWN 行数
# 之和校验——多 P 场景每份日志只占总请求数的一部分，逐文件卡门槛会把
# 合法采集误判为失败（多 P 是本 skill 的旗舰场景，evals 即双 P）
if [ "$EXPECT_REQ" -gt 0 ]; then
  if [ "${#P_BREAKDOWN_COUNTS[@]}" -eq 0 ]; then
    echo "!! --expect-req $EXPECT_REQ 已给出，但未找到任何 P 日志"
    FAIL=1
  elif [ "$P_TOTAL" -lt "$EXPECT_REQ" ]; then
    echo "!! P 侧 BREAKDOWN 总行数 $P_TOTAL < --expect-req $EXPECT_REQ（打点未生效或日志不完整）"
    FAIL=1
  fi
fi

if [ "$found_prefill" -eq 0 ] || [ "$found_bench" -eq 0 ]; then
  echo "缺少关键输入，退出码 1"
  exit 1
fi
if [ "$FAIL" -ne 0 ]; then
  echo "存在校验失败项，退出码 $FAIL"
fi
exit "$FAIL"
