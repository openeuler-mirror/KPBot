#!/bin/bash
# 统一性能采样脚本（在线 PID 附加 / 离线命令闭环启动）
# 用法:
#   在线模式: bash run_and_profile.sh --pid <PID> [--sampling-duration <秒>]
#   离线模式: bash run_and_profile.sh [--warmup-delay <秒>] [--warmup-keyword <关键词>] [--sampling-duration <秒>] <command> [args...]
#     --warmup-delay <秒>         启动后等待指定秒数再开始 perf 采样（默认 0，适用短启动延迟场景）
#     --warmup-keyword <关键词>    启动后轮询 stdout 日志，出现关键词后再开始采样（与 delay 互斥，优先于 delay）
#     --sampling-duration <秒>     perf 采样持续时间（默认 10）
# 输出: 仅打印生成的 JSON 报告文件的绝对路径（自包含，离线/在线两模式同构）:
#   { mode, command|pid, process_found, ps_info, libraries, environ,
#     perf_available, perf_degraded, hotspots(self%, Top100), perf_data,
#     perf_callcount_report, perf_dso_report, [perf_error], [needs_authorization, auth_hints] }
#
# 设计要点:
# 1. 模式判定: 指定 --pid 即在线模式 — 目标进程已在外部运行，只读采样，不 kill；
#    否则离线模式 — 脚本内部完成 启动→(warmup 等待)→采样→收集→清理 全生命周期闭环
#    （禁止跨脚本传递活动进程 PID，见主 SKILL.md 红线 #2）。
# 2. 静态信息（ps / lsof + /proc/<PID>/maps / environ）总是采集并内联写入报告，
#    作为 perf 失败/无权限时的兜底；lsof 与 /proc/maps 输出互补合并。
# 3. perf 统一 -g --call-graph dwarf 采样；DWARF debug 信息损坏（常见于 CANN/第三方库）
#    会导致 perf report 空输出但退出码为 0，因此报告生成以"输出非空"为准逐级降级:
#    DWARF 扁平报告 → IP-only 重采样 → DSO-only 报告，降级记录在 perf_degraded 字段。
# 4. hotspots 用 --no-children 取 self%（call graph 数据下避免百分比重复累加）。
# 5. 不使用 set -e: 脚本需要捕获 perf record 的非零退出码并降级处理，
#    set -e 会使降级分支不可达。

set -uo pipefail

# =========================================================================
# 辅助函数（定义在前，运行时解析变量）
# =========================================================================

# JSON 字符串转义（反斜杠/双引号/换行/回车/制表符 → 合法 JSON 转义序列）
json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\n'/\\n}"
  s="${s//$'\r'/\\r}"
  s="${s//$'\t'/\\t}"
  printf '%s' "$s"
}

# 生成扁平 self% 热点 JSON 数组
# 参数: <perf data 文件> <超时秒>; 输出: [{"overhead","lib","symbol"}...]（Top 100）
# 关键: DWARF 损坏时 perf report 可能空输出但退出码为 0，由调用方以内容非空判定降级
flat_report_json() {
  local data_file="$1" tmo="$2"
  if [[ ! -s "$data_file" ]]; then
    echo "[]"
    return
  fi
  timeout "$tmo" perf report --stdio -i "$data_file" -F overhead,dso,symbol --no-children --no-call-graph 2>/dev/null | awk '
    BEGIN { print "["; first=1; count=0 }
    /^#/ || NF<3 { next }
    {
      if (count >= 100) next
      overhead = $1; dso = $2; symbol = $3
      for (i=4; i<=NF; i++) symbol = symbol " " $i
      gsub(/\\/, "\\\\", symbol); gsub(/"/, "\\\"", symbol)
      gsub(/\\/, "\\\\", dso); gsub(/"/, "\\\"", dso)
      if (!first) print ","
      printf "    {\"overhead\": \"%s\", \"lib\": \"%s\", \"symbol\": \"%s\"}", overhead, dso, symbol
      first = 0; count++
    }
    END { print "\n  ]" }
  '
}

# 生成 DSO-only 报告（符号解析完全失败时的最终降级，仅 DSO 维度）
# 优先使用 IP-only data（小文件），其次 DWARF data
generate_dso_report() {
  local src=""
  if [[ -s "$IP_DATA_FILE" ]]; then src="$IP_DATA_FILE"; else src="$DATA_FILE"; fi
  [[ -s "$src" ]] || return 1
  timeout 10 perf report --stdio -i "$src" -F overhead,dso --no-children 2>/dev/null | head -50 > "$DSO_FILE"
  [[ -s "$DSO_FILE" ]]
}

