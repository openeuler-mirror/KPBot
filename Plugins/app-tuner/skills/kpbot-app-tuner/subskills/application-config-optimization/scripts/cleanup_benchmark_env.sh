#!/usr/bin/env bash
set -euo pipefail

# cleanup_benchmark_env.sh — 输出 benchmark 环境清理命令 JSON
#
# 推荐器模式：只输出推荐命令，不直接执行。
# 主框架读取 JSON 后通过安全门控执行。
#
# 用法:
#   cleanup_benchmark_env.sh [--jm-host <host>] [--flink-home <path>]
#
# 输出: JSON 格式的候选动作列表（stdout）

JM_HOST=""
FLINK_HOME="${FLINK_HOME:-/usr/local/flink}"

while [[ $# -gt 0 ]]; do
  case $1 in
    --jm-host)     JM_HOST="$2"; shift 2 ;;
    --flink-home)  FLINK_HOME="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 [--jm-host <host>] [--flink-home <path>]"
      echo "推荐器模式：输出清理命令 JSON，不执行修改。"
      exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

SSH_PREFIX=""
if [[ -n "$JM_HOST" ]]; then
  SSH_PREFIX="ssh -o StrictHostKeyChecking=no -o ConnectTimeout=30 ${JM_HOST}"
fi

cat << EOF
{
  "skill": "cleanup_benchmark_env",
  "description": "清理 benchmark 测试环境中的残留进程和端口占用",
  "target": "${JM_HOST:-localhost}",
  "actions": [
    {
      "action_id": "cleanup-benchmark-processes",
      "action_type": "process_kill",
      "description": "清理残留的 benchmark 进程",
      "commands_execute": [
        "${SSH_PREFIX} pkill -9 -f Benchmark 2>/dev/null || true",
        "${SSH_PREFIX} pkill -9 -f CpuMetricSender 2>/dev/null || true",
        "${SSH_PREFIX} pkill -9 -f CpuMetricReceiver 2>/dev/null || true",
        "${SSH_PREFIX} pkill -9 -f zdl 2>/dev/null || true",
        "${SSH_PREFIX} pkill -9 -f zdl.sh 2>/dev/null || true"
      ],
      "rollback": [],
      "risk": "high",
      "validation": "${SSH_PREFIX} ps aux | grep -E 'Benchmark|zdl|CpuMetric' | grep -v grep | wc -l"
    },
    {
      "action_id": "cleanup-port-9098",
      "action_type": "process_kill",
      "description": "清理 9098 端口占用",
      "commands_execute": [
        "${SSH_PREFIX} for pid in \$(lsof -t -i:9098 2>/dev/null || true); do kill -9 \$pid 2>/dev/null || true; done"
      ],
      "rollback": [],
      "risk": "medium",
      "validation": "${SSH_PREFIX} lsof -i :9098 2>/dev/null | grep -v COMMAND | wc -l"
    },
    {
      "action_id": "stop-flink-cluster",
      "action_type": "service_stop",
      "description": "停止 Flink 集群",
      "commands_execute": [
        "${SSH_PREFIX} ${FLINK_HOME}/bin/stop-cluster.sh 2>/dev/null || true"
      ],
      "rollback": [
        "${SSH_PREFIX} ${FLINK_HOME}/bin/start-cluster.sh"
      ],
      "risk": "high",
      "validation": "${SSH_PREFIX} curl -s http://localhost:8081/overview 2>/dev/null | wc -c"
    },
    {
      "action_id": "cleanup-tm-processes",
      "action_type": "process_kill",
      "description": "清理残留 TaskManager 进程",
      "commands_execute": [
        "${SSH_PREFIX} pkill -9 -f TaskManagerRunner 2>/dev/null || true"
      ],
      "rollback": [],
      "risk": "high",
      "validation": "${SSH_PREFIX} ps aux | grep TaskManagerRunner | grep -v grep | wc -l"
    },
    {
      "action_id": "restart-flink-cluster",
      "action_type": "service_restart",
      "description": "重启 Flink 集群",
      "commands_execute": [
        "${SSH_PREFIX} ${FLINK_HOME}/bin/start-cluster.sh"
      ],
      "rollback": [
        "${SSH_PREFIX} ${FLINK_HOME}/bin/stop-cluster.sh 2>/dev/null || true"
      ],
      "risk": "medium",
      "validation": "${SSH_PREFIX} curl -s http://localhost:8081/taskmanagers 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); tms=d[\"taskmanagers\"]; print(f\"TM: {len(tms)}, slots: {sum(t[\"slotsNumber\"] for t in tms)}\")' 2>/dev/null || echo unable"
    }
  ]
}
EOF
