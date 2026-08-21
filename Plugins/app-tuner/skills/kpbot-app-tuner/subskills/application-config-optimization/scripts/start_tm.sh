#!/usr/bin/env bash
set -euo pipefail

# start_tm.sh — 输出启动 Flink TaskManager 进程的命令 JSON
#
# 推荐器模式：只输出推荐命令，不直接执行。
# 合并了原 start-multiple-tm.sh 和 start-tm-cluster.sh（两者功能重叠）。
#
# 用法:
#   start_tm.sh --tm-count <N> [--flink-home <path>] [--container <name>]
#
# 输出: JSON 格式的候选动作列表（stdout）

TM_COUNT="${TM_COUNT:-4}"
FLINK_HOME="${FLINK_HOME:-/usr/local/flink-1.19.2}"
CONTAINER=""

while [[ $# -gt 0 ]]; do
  case $1 in
    --tm-count)   TM_COUNT="$2"; shift 2 ;;
    --flink-home) FLINK_HOME="$2"; shift 2 ;;
    --container)  CONTAINER="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 --tm-count <N> [--flink-home <path>] [--container <name>]"
      echo "推荐器模式：输出 TM 启动命令 JSON，不执行修改。"
      exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

local_prefix=""
if [[ -n "$CONTAINER" ]]; then
  local_prefix="docker exec ${CONTAINER}"
fi

# 生成启动命令列表
start_cmds=""
for i in $(seq 0 $((TM_COUNT - 1))); do
  if [[ -n "$start_cmds" ]]; then start_cmds+=","; fi
  start_cmds+="\"${local_prefix} HOSTNAME=tm-${i} FLINK_CONF_DIR=${FLINK_HOME}/conf FLINK_LOG_DIR=${FLINK_HOME}/log ${FLINK_HOME}/bin/flink-daemon.sh start taskexecutor\""
done

cat << EOF
{
  "skill": "start_tm",
  "description": "启动指定数量的 Flink TaskManager 进程",
  "tm_count": ${TM_COUNT},
  "flink_home": "${FLINK_HOME}",
  "container": "${CONTAINER:-localhost}",
  "actions": [
    {
      "action_id": "stop-existing-tm",
      "action_type": "process_kill",
      "description": "停止现有 TaskManager 进程",
      "commands_execute": [
        "${local_prefix} pkill -9 -f TaskManagerRunner 2>/dev/null || true"
      ],
      "rollback": [],
      "risk": "high",
      "validation": "${local_prefix} ps aux | grep TaskManagerRunner | grep -v grep | wc -l"
    },
    {
      "action_id": "start-tm-instances",
      "action_type": "process_start",
      "description": "启动 ${TM_COUNT} 个 TaskManager 实例",
      "commands_execute": [
        ${start_cmds}
      ],
      "rollback": [
        "${local_prefix} pkill -9 -f TaskManagerRunner 2>/dev/null || true"
      ],
      "risk": "medium",
      "validation": "${local_prefix} ps aux | grep TaskManagerRunner | grep -v grep | wc -l"
    }
  ]
}
EOF
