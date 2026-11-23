#!/usr/bin/env bash
# fetch_logs.sh — 在 P/D/proxy 宿主机侧定位 TTFT 打点日志并校验/暂存。
#
# 用法（在宿主机上执行；/data 是共享挂载，容器内日志宿主机可见）：
#   bash fetch_logs.sh                          # 列出默认 /data 下四类日志 + 打点行数
#   bash fetch_logs.sh --root /data/user1 --root /data/user2
#   bash fetch_logs.sh --stage /tmp/ttft_fetch  # 复制到平面目录，便于整体下载
#   bash fetch_logs.sh --expect-req 100         # breakdown 行数 <100 时退出码非 0
#
# 定位目标：
#   P   日志   prefill.log                     （[TTFT][BREAKDOWN] + KVXFER + pool + shm + draft）
#   D   日志   decode.log                      （[TTFT_DECODE]）
#   proxy    load_balance.log                  （[LB_DISPATCH]）
#   压测     manual_*_dumpjsonl.json           （--save-detailed 结果）
#
# 注意：本脚本不内置任何节点/路径列表——路径一律由 --root 或位置参数传入。

set -u

ROOTS=("/data")
STAGE=""
EXPECT_REQ=0

while [ $# -gt 0 ]; do
  case "$1" in
    --root)   ROOTS+=("$2"); shift 2 ;;
    --stage)  STAGE="$2"; shift 2 ;;
    --expect-req) EXPECT_REQ="$2"; shift 2 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) ROOTS+=("$1"); shift ;;
  esac
done

count_marker() {  # count_marker FILE MARKER  → 行数（文件不存在输出 -1）
  if [ -f "$1" ]; then
    local n
    n=$(grep -c -- "$2" "$1" 2>/dev/null)
    echo "${n:-0}"
  else
    echo -1
  fi
}

found_prefill=0; found_decode=0; found_lb=0; found_bench=0

scan_one() {  # scan_one PATH KIND MARKER
  local f="$1" kind="$2" marker="$3"
  [ -f "$f" ] || return 1
  local n size mtime
  n=$(count_marker "$f" "$marker")
  size=$(du -h "$f" 2>/dev/null | cut -f1)
  mtime=$(date -r "$f" "+%F %T" 2>/dev/null || stat -c '%y' "$f" 2>/dev/null)
  printf '%-8s %-60s %8s  %s  %s=%s\n' "$kind" "$f" "$size" "$mtime" "$marker" "$n"
  if [ -n "$STAGE" ]; then
    mkdir -p "$STAGE"
    cp -f "$f" "$STAGE/" && echo "        → staged: $STAGE/$(basename "$f")"
  fi
  return 0
}

echo "== TTFT 日志扫描 roots: ${ROOTS[*]} =="
echo "（四份必须是同一次压测；breakdown 行数应 ≈ 压测请求数）"
echo

for root in "${ROOTS[@]}"; do
  [ -d "$root" ] || { echo "!! 目录不存在：$root"; continue; }
  # P 日志：所有 prefill.log（多 P 各一份）
  while IFS= read -r -d '' f; do
    scan_one "$f" P "[TTFT][BREAKDOWN]" && found_prefill=$((found_prefill+1)) || true
    n=$(count_marker "$f" "[SHM_DEQUEUE_BATCH]")
    [ "$n" != "0" ] && [ "$n" != "-1" ] && echo "        [SHM_DEQUEUE_BATCH]=$n（0 则 dequeue 列为 0，检查 VLLM_TTFT_SHM_LOG=1）"
    n=$(count_marker "$f" "[TTFT_DRAFT]")
    [ "$n" != "0" ] && [ "$n" != "-1" ] && echo "        [TTFT_DRAFT]=$n（0 则 mtp 列为 0）"
  done < <(find "$root" -name 'prefill.log' -type f -print0 2>/dev/null | sort -z)
  # D 日志
  while IFS= read -r -d '' f; do
    scan_one "$f" D "[TTFT_DECODE]" && found_decode=$((found_decode+1)) || true
  done < <(find "$root" -name 'decode.log' -type f -print0 2>/dev/null | sort -z)
  # proxy 日志
  while IFS= read -r -d '' f; do
    scan_one "$f" PROXY "[LB_DISPATCH]" && found_lb=$((found_lb+1)) || true
  done < <(find "$root" -name 'load_balance.log' -type f -print0 2>/dev/null | sort -z)
  # 压测结果（必须 --save-detailed）
  while IFS= read -r -d '' f; do
    scan_one "$f" BENCH "client_send_ts" && found_bench=$((found_bench+1)) || true
  done < <(find "$root" -name 'manual_*_dumpjsonl.json' -type f -print0 2>/dev/null | sort -z)
done

echo
echo "== 汇总：P=${found_prefill} D=${found_decode} PROXY=${found_lb} BENCH=${found_bench} =="
[ "$found_prefill" -eq 0 ] && echo "!! 未找到 prefill.log（检查 /data 路径或容器日志目录名）"
[ "$found_decode" -eq 0 ] && echo "!! 未找到 decode.log"
[ "$found_lb"   -eq 0 ] && echo "!! 未找到 load_balance.log"
[ "$found_bench" -eq 0 ] && echo "!! 未找到 manual_*_dumpjsonl.json（压测需 --save-detailed）"
if [ "$found_prefill" -eq 0 ] || [ "$found_bench" -eq 0 ]; then
  echo "缺少关键输入，退出码 1"
  exit 1
fi
exit 0
