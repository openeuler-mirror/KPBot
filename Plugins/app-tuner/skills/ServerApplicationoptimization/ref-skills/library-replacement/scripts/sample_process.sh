#!/bin/bash
#
# sample_process.sh - 原子化进程采样脚本
# 用法: ./sample_process.sh <command> [args...]
# 输出: JSON 格式采样结果
#
# 采样策略:
# 1. 后台静默启动目标进程 (丢弃输出防止阻塞 Agent)
# 2. 快速轮询检查进程是否存活
# 3. 采集 ps + lsof
# 4. 输出 JSON 后立即强制清理目标进程

set -euo pipefail

COMMAND="$*"
if [[ -z "$COMMAND" ]]; then
  echo '{"error": "no command provided"}'
  exit 1
fi

# 后台静默启动进程，脱离输出管道，防止堵塞调用方
eval "$COMMAND" >/dev/null 2>&1 &
PID=$!

PS_OUTPUT=""
LSOF_OUTPUT=""
PROCESS_FOUND=0

# 快速轮询：10ms 间隔，最多 50 次 (共 500ms)
for i in $(seq 1 50); do
  if kill -0 "$PID" 2>/dev/null; then
    PROCESS_FOUND=1
    # 采集信息，comm= 放在最后防止含空格的进程名导致解析错位
    PS_OUTPUT=$(ps -p "$PID" -o pid=,%cpu=,%mem=,vsz=,rss=,comm= 2>/dev/null || echo "")
    LSOF_OUTPUT=$(lsof -p "$PID" 2>/dev/null | grep '\.so' | awk '{print $NF}' | sort -u || echo "")
    break
  fi
  sleep 0.01
done

EXIT_CODE=0

if [[ $PROCESS_FOUND -eq 0 ]]; then
  PS_OUTPUT="PROCESS_TOO_SHORT"
  LSOF_OUTPUT="PROCESS_TOO_SHORT"
fi

generate_json() {
  local pid="$1"
  local command="$2"
  local exit_code="$3"
  local ps_output="$4"
  local lsof_output="$5"
  local process_found="$6"

  # 转义 command 中的特殊字符
  local command_escaped="${command//\\/\\\\}"
  command_escaped="${command_escaped//\"/\\\"}"
  command_escaped="${command_escaped//$'\n'/\\n}"

  echo "{"
  echo "  \"pid\": $pid,"
  echo "  \"command\": \"$command_escaped\","
  echo "  \"exit_code\": $exit_code,"
  echo "  \"process_found\": $process_found,"

  if [[ $process_found -eq 1 && -n "$ps_output" ]]; then
    # 利用默认 IFS 处理前导空格，comm_val 吸收所有剩余字符
    read -r pid_val cpu_val mem_val vsz_val rss_val comm_val <<< "$ps_output"
    
    # 兜底默认值
    pid_val="${pid_val:-0}"
    cpu_val="${cpu_val:-0}"
    mem_val="${mem_val:-0}"
    vsz_val="${vsz_val:-0}"
    rss_val="${rss_val:-0}"
    comm_val="${comm_val:-unknown}"

    # 转义 comm 中的特殊字符
    local comm_val_escaped="${comm_val//\\/\\\\}"
    comm_val_escaped="${comm_val_escaped//\"/\\\"}"

    # 生成 libraries 数组
    echo "  \"libraries\": ["
    local first_lib=1
    while IFS= read -r lib; do
      [[ -z "$lib" ]] && continue
      local lib_escaped="${lib//\\/\\\\}"
      lib_escaped="${lib_escaped//\"/\\\"}"
      
      if [[ $first_lib -eq 1 ]]; then
        first_lib=0
      else
        echo ","
      fi
      printf '    "%s"' "$lib_escaped"
    done <<< "$lsof_output"
    echo ""
    echo "  ],"
    
    # 输出 ps_info
    echo "  \"ps_info\": {"
    echo "    \"pid\": $pid_val,"
    echo "    \"comm\": \"$comm_val_escaped\","
    echo "    \"cpu\": \"$cpu_val\","
    echo "    \"mem\": \"$mem_val\","
    echo "    \"vsz\": \"$vsz_val\","
    echo "    \"rss\": \"$rss_val\""
    echo "  }"
  else
    echo "  \"libraries\": [],"
    echo "  \"note\": \"Process exited before sampling (duration < 10ms).\""
  fi

  echo "}"
}

# 1. 输出 JSON
generate_json "$PID" "$COMMAND" "$EXIT_CODE" "$PS_OUTPUT" "$LSOF_OUTPUT" "$PROCESS_FOUND"

# 2. 采样结束，立即清理目标进程 (无论其是否还在运行)
if kill -0 "$PID" 2>/dev/null; then
  kill -9 "$PID" 2>/dev/null
fi