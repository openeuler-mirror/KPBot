#!/bin/bash
# 离线闭环启动与性能采样脚本
# 用法: ./run_and_profile_offline.sh <command> [args...]
# 输出: 仅打印生成的 JSON 报告文件的绝对路径

set -euo pipefail

COMMAND="$*"
REPORT_FILE="/tmp/library_replacement_report_$$.json"

if [[ -z "$COMMAND" ]]; then
  echo "{\"error\": \"no command provided\"}" > "$REPORT_FILE"
  echo "$REPORT_FILE"
  exit 1
fi

DATA_FILE="/tmp/perf_$$.data"
LIBS_TMP="/tmp/libs_$$.txt"

# 确保脚本退出时清理临时采样文件，但不清理生成的报告文件
trap 'rm -f "$DATA_FILE" "$LIBS_TMP"' EXIT

PS_OUTPUT=""
LSOF_OUTPUT=""
PROCESS_FOUND=0

# -------------------------------------------------------------------------
# 1. 直接启动目标进程并拿到其 PID，再用 perf 采样该 PID
#    不用 `perf record -- $COMMAND`：那样目标进程是 perf 的孙进程，
#    pgrep -P 只能取到 perf 自身，lsof 会采到 perf 的库而非应用库。
#    采样窗口默认 30s：JVM 类负载启动慢，librocksdbjni.so 等 JNI 库懒加载，
#    10s 往往来不及覆盖；可通过环境变量 PERF_WINDOW 覆盖。
# -------------------------------------------------------------------------
PERF_WINDOW="${PERF_WINDOW:-30}"
# 用 bash -c 启动：子 shell 会 exec 成目标命令本身（comm=java/sleep），
# 使 $! 即应用 PID；若用 eval，子 shell 停留在 bash，lsof 只能采到 bash 的库。
bash -c "$COMMAND" >/dev/null 2>&1 &
TARGET_PID=$!
PERF_PID=""

# perf 可用则对目标 PID 采样（perf 不可用或失败不影响静态采集）
if command -v perf &> /dev/null; then
  timeout "$PERF_WINDOW" perf record -p "$TARGET_PID" -g -o "$DATA_FILE" >/dev/null 2>&1 &
  PERF_PID=$!
fi

# 采集库依赖：lsof（末列为路径）+ /proc/<PID>/maps（第 6 列为映射路径，覆盖更全）
collect_libs() {
  local pid="$1"
  lsof -p "$pid" 2>/dev/null | grep '\.so' | awk '{print $NF}' >> "$LIBS_TMP" || true
  grep '\.so' "/proc/$pid/maps" 2>/dev/null | awk '{print $6}' >> "$LIBS_TMP" || true
}

# 判断进程是否仍存活（排除僵尸：僵尸的 kill -0 仍成功，会误导轮询不退出）
is_alive() {
  local p="$1"
  local st
  st=$(grep '^State:' "/proc/$p/status" 2>/dev/null | awk '{print $2}')
  [[ -n "$st" && "$st" != "Z" ]]
}

# -------------------------------------------------------------------------
# 2. 首次发现进程时采集一次 ps（CPU% 累计值早期更稳定）
# -------------------------------------------------------------------------
: > "$LIBS_TMP"
for _ in $(seq 1 10); do
  if kill -0 "$TARGET_PID" 2>/dev/null; then
    PROCESS_FOUND=1
    PS_OUTPUT=$(ps -p "$TARGET_PID" -o pid=,%cpu=,%mem=,vsz=,rss=,comm= --no-headers 2>/dev/null || echo "")
    break
  fi
  sleep 0.1
done

# -------------------------------------------------------------------------
# 3. 在目标进程生命周期内轮询采集库依赖（每秒一次，取并集）
#    原因：JNI 库（如 librocksdbjni.so）由 JVM 懒加载，单次早期 lsof 会漏掉；
#    需反复抓取 lsof + /proc/<PID>/maps 并合并，才能捕获懒加载的 .so。
#    进程退出后即停止；长时进程在 PERF_WINDOW+5s 后停止（perf 已结束）。
# -------------------------------------------------------------------------
MAX_POLL=$(( PERF_WINDOW + 5 ))
i=0
while (( i < MAX_POLL )) && is_alive "$TARGET_PID"; do
  PROCESS_FOUND=1
  collect_libs "$TARGET_PID"
  sleep 1
  i=$(( i + 1 ))
