#!/usr/bin/env bash
# fetch_logs.sh — 在 P/D/proxy 宿主机侧定位 TTFT 打点日志并校验/暂存。
#
# 用法（在宿主机上执行；--root 必填：/data 是共享挂载，不给根直接退出，
#       防止递归扫描把其他用户目录下可读的同名日志也扫进来）：
#   bash fetch_logs.sh --root /data/user1       # 首个 --root 即扫描根（共享根必须显式给）
#   bash fetch_logs.sh --root /a --root /b      # 多根追加
#   bash fetch_logs.sh --root /data/<user> --stage /tmp/ttft_fetch  # 暂存待下载（文件名含完整来源路径，防同名覆盖）
#   bash fetch_logs.sh --root /data/<user> --expect-req 100         # P 侧 BREAKDOWN 总行数（多 P 求和）低于门槛 → 退出码非 0
#   bash fetch_logs.sh <prefill.log> <manual_xxx_dumpjsonl.json> ...  # 位置参数传文件清单：多轮压测残留时
#                                                                    # 按文件选定本轮，计数/暂存只算清单内文件
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

ROOTS=()          # 扫描根：必填（/data 是共享挂载，缺 --root 直接退出，防误扫他人目录）
ROOTS_EXPLICIT=0
STAGE=""
EXPECT_REQ=0
FAIL=0
declare -A SEEN     # realpath → 1，去重
P_BREAKDOWN_COUNTS=()
BENCH_LINE_TOTAL=0
BENCH_TS_VALID_TOTAL=0   # 含 client_send_ts 的压测记录数（与记录总数分开累计、分开报告）

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
  echo "!! 未指定扫描根（--root 必填）：/data 是共享挂载，默认递归整个根会把其他用户目录下的同名日志也扫进来" >&2
  echo "   请显式指定获准采集的目录（如 --root /data/<user>），可多根追加" >&2
  exit 2
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

count_bench_records() {  # count_bench_records FILE → 输出 "记录总数 有效时间戳数"（空格分隔）
  # 与 analyze_ttft.py load_bench 同口径：'[' 开头整文件按 JSON 数组解析，
  # 否则按 jsonl 逐行解析；每条 dict 记录计 1。不数字符串出现次数——
  # 单条记录的字段值恰含 "client_send_ts" 字样时会误计（检视复核意见）。
  # 有效时间戳数 = 含 client_send_ts 键的记录数：记录总数与有效数分开报告，
  # 数量恰好相等但全部缺字段时也能看出无法对账（缺字段记录不参与下游 ttft 对账）。
  # python3 探测加可执行性验证（Windows Git Bash 环境下 python3 可能是 Store stub，
  # command -v 命中但执行即失败——输出为空会让调用方的整数比较崩溃）
  if command -v python3 >/dev/null 2>&1 && python3 -c "pass" >/dev/null 2>&1; then
    python3 - "$1" <<'PYEOF'
import json, sys
try:
    text = open(sys.argv[1], "r", encoding="utf-8", errors="replace").read().strip()
except OSError:
    print(-1, -1); sys.exit(0)
if not text:
    print(0, 0); sys.exit(0)
# 与解析侧 load_bench 逐字对齐的计数：
#   只计「顶层元素中的 dict」——jsonl 里一行的 JSON 是数组/标量时整行跳过
#   （load_bench 的 for it in items: if not isinstance(it, dict): continue 同口径），
#   不递归嵌套数组——递归口径会让嵌套形态在采集侧"对齐绿灯"、解析侧却
#   必然终止或静默漏载
def add_dict(d, out):
    out[0] += 1
    if "client_send_ts" in d:
        out[1] += 1
n = [0, 0]
if text.startswith("["):
    # 整文件 JSON 数组（load_bench 同款形态）：解析失败不退回逐行——
    # load_bench 对该形态直接终止，采集侧按 0 条计（触发偏差告警/门槛红灯），
    # 不在坏文件上凑数字
    try:
        v = json.loads(text)
    except ValueError:
        print("[warn] %s 整文件按 JSON 数组解析失败（解析侧 load_bench 将直接终止），按 0 条计"
              % sys.argv[1], file=sys.stderr)
        print(0, 0); sys.exit(0)
    if isinstance(v, list):
        for x in v:
            if isinstance(x, dict):
                add_dict(x, n)
    elif isinstance(v, dict):
        add_dict(v, n)
    print(n[0], n[1]); sys.exit(0)
bad = 0
for line in text.splitlines():
    s = line.strip().rstrip(",").strip()
    if not s or s in ("[", "]"):
        continue
    try:
        v = json.loads(s)
    except ValueError:
        bad += 1
        continue
    if isinstance(v, dict):   # list/标量行与 load_bench 同口径整行跳过（不展开）
        add_dict(v, n)
if bad:
    print("[warn] %s 存在无法按 JSON 解析的内容（本地 analyze_ttft.py 解析时会显式报错）"
          % sys.argv[1], file=sys.stderr)
print(n[0], n[1])
PYEOF
    return
  fi
  # 宿主机无 python3 时的兜底：按 JSON 键字面量计数（“键”: 才计入，纯值不匹配；
  # 仍可能漏计罕见转义形态，完整口径以本地 analyze_ttft.py 解析为准）。
  # 兜底只有键出现数一个口径，记录总数拿不到——两数同值输出并在 stderr 告警降级。
  echo "[warn] 宿主机无 python3，压测请求数退化为键字面量计数（无记录总数口径，缺字段告警不可用）" >&2
  local c
  c=$(grep -oE '"client_send_ts"[[:space:]]*:' -- "$1" 2>/dev/null | wc -l | tr -d '[:space:]')
  echo "${c:-0} ${c:-0}"
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
    # 不数字符串出现次数——jsonl / 单行多行 JSON 数组均按记录条数计；
    # 双口径输出：记录总数 + 含 client_send_ts 的有效数，缺字段记录告警
    local bvalid
    read -r n bvalid < <(count_bench_records "$f")
    # 防御：解析输出为空或非数字（解释器异常等）时按 0 处理，不让整数比较崩溃；
    # 先剥掉行尾 \r——Windows python print 输出 CRLF，read 后尾随 \r 会让下面
    # 的非数字分支把本有效的计数值误归 0（Linux 宿主机无此问题，剥了也无副作用）
    n="${n%%$'\r'}"
    bvalid="${bvalid%%$'\r'}"
    case "$n" in ''|*[!0-9]*) n=0 ;; esac
    case "$bvalid" in ''|*[!0-9]*) bvalid=0 ;; esac
    if [ "$bvalid" -lt "$n" ]; then
      echo "        [warn] $n 条记录中仅 $bvalid 条含 client_send_ts（缺字段记录无法参与 ttft 对账，请确认压测是否 --save-detailed）"
    fi
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
    # n=记录总数、bvalid=有效时间戳数（见上），两个口径分开累计：记录数与 P 侧对账，
    # 有效数暴露"总数恰好相等但全部缺 client_send_ts、对不上账"的情形
    BENCH_LINE_TOTAL=$(( BENCH_LINE_TOTAL + n ))
    BENCH_TS_VALID_TOTAL=$(( BENCH_TS_VALID_TOTAL + bvalid ))
  fi
  if [ -n "$STAGE" ]; then
    # 唯一目标名：完整路径的 / 换成 __（':' 一并换掉，防 Windows 盘符字符），
    # 多 P 同名 prefill.log 不会互相覆盖；
    # 排他创建（noclobber 占位，O_EXCL 语义）：两个采集进程共用暂存目录时，
    # 都可能通过普通存在性检查后用 cp -f 互相覆盖——占位创建失败即并发冲突，返回失败
    local dest rel
    rel="${f#/}"
    dest="$STAGE/${rel//[\/:]/__}"
    mkdir -p "$STAGE"
    if [ -e "$dest" ] || ! ( set -o noclobber; : > "$dest.claim" ) 2>/dev/null; then
      echo "!! 暂存冲突：$dest 已存在或被并发采集进程占用（来源 $f）" >&2
      FAIL=1
      return 0
    fi
    if cp -f "$f" "$dest"; then
      echo "        → staged: $dest"
    else
      echo "!! 暂存失败：$f → $dest" >&2
      FAIL=1
    fi
    rm -f "$dest.claim"   # 释放占位：防 .claim 残留污染暂存目录、阻塞下一轮采集
  fi
  return 0
}