# 写错误报告（始终落盘报告文件，路径作为唯一标准输出）
# 参数: <错误信息> [PID]
write_error_report() {
  local err_msg="$1" pid_field="${2:-null}"
  {
    printf '{\n'
    printf '  "mode": "%s",\n' "$MODE"
    printf '  "pid": %s,\n' "$pid_field"
    printf '  "error": "%s",\n' "$(json_escape "$err_msg")"
    printf '  "process_found": false,\n'
    printf '  "ps_info": null,\n'
    printf '  "libraries": [],\n'
    printf '  "environ": [],\n'
    printf '  "perf_available": false,\n'
    printf '  "perf_degraded": "none",\n'
    printf '  "hotspots": [],\n'
    printf '  "perf_data": null,\n'
    printf '  "perf_callcount_report": null,\n'
    printf '  "perf_dso_report": null\n'
    printf '}\n'
  } > "$REPORT_FILE"
  echo "$REPORT_FILE"
}

# =========================================================================
# 0. 参数解析
# =========================================================================
MODE="offline"
TARGET_PID_ARG=""
WARMUP_DELAY=0
WARMUP_KEYWORD=""
SAMPLING_DURATION=10
COMMAND_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pid)
      MODE="online"
      if [[ $# -ge 2 ]]; then TARGET_PID_ARG="$2"; shift 2; else shift; fi
      ;;
    --warmup-delay)
      if [[ $# -ge 2 ]]; then WARMUP_DELAY="$2"; shift 2; else shift; fi
      ;;
    --warmup-keyword)
      if [[ $# -ge 2 ]]; then WARMUP_KEYWORD="$2"; shift 2; else shift; fi
      ;;
    --sampling-duration)
      if [[ $# -ge 2 ]]; then SAMPLING_DURATION="$2"; shift 2; else shift; fi
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

# =========================================================================
# 1. 运行环境与文件路径
# =========================================================================
TAG=$$
REPORT_FILE="/tmp/library_sampling_report_${TAG}.json"
DATA_FILE="/tmp/perf_dwarf_${TAG}.data"          # DWARF call-graph 采样数据
IP_DATA_FILE="/tmp/perf_ip_${TAG}.data"          # IP-only 降级采样数据（无 call-graph）
CALLCOUNT_FILE="/tmp/perf_callcount_${TAG}.txt"  # -n 调用次数报告（malloc 频次/调用方分析）
DSO_FILE="/tmp/perf_dso_${TAG}.txt"              # DSO-only 降级报告
LOG_FILE="/tmp/warmup_log_${TAG}.txt"            # 离线模式命令输出（warmup-keyword 轮询）
LIBS_TMP="/tmp/libs_${TAG}.txt"                  # 库列表中间文件（多次采集合并）
ENVIRON_FILE="/tmp/environ_${TAG}.txt"          # 关键环境变量中间文件

: > "$LIBS_TMP"
: > "$ENVIRON_FILE"

# 清理中间文件；报告文件与被报告引用的 perf 产物（data/callcount/dso）保留
trap 'rm -f "$LOG_FILE" "$LIBS_TMP" "$ENVIRON_FILE"' EXIT

# =========================================================================
# 2. 参数校验
# =========================================================================
if [[ "$MODE" == "online" ]]; then
  if [[ -z "$TARGET_PID_ARG" ]] || ! [[ "$TARGET_PID_ARG" =~ ^[0-9]+$ ]]; then
    write_error_report "valid --pid <PID> is required for online mode"
    exit 1
  fi
else
  if [[ "${#COMMAND_ARGS[@]}" -eq 0 ]]; then
    write_error_report "no command provided"
    exit 1
  fi
  if ! [[ "$SAMPLING_DURATION" =~ ^[0-9]+$ ]] || ! [[ "$WARMUP_DELAY" =~ ^[0-9]+$ ]]; then
    write_error_report "sampling-duration/warmup-delay must be non-negative integers"
    exit 1
  fi
fi

# =========================================================================
# 3. 确定目标进程（在线: 外部 PID / 离线: 后台启动命令）
# =========================================================================
PS_OUTPUT=""
PROCESS_FOUND=0
COMMAND_JSON="null"
TARGET_PID=""

# 瞬时 CPU%（/proc stat 0.5s 差分；ps 的 %cpu 是生命周期平均，对长运行进程有误导性）
sample_cpu_now() {
  local tp="$1" a b clk
  a=$(awk '{print $14+$15}' "/proc/$tp/stat" 2>/dev/null) || return 1
  sleep 0.5
  b=$(awk '{print $14+$15}' "/proc/$tp/stat" 2>/dev/null) || return 1
  [[ -n "$a" && -n "$b" ]] || return 1
  clk=$(getconf CLK_TCK 2>/dev/null || echo 100)
  awk -v d=$((b - a)) -v clk="$clk" 'BEGIN{printf "%.1f", d/clk/0.5*100}'
}

# 静态信息采集（可重复调用: 库列表增量合并，ps/environ 仅首次采集）
# 进程死亡时静默跳过（保持 best-effort 语义）
capture_static() {
  local tp="$1"
  kill -0 "$tp" 2>/dev/null || return 0
  if [[ -z "$PS_OUTPUT" ]]; then
    PS_OUTPUT=$(ps -p "$tp" -o pid=,%cpu=,%mem=,vsz=,rss=,comm= --no-headers 2>/dev/null || echo "")
    CPU_NOW=$(sample_cpu_now "$tp" 2>/dev/null || echo "")
  fi
  # lsof: 最后一列为库路径；容器内可能返回空；过滤 lsof 装饰后缀等非路径字段
  lsof -p "$tp" 2>/dev/null | grep '\.so' | awk '{print $NF}' | grep -E '^/' >> "$LIBS_TMP" || true
  # /proc/<PID>/maps: 第 6 列为映射路径（lsof 缺失或权限不足时覆盖更全，输出互补）
  grep '\.so' "/proc/$tp/maps" 2>/dev/null | awk '{print $6}' >> "$LIBS_TMP" || true
  # 环境变量（LD_PRELOAD / MALLOC_CONF / TCMALLOC_* 等，仅采集一次）
  if [[ ! -s "$ENVIRON_FILE" ]]; then
    cat "/proc/$tp/environ" 2>/dev/null | tr '\0' '\n' \
      | grep -E '^LD_PRELOAD=|^MALLOC_CONF=|^MALLOC_ARENA|^TCMALLOC|^HEAPPROFILE=' \
      >> "$ENVIRON_FILE" 2>/dev/null || true
  fi
}

if [[ "$MODE" == "online" ]]; then
  TARGET_PID="$TARGET_PID_ARG"
  if ! kill -0 "$TARGET_PID" 2>/dev/null; then
    # 进程不存在: 语义为"分析完成但目标缺失"，报告落盘后正常退出
    write_error_report "process $TARGET_PID not found" "$TARGET_PID"
    exit 0
  fi
  capture_static "$TARGET_PID"
else
  COMMAND="${COMMAND_ARGS[*]}"
  COMMAND_JSON="\"$(json_escape "$COMMAND")\""
  # 启动命令（输出重定向到日志文件，供 warmup-keyword 轮询）
  # 注意: eval "$COMMAND" & 的 $! 是 bash 子壳而非命令本身——子壳会 fork 实际命令
  # 作为其子进程, 因此启动后必须轮询解析子壳的子进程作为真实采样目标
  eval "$COMMAND" > "$LOG_FILE" 2>&1 &
  SUBSHELL_PID=$!
  disown "$SUBSHELL_PID" 2>/dev/null || true
  TARGET_PID=""
  local_child=""
  for _ in $(seq 1 50); do
    local_child=$(pgrep -P "$SUBSHELL_PID" 2>/dev/null | head -n 1)
    if [[ -n "$local_child" ]]; then
      TARGET_PID="$local_child"
      break
    fi
    kill -0 "$SUBSHELL_PID" 2>/dev/null || break
    sleep 0.1
  done
  # 无子进程（命令已退出或子壳直接执行）时回退到子壳本身
  [[ -z "$TARGET_PID" ]] && TARGET_PID="$SUBSHELL_PID"

  # warmup 等待: 关键词轮询优先，其次固定延迟，最后默认短暂等待动态库加载完成
  if [[ -n "$WARMUP_KEYWORD" ]]; then
    WARMUP_TIMEOUT=600
    ELAPSED=0
    while [[ $ELAPSED -lt $WARMUP_TIMEOUT ]]; do
      kill -0 "$TARGET_PID" 2>/dev/null || break
      grep -qF "$WARMUP_KEYWORD" "$LOG_FILE" 2>/dev/null && break
      sleep 2
      ELAPSED=$((ELAPSED + 2))
    done
  elif [[ "$WARMUP_DELAY" -gt 0 ]]; then
    sleep "$WARMUP_DELAY"
  else
    sleep 0.3
  fi

  capture_static "$TARGET_PID"
fi

if kill -0 "$TARGET_PID" 2>/dev/null; then
  PROCESS_FOUND=1
fi

# =========================================================================
# 4. perf 采样（best-effort，权限不足时标记 needs_authorization）
# =========================================================================
PERF_AVAILABLE="false"
PERF_ERROR=""
NEEDS_AUTH="false"
PERF_DEGRADED="none"
HOTSPOTS_JSON="[]"
USED_DATA_FILE=""

if ! command -v perf &> /dev/null; then
  PERF_ERROR="perf command not found, please install: apt install linux-tools-generic"
elif ! kill -0 "$TARGET_PID" 2>/dev/null; then
  PERF_ERROR="target process exited before sampling"
elif [[ ! -r "/proc/$TARGET_PID" ]]; then
  PERF_ERROR="Cannot access /proc/$TARGET_PID, permission denied"
  NEEDS_AUTH="true"
else
  PERF_OUT=$(timeout "$((SAMPLING_DURATION + 10))" perf record -p "$TARGET_PID" -g --call-graph dwarf -o "$DATA_FILE" -- sleep "$SAMPLING_DURATION" 2>&1 || true)
  if echo "$PERF_OUT" | grep -qi "permission denied"; then
    PERF_ERROR="perf requires permission to sample process $TARGET_PID"
    NEEDS_AUTH="true"
  elif [[ -f "$DATA_FILE" ]]; then
    # ---- 报告生成（多级降级，以输出非空为准）----
    # Level 1: DWARF 扁平 self% 报告
    HOTSPOTS_JSON=$(flat_report_json "$DATA_FILE" 30)
    if grep -q '"symbol"' <<< "$HOTSPOTS_JSON" 2>/dev/null; then
      USED_DATA_FILE="$DATA_FILE"
    else
      # Level 2: DWARF 损坏/大文件超时 → IP-only 重采样（无 call-graph，data 小、处理快）
      PERF_DEGRADED="ip_only"
      if kill -0 "$TARGET_PID" 2>/dev/null; then
        timeout "$((SAMPLING_DURATION + 10))" perf record -p "$TARGET_PID" -o "$IP_DATA_FILE" -- sleep "$SAMPLING_DURATION" >/dev/null 2>&1 || true
        HOTSPOTS_JSON=$(flat_report_json "$IP_DATA_FILE" 15)
        if grep -q '"symbol"' <<< "$HOTSPOTS_JSON" 2>/dev/null; then
          USED_DATA_FILE="$IP_DATA_FILE"
        fi
      fi
      # Level 3: DSO-only 报告（无符号解析，最可靠降级）
      if [[ -z "$USED_DATA_FILE" ]]; then
        PERF_DEGRADED="dso_only"
        HOTSPOTS_JSON="[]"
        generate_dso_report || true
      fi
    fi
    PERF_AVAILABLE="true"

    # 调用次数报告（best-effort，供 malloc 频次/调用方分析）
    if [[ -n "$USED_DATA_FILE" ]]; then
      timeout 30 perf report --stdio -n -i "$USED_DATA_FILE" 2>/dev/null | head -300 > "$CALLCOUNT_FILE" || true
      [[ -s "$CALLCOUNT_FILE" ]] || rm -f "$CALLCOUNT_FILE"
    fi
  else
    PERF_ERROR="perf sampling failed: $PERF_OUT"
  fi
fi

# =========================================================================
# 5. 采样后静态信息补充（捕获 late-dlopen 库）+ 离线进程清理
# =========================================================================
capture_static "$TARGET_PID"

# 收集进程树（含自身与全部后代）
collect_tree() {
  local p="$1" c
  echo "$p"
  for c in $(pgrep -P "$p" 2>/dev/null); do
    collect_tree "$c"
  done
}

if [[ "$MODE" == "offline" ]]; then
  # 清理脚本自身启动的整棵进程树（eval 子壳 + 实际命令 + 后代），
  # 仅杀子壳会让实际命令成为孤儿进程；命令 daemonize 导致子壳先退出时以目标 PID 为根
  ROOT=""
  kill -0 "$SUBSHELL_PID" 2>/dev/null && ROOT="$SUBSHELL_PID"
  [[ -z "$ROOT" ]] && kill -0 "$TARGET_PID" 2>/dev/null && ROOT="$TARGET_PID"
  if [[ -n "$ROOT" ]]; then
    TREE_PIDS=$(collect_tree "$ROOT")
    # shellcheck disable=SC2086
    kill $TREE_PIDS 2>/dev/null || true
    sleep 0.5
    ALIVE_PIDS=""
    for p in $TREE_PIDS; do
      kill -0 "$p" 2>/dev/null && ALIVE_PIDS="$ALIVE_PIDS $p"
    done
    # shellcheck disable=SC2086
    [[ -n "${ALIVE_PIDS// /}" ]] && kill -9 $ALIVE_PIDS 2>/dev/null || true
  fi
fi

# =========================================================================
# 6. JSON 组装与落盘（printf 逐行写入，避免 heredoc 对值中 $/\ 的二次展开）
# =========================================================================
# 库列表 JSON 数组（sort -u 去重合并多次采集）
LSOF_OUTPUT=$(sort -u "$LIBS_TMP" 2>/dev/null || echo "")
LIBS_JSON="["
first=1
while IFS= read -r lib; do
  [[ -z "$lib" ]] && continue
  if [[ $first -eq 1 ]]; then first=0; else LIBS_JSON+=", "; fi
  LIBS_JSON+="\"$(json_escape "$lib")\""
done <<< "$LSOF_OUTPUT"
LIBS_JSON+="]"

# 环境变量 JSON 数组（KEY=VALUE 字符串列表）
ENVIRON_JSON="["
first=1
if [[ -s "$ENVIRON_FILE" ]]; then
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    if [[ $first -eq 1 ]]; then first=0; else ENVIRON_JSON+=", "; fi
    ENVIRON_JSON+="\"$(json_escape "$line")\""
  done < "$ENVIRON_FILE"
fi
ENVIRON_JSON+="]"

# ps_info JSON 对象
if [[ -n "$PS_OUTPUT" ]]; then
  read -r pid_val cpu_val mem_val vsz_val rss_val comm_val <<< "$PS_OUTPUT"
  pid_val="${pid_val:-0}"
  comm_val="${comm_val:-unknown}"
  PS_INFO_JSON="\"ps_info\": {\"pid\": $pid_val, \"comm\": \"$(json_escape "$comm_val")\", \"cpu\": \"${cpu_val:-0}\", \"cpu_now\": \"${CPU_NOW:-}\", \"mem\": \"${mem_val:-0}\", \"vsz\": \"${vsz_val:-0}\", \"rss\": \"${rss_val:-0}\"}"
else
  PS_INFO_JSON="\"ps_info\": null"
fi

# perf 产物路径
PERF_DATA_JSON="null"; [[ -n "$USED_DATA_FILE" ]] && PERF_DATA_JSON="\"$USED_DATA_FILE\""
CALLCOUNT_JSON="null"; [[ -s "$CALLCOUNT_FILE" ]] && CALLCOUNT_JSON="\"$CALLCOUNT_FILE\""
DSO_JSON="null"; [[ -s "$DSO_FILE" ]] && DSO_JSON="\"$DSO_FILE\""

# 错误/权限信息（可选字段，以 printf %s 参数形式追加，避免数据被当作格式串解析）
# 落盘
{
  printf '{\n'
  printf '  "mode": "%s",\n' "$MODE"
  printf '  "command": %s,\n' "$COMMAND_JSON"
  printf '  "pid": %s,\n' "${TARGET_PID:-null}"
  printf '  "process_found": %s,\n' "$([[ $PROCESS_FOUND -eq 1 ]] && echo true || echo false)"
  printf '  %s,\n' "$PS_INFO_JSON"
  printf '  "libraries": %s,\n' "$LIBS_JSON"
  printf '  "environ": %s,\n' "$ENVIRON_JSON"
  printf '  "perf_available": %s,\n' "$PERF_AVAILABLE"
  printf '  "perf_degraded": "%s",\n' "$PERF_DEGRADED"
  printf '  "hotspots": %s,\n' "$HOTSPOTS_JSON"
  printf '  "perf_data": %s,\n' "$PERF_DATA_JSON"
  printf '  "perf_callcount_report": %s,\n' "$CALLCOUNT_JSON"
  printf '  "perf_dso_report": %s' "$DSO_JSON"
  if [[ -n "$PERF_ERROR" ]]; then
    PERF_ERROR_ESC=$(json_escape "$(printf '%s\n' "$PERF_ERROR" | head -3)")
    printf ',\n  "perf_error": "%s"' "$PERF_ERROR_ESC"
  fi
  if [[ "$NEEDS_AUTH" == "true" ]]; then
    printf ',\n  "needs_authorization": true, "auth_hints": ["sudo sysctl -w kernel.perf_event_paranoid=1", "sudo setcap cap_perfmon,cap_sys_ptrace=ep %s"]' "$(command -v perf)"
  fi
  printf '\n}\n'
} > "$REPORT_FILE"

# =========================================================================
# 7. 唯一标准输出: 报告文件绝对路径
# =========================================================================
echo "$REPORT_FILE"