done

# 回收已退出的目标进程（避免僵尸）；回收 perf（若有）
if ! is_alive "$TARGET_PID" 2>/dev/null; then
  wait "$TARGET_PID" 2>/dev/null || true
fi
if [[ -n "$PERF_PID" ]]; then
  kill "$PERF_PID" 2>/dev/null || true
  wait "$PERF_PID" 2>/dev/null || true
fi

# 若目标仍存活（perf 超时但进程未退出），补采一次（此时懒加载库通常已加载）
if kill -0 "$TARGET_PID" 2>/dev/null; then
  PROCESS_FOUND=1
  collect_libs "$TARGET_PID"
fi

LSOF_OUTPUT=$(sort -u "$LIBS_TMP" 2>/dev/null || echo "")

# -------------------------------------------------------------------------
# 3. 解析并拍平 Perf 数据，转换为 JSON 结构
# -------------------------------------------------------------------------
PERF_AVAILABLE="false"
HOTSPOTS_JSON="[]"

if [[ -f "$DATA_FILE" ]] && command -v perf &> /dev/null; then
  PERF_AVAILABLE="true"
  # 最佳实践：使用 -F overhead,dso,symbol 强制输出干净的三列数据，直接交由 awk 封装为 JSON
  HOTSPOTS_JSON=$(perf report --stdio -i "$DATA_FILE" -F overhead,dso,symbol --no-call-graph 2>/dev/null | \
  awk '
    BEGIN { print "[\n"; first=1; count=0 }
    /^#/ || NF<3 { next }
    {
      if (count >= 30) next; # 取 Top 30
      
      overhead = $1;
      dso = $2;
      symbol = $3;
      for(i=4; i<=NF; i++) symbol = symbol " " $i;
      
      gsub(/\\/, "\\\\", symbol);
      gsub(/"/, "\\\"", symbol);
      gsub(/\\/, "\\\\", dso);
      gsub(/"/, "\\\"", dso);
      
      if (!first) print ",";
      printf "    {\"overhead\": \"%s\", \"lib\": \"%s\", \"symbol\": \"%s\"}", overhead, dso, symbol;
      first = 0;
      count++;
    }
    END { print "\n  ]" }
  ')
fi

# -------------------------------------------------------------------------
# 4. JSON 数据组装与落盘
# -------------------------------------------------------------------------
LIBS_JSON="["
first=1
while IFS= read -r lib; do
  [[ -z "$lib" ]] && continue
  lib_escaped="${lib//\\/\\\\}"
  lib_escaped="${lib_escaped//\"/\\\"}"
  if [[ $first -eq 1 ]]; then first=0; else LIBS_JSON+=", "; fi
  LIBS_JSON+="\"${lib_escaped}\""
done <<< "$LSOF_OUTPUT"
LIBS_JSON+="]"

if [[ $PROCESS_FOUND -eq 1 && -n "$PS_OUTPUT" ]]; then
  read -r pid_val cpu_val mem_val vsz_val rss_val comm_val <<< "$PS_OUTPUT"
  pid_val="${pid_val:-0}"
  comm_val="${comm_val:-unknown}"
  PS_INFO_JSON="\"ps_info\": {\"pid\": $pid_val, \"comm\": \"$comm_val\", \"cpu\": \"${cpu_val:-0}\", \"mem\": \"${mem_val:-0}\", \"vsz\": \"${vsz_val:-0}\", \"rss\": \"${rss_val:-0}\"}"
else
  PS_INFO_JSON="\"ps_info\": null"
fi

command_escaped="${COMMAND//\\/\\\\}"
command_escaped="${command_escaped//\"/\\\"}"

# 将完整 JSON 写入独立文件
cat << EOF > "$REPORT_FILE"
{
  "command": "$command_escaped",
  "process_found": $( [[ $PROCESS_FOUND -eq 1 ]] && echo "true" || echo "false" ),
  $PS_INFO_JSON,
  "libraries": $LIBS_JSON,
  "perf_available": $PERF_AVAILABLE,
  "hotspots": $HOTSPOTS_JSON
}
EOF

# 清理目标进程
if kill -0 "$TARGET_PID" 2>/dev/null; then
  kill -9 "$TARGET_PID" 2>/dev/null
fi

# 5. 唯一标准输出：绝对路径
echo "$REPORT_FILE"