echo "== TTFT 日志扫描 roots: ${ROOTS[*]} =="
echo "（四份必须是同一次压测；breakdown 行数应 ≈ 压测请求数）"
echo

scan_file() {  # scan_file FILE：文件清单直传——按文件名分派类型，多轮压测残留时只算清单内文件
  local f="$1" base
  base=$(basename "$f")
  case "$base" in
    prefill.log)             scan_one "$f" P "[TTFT][BREAKDOWN]" "$f" && found_prefill=$((found_prefill+1)) || true ;;
    decode.log)              scan_one "$f" D "[TTFT_DECODE]" "$f" && found_decode=$((found_decode+1)) || true ;;
    load_balance.log)        scan_one "$f" PROXY "[LB_DISPATCH]" "$f" && found_lb=$((found_lb+1)) || true ;;
    manual_*_dumpjsonl.json) scan_one "$f" BENCH "client_send_ts" "$f" && found_bench=$((found_bench+1)) || true ;;
    *) echo "!! 无法识别文件类型（仅支持 prefill.log / decode.log / load_balance.log / manual_*_dumpjsonl.json）：$f" >&2; FAIL=1 ;;
  esac
}

for root in ${ROOTS[@]+"${ROOTS[@]}"}; do
  if [ -f "$root" ]; then
    scan_file "$root"          # 文件清单项：只对本文件计数/暂存
    continue
  fi
  [ -d "$root" ] || { echo "!! 路径不存在（既非采集目录也非清单文件）：$root"; continue; }
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
if [ "$found_prefill" -gt 0 ] && [ "$found_bench" -gt 0 ]; then
  # 两侧文件均找到即比较（含压测文件为空数组 / 零条记录的情形——
  # BENCH_LINE_TOTAL=0 与 P_TOTAL>0 不等也要触发偏差告警，不能被 -gt 0 跳过）；
  # 记录总数与有效 client_send_ts 数分开报告并分别对账——总数恰好相等但全部缺
  # 字段（有效数 0）时同样触发告警，不把"数量相等"误当"能对上账"
  if [ "$P_TOTAL" -ne "$BENCH_LINE_TOTAL" ] || [ "$P_TOTAL" -ne "$BENCH_TS_VALID_TOTAL" ]; then
    echo "!! 告警：P 侧 BREAKDOWN 总行数 $P_TOTAL ≠ 压测请求数（记录 $BENCH_LINE_TOTAL 条 / 有效 client_send_ts $BENCH_TS_VALID_TOTAL 条；请确认四份日志是否同一次压测、压测是否 --save-detailed）"
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
