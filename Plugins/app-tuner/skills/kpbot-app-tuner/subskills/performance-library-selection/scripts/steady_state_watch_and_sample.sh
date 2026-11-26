#!/bin/bash
# steady_state_watch_and_sample.sh — 稳态监视 + 立即在线采样（单脚本闭环）
#
# 解决短稳态窗口采样竞态：检测到稳态与启动采样在同一脚本内完成，
# 消除"看到稳态标记后再人工/Agent 串行启动采样"的窗口流失（实测可错失整个稳态段）。
# 配合 SKILL.md Step 1.3 "短稳态窗口在线采样配方"使用：目标 PID 须在编译/预热期预锁。
#
# 用法:
#   bash steady_state_watch_and_sample.sh --pid <PID> (--log <日志> --pattern <稳态正则> | --npu-smi) \
#        [--sampling-duration <秒>] [--timeout <秒>] [--interval <秒>] [--script-dir <目录>]
#
# 参数:
#   --pid <PID>              目标进程 PID（预锁，见 SKILL.md 采样配方；在线只读，不 kill）
#   --log <路径>             业务日志路径（轮询目标；行内容自动 ANSI 色码剥离）
#   --pattern <正则>         稳态标记正则（如 "step: +2 +loss"，匹配剥离后文本）
#   --npu-smi                并行启用 npu-smi AICore 利用率信号（任一 chip AICore>0 即活跃；
#                            比日志更实时——日志有块缓冲落盘延迟，NPU 活跃即采样价值窗口）
#   --sampling-duration <秒> 采样时长，透传 run_and_profile.sh（默认 20）
#   --timeout <秒>           稳态等待超时（默认 600）
#   --interval <秒>          轮询间隔（默认 5）
#   --script-dir <目录>      run_and_profile.sh 所在目录（默认本脚本目录）
#
# 触发条件: 日志模式命中 或 npu-smi AICore 活跃（任一，先到先触发）
# 输出契约: stdout 仅 run_and_profile.sh 的 JSON 报告绝对路径；诊断信息走 stderr
# 退出码:   0=采样完成（含目标缺失的标准错误报告）；1=稳态未触发/参数错误/目标先退出
#
# 红线合规: 只读监视（kill -0 / 读日志 / npu-smi info），不启动也不 kill 业务进程；
#           与 run_and_profile.sh 仅通过文本参数（PID/时长）交互，无跨脚本进程状态依赖。

set -uo pipefail

PID=""
LOG=""
PATTERN=""
DUR=20
TIMEOUT=600
INTERVAL=5
USE_NPUSMI=false
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pid)               PID="$2"; shift 2 ;;
    --log)               LOG="$2"; shift 2 ;;
    --pattern)           PATTERN="$2"; shift 2 ;;
    --sampling-duration) DUR="$2"; shift 2 ;;
    --timeout)           TIMEOUT="$2"; shift 2 ;;
    --interval)          INTERVAL="$2"; shift 2 ;;
    --npu-smi)           USE_NPUSMI=true; shift ;;
    --script-dir)        SCRIPT_DIR="$2"; shift 2 ;;
    *) echo "[watch] 未知参数: $1" >&2; exit 1 ;;
  esac
done

SAMPLER="$SCRIPT_DIR/run_and_profile.sh"
[[ -f "$SAMPLER" ]] || { echo "[watch] 找不到采样器: $SAMPLER" >&2; exit 1; }
[[ "$PID" =~ ^[0-9]+$ ]] || { echo "[watch] 需要有效 --pid <PID>" >&2; exit 1; }
if [[ -n "$LOG" || -n "$PATTERN" ]]; then
  [[ -n "$LOG" && -n "$PATTERN" ]] || { echo "[watch] --log 与 --pattern 须成对提供" >&2; exit 1; }
  [[ -f "$LOG" ]] || { echo "[watch] 日志不存在: $LOG" >&2; exit 1; }
fi
if [[ "$USE_NPUSMI" == "true" ]] && ! command -v npu-smi &>/dev/null; then
  echo "[watch] npu-smi 不可用" >&2; exit 1
fi
[[ -n "$LOG" || "$USE_NPUSMI" == "true" ]] || { echo "[watch] 须提供 --log/--pattern 或 --npu-smi 至少一个稳态信号" >&2; exit 1; }

kill -0 "$PID" 2>/dev/null || { echo "[watch] 目标进程 $PID 不存在" >&2; exit 1; }

# 目标存活判定: kill -0 对僵尸进程(父进程未收割, 容器 PID1 不 reap 时常见)也返回成功,
# 须叠加进程状态检查——僵尸态视为已退出, 避免对死目标空轮询到超时
target_alive() {
  kill -0 "$PID" 2>/dev/null || return 1
  local st
  st=$(ps -o stat= -p "$PID" 2>/dev/null | tr -d ' ')
  [[ "$st" != Z* && -n "$st" ]]
}
target_alive || { echo "[watch] 目标进程 $PID 不存在或已退出（僵尸态）" >&2; exit 1; }

log_hit() {
  sed 's/\x1b\[[0-9;]*m//g' "$LOG" 2>/dev/null | grep -qE "$PATTERN"
}

npu_active() {
  npu-smi info 2>/dev/null | awk -F'|' '
    $3 ~ /:[0-9A-Fa-f]{2}\.[0-9]/ { split($4, a, " "); if (a[1]+0 > 0) { f=1; exit } }
    END { exit !f }'
}

echo "[watch] 目标 PID=$PID 信号: log=${PATTERN:-无} npu-smi=$($USE_NPUSMI && echo on || echo off) 超时=${TIMEOUT}s" >&2

ELAPSED=0
while (( ELAPSED < TIMEOUT )); do
  if ! target_alive; then
    echo "[watch] 目标进程 $PID 在稳态触发前退出（已等待 ${ELAPSED}s，含僵尸态）" >&2
    exit 1
  fi
  TRIGGER=""
  if [[ -n "$LOG" ]] && log_hit; then
    TRIGGER="log-pattern"
  elif [[ "$USE_NPUSMI" == "true" ]] && npu_active; then
    TRIGGER="npu-smi-aicore"
  fi
  if [[ -n "$TRIGGER" ]]; then
    echo "[watch] 稳态触发 (${TRIGGER}) 于 ${ELAPSED}s，立即采样 ${DUR}s" >&2
    bash "$SAMPLER" --pid "$PID" --sampling-duration "$DUR"
    exit $?
  fi
  sleep "$INTERVAL"
  ELAPSED=$((ELAPSED + INTERVAL))
done

echo "[watch] ${TIMEOUT}s 内未检测到稳态信号（日志模式未命中且 AICore 未活跃）" >&2
exit 1
