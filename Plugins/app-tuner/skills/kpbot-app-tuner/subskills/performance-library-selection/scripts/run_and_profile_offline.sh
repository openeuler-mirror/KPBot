#!/bin/bash
# 离线闭环启动与性能采样脚本
# 用法: ./run_and_profile_offline.sh [--warmup-delay <秒>] [--warmup-keyword <关键词>] [--sampling-duration <秒>] <command> [args...]
#   --warmup-delay <秒>      启动后等待指定秒数再开始 perf 采样（默认 0，适用短启动延迟场景）
#   --warmup-keyword <关键词> 启动后轮询 stdout 日志，直到出现指定关键词再开始 perf 采样
#                             （需配合将命令输出重定向到日志的场景；与 --warmup-delay 互斥，优先于 delay）
#   --sampling-duration <秒>  perf 采样持续时间（默认 10）
# 输出: 仅打印生成的 JSON 报告文件的绝对路径
#
# 长启动延迟场景（如 Ascend NPU 推理首次图编译需数分钟）：
#   bash run_and_profile_offline.sh --warmup-keyword "Inference time" --sampling-duration 10 <command>
#   bash run_and_profile_offline.sh --warmup-delay 120 --sampling-duration 10 <command>

set -euo pipefail

# -------------------------------------------------------------------------
# 0. 参数解析
# -------------------------------------------------------------------------
WARMUP_DELAY=0
WARMUP_KEYWORD=""
SAMPLING_DURATION=10
COMMAND_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --warmup-delay)
      WARMUP_DELAY="$2"
      shift 2
      ;;
    --warmup-keyword)
      WARMUP_KEYWORD="$2"
      shift 2
      ;;
    --sampling-duration)
      SAMPLING_DURATION="$2"
      shift 2
      ;;
    --)
      shift
      COMMAND_ARGS+=("$@")
      break
      ;;
    *)
      COMMAND_ARGS+=("$1")
      shift
      ;;
  esac
done

COMMAND="${COMMAND_ARGS[*]}"
REPORT_FILE="/tmp/library_replacement_report_$$.json"

if [[ -z "$COMMAND" ]]; then
  echo "{\"error\": \"no command provided\"}" > "$REPORT_FILE"
  echo "$REPORT_FILE"
  exit 1
fi

DATA_FILE="/tmp/perf_$$.data"
LOG_FILE="/tmp/warmup_log_$$.txt"

# 确保脚本退出时清理临时采样文件，但不清理生成的报告文件
trap 'rm -f "$DATA_FILE" "$LOG_FILE"' EXIT

PS_OUTPUT=""
LSOF_OUTPUT=""
PROCESS_FOUND=0

# 判断是否需要 warmup 模式
USE_WARMUP=false
if [[ "$WARMUP_DELAY" -gt 0 ]] || [[ -n "$WARMUP_KEYWORD" ]]; then
  USE_WARMUP=true
fi

# -------------------------------------------------------------------------
# 1. 启动目标进程 + Perf 采样
#    - 默认模式：perf 包裹命令（采样窗口 = 命令运行时间，最多 SAMPLING_DURATION 秒）
#    - warmup 模式：先启动命令，等待 delay/keyword 后再 attach perf 采样 SAMPLING_DURATION 秒
# -------------------------------------------------------------------------
if [[ "$USE_WARMUP" == "true" ]]; then
  # === warmup 模式：先启动命令，等待后再 attach perf ===
  # 将 stdout/stderr 重定向到日志文件（供 warmup-keyword 轮询）
  eval "$COMMAND" > "$LOG_FILE" 2>&1 &
  CMD_PID=$!

  sleep 0.5
  TARGET_PID=$CMD_PID

  # 等待 warmup 条件满足
  if [[ -n "$WARMUP_KEYWORD" ]]; then
    # 轮询日志文件，等待关键词出现（最长等待 600 秒）
    WARMUP_TIMEOUT=600
    ELAPSED=0
    while [[ $ELAPSED -lt $WARMUP_TIMEOUT ]]; do
      if ! kill -0 "$TARGET_PID" 2>/dev/null; then
        break
      fi
      if grep -q "$WARMUP_KEYWORD" "$LOG_FILE" 2>/dev/null; then
        break
      fi
      sleep 2
      ELAPSED=$((ELAPSED + 2))
    done
  else
    # 固定延迟
    sleep "$WARMUP_DELAY"
  fi

  # attach perf 采样
  if command -v perf &> /dev/null && kill -0 "$TARGET_PID" 2>/dev/null; then
    timeout "$SAMPLING_DURATION" perf record -p "$TARGET_PID" -o "$DATA_FILE" -- sleep "$SAMPLING_DURATION" 2>/dev/null || true
  fi
else
  # === 默认模式：perf 包裹命令 ===
  if command -v perf &> /dev/null; then
    timeout "$SAMPLING_DURATION" perf record -o "$DATA_FILE" -- $COMMAND >/dev/null 2>&1 &
    PERF_PID=$!
  else
    # 退化模式：如果没有 perf，仅拉起进程
    eval "$COMMAND" >/dev/null 2>&1 &
    PERF_PID=$!
  fi

  sleep 0.2

  TARGET_PID=$(pgrep -P $PERF_PID | head -n 1 || true)
  if [[ -z "$TARGET_PID" ]]; then
    TARGET_PID=$PERF_PID
  fi

  # 等待采样结束
  wait $PERF_PID 2>/dev/null || true
fi

# -------------------------------------------------------------------------
# 2. 采集静态进程状态
# -------------------------------------------------------------------------
if kill -0 "$TARGET_PID" 2>/dev/null; then
  PROCESS_FOUND=1
  PS_OUTPUT=$(ps -p "$TARGET_PID" -o pid=,vsz=,rss=,comm= --no-headers 2>/dev/null || echo "")
  LSOF_OUTPUT=$(lsof -p "$TARGET_PID" 2>/dev/null | grep '\.so' | awk '{print $NF}' | sort -u || echo "")
  # lsof 降级：容器内 lsof 可能返回空，用 /proc/maps 补充
  if [[ -z "$LSOF_OUTPUT" ]]; then
    LSOF_OUTPUT=$(cat /proc/"$TARGET_PID"/maps 2>/dev/null | grep '\.so' | awk '{print $6}' | sort -u || echo "")
  fi
fi

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
  read -r pid_val vsz_val rss_val comm_val <<< "$PS_OUTPUT"
  pid_val="${pid_val:-0}"
  comm_val="${comm_val:-unknown}"
  PS_INFO_JSON="\"ps_info\": {\"pid\": $pid_val, \"comm\": \"$comm_val\", \"vsz\": \"${vsz_val:-0}\", \"rss\": \"${rss_val:-0}\"}"
else
  PS_INFO_JSON="\"ps_info\": null
  "
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

# 清理目标进程（先 SIGTERM 优雅退出，0.5s 后仍存活则 SIGKILL 兜底）
if kill -0 "$TARGET_PID" 2>/dev/null; then
  kill "$TARGET_PID" 2>/dev/null
  sleep 0.5
  kill -0 "$TARGET_PID" 2>/dev/null && kill -9 "$TARGET_PID" 2>/dev/null
fi

# 5. 唯一标准输出：绝对路径
echo "$REPORT_FILE"
