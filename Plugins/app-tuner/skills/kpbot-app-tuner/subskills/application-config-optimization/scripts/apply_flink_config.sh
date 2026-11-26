#!/usr/bin/env bash
set -euo pipefail

# apply_flink_config.sh — Flink 环境检测 + 推荐参数计算 + 输出候选命令 JSON
#
# 推荐器模式：只分析环境并输出推荐命令，不直接执行任何修改操作。
# 主框架读取 JSON 后通过安全门控执行 commands_execute 中的命令。
#
# 用法:
#   --target        目标容器名（如 flink_JM）
#   --apply-all     自动检测 JM+所有TM容器并批量分析
#   --flink-home    Flink 安装路径（默认 /usr/local/flink）
#   --config-file   配置文件名（默认 flink-conf.yaml）
#   --parallelism   手动指定 parallelism.default（可选）
#   --task-slots    手动指定 taskmanager.numberOfTaskSlots（可选）
#   --object-reuse  true/false/auto（默认 auto）
#   --mini-batch    true/false/auto（默认 auto）
#   --state-backend memory/rocksdb（默认 auto=memory）
#   --tm-per-container  每容器 TM 进程数（可选，自动检测）
#
# 输出: JSON 格式的候选动作列表（stdout）

TARGET=""
FLINK_HOME="/usr/local/flink"
CONFIG_FILE="flink-conf.yaml"
APPLY_ALL=false
PARALLELISM="auto"
TASK_SLOTS="auto"
OBJECT_REUSE="auto"
MINI_BATCH="auto"
STATE_BACKEND="auto"
ROLE="auto"
TM_PER_CONTAINER="auto"

while [[ $# -gt 0 ]]; do
  case $1 in
    --target)           TARGET="$2"; shift 2 ;;
    --flink-home)       FLINK_HOME="$2"; shift 2 ;;
    --config-file)      CONFIG_FILE="$2"; shift 2 ;;
    --apply-all)        APPLY_ALL=true; shift ;;
    --parallelism)      PARALLELISM="$2"; shift 2 ;;
    --task-slots)       TASK_SLOTS="$2"; shift 2 ;;
    --object-reuse)     OBJECT_REUSE="$2"; shift 2 ;;
    --mini-batch)       MINI_BATCH="$2"; shift 2 ;;
    --state-backend)    STATE_BACKEND="$2"; shift 2 ;;
    --role)             ROLE="$2"; shift 2 ;;
    --tm-per-container) TM_PER_CONTAINER="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 --target <container> [options]"
      echo "       $0 --apply-all [options]"
      echo ""
      echo "推荐器模式：输出候选命令 JSON，不执行修改。"
      exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$TARGET" && "$APPLY_ALL" != true ]]; then
  echo "Error: --target is required (or use --apply-all)" >&2
  exit 1
fi

CONFIG_PATH="${FLINK_HOME}/conf/${CONFIG_FILE}"

# ===========================================
# 环境检测函数
# ===========================================

get_container_cpus() {
  local target="$1"
  local nano_cpus
  nano_cpus=$(docker inspect "$target" --format '{{.HostConfig.NanoCpus}}' 2>/dev/null || echo "0")
  if [[ -n "$nano_cpus" && "$nano_cpus" != "0" && "$nano_cpus" != "<no value>" ]]; then
    echo $((nano_cpus / 1000000000)); return
  fi
  docker exec "$target" nproc 2>/dev/null || echo "0"
}

get_container_memory_mb() {
  local target="$1"
  local memory_bytes
  memory_bytes=$(docker inspect "$target" --format '{{.HostConfig.Memory}}' 2>/dev/null || echo "0")
  if [[ "$memory_bytes" != "0" && -n "$memory_bytes" ]]; then
    echo $((memory_bytes / 1024 / 1024))
  else echo "0"; fi
}

get_tm_containers() {
  docker ps --format '{{.Names}}' 2>/dev/null | grep -iE 'flink.*TM|flink.*taskmanager' | grep -vi 'velox' || echo ""
}

get_tm_procs_in_container() {
  local target="$1" count
  count=$(docker exec "$target" bash -c 'ps aux | grep -c [T]askManagerRunner' 2>/dev/null || echo "0")
  count=$(echo "$count" | tail -1 | tr -d '[:space:]')
  echo "${count:-0}"
}

get_jm_container() {
  docker ps --format '{{.Names}}' 2>/dev/null | grep -iE 'flink.*JM|flink.*jobmanager' | grep -vi 'velox' | head -1 || echo ""
}

get_jm_address() {
  local jm_container
  jm_container=$(docker ps --format '{{.Names}} {{.Ports}}' 2>/dev/null | grep "8081" | awk '{print $1}' | head -1)
  if [[ -n "$jm_container" ]]; then
    docker inspect "$jm_container" --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' 2>/dev/null
  else echo "localhost"; fi
}

detect_role() {
  local target="$1"
  if [[ "$ROLE" != "auto" ]]; then echo "$ROLE"; return; fi
  if echo "$target" | grep -qiE 'flink.*jm|flink.*jobmanager'; then echo "jobmanager"; return; fi
  if echo "$target" | grep -qiE 'flink.*tm|flink.*taskmanager'; then echo "taskmanager"; return; fi
  local ports
  ports=$(docker inspect "$target" --format '{{.NetworkSettings.Ports}}' 2>/dev/null || echo "")
  if echo "$ports" | grep -q "8081"; then echo "jobmanager"; else echo "taskmanager"; fi
}

# ===========================================
# 参数计算函数
# ===========================================

calc_parallelism() {
  local cores="$1"
  if [[ "$PARALLELISM" != "auto" ]]; then echo "$PARALLELISM"; return; fi
  if [[ "$cores" -le 8 ]]; then echo "$cores"; else echo $((cores / 2)); fi
}

calc_task_slots() {
  local parallelism="$1" tm_count="$2" tm_procs="$3"
  if [[ "$TASK_SLOTS" != "auto" ]]; then echo "$TASK_SLOTS"; return; fi
  [[ "$tm_procs" -lt 1 ]] && tm_procs=1
  local slots=$((parallelism / tm_count / tm_procs))
  [[ "$slots" -lt 1 ]] && slots=1
  echo "$slots"
}

resolve_object_reuse() {
  case "${1:-auto}" in true) echo "true";; false) echo "false";; *)
    case "${2:-auto}" in rocksdb|RocksDB|ROCKSDB) echo "false";; *) echo "true";; esac
  esac
}

resolve_mini_batch() {
  case "${1:-auto}" in auto|true) echo "true";; false) echo "false";; *) echo "true";; esac
}

get_current_config_value() {
  local container="$1" key="$2" val
  val=$(docker exec "$container" bash -c "grep -E '^[[:space:]]*${key}[[:space:]:]' ${CONFIG_PATH} 2>/dev/null | grep -v '^[[:space:]]*#' | head -1" 2>/dev/null || echo "")
  if [[ -z "$val" ]]; then echo "缺失"; else echo "$val" | sed -E "s/^[[:space:]]*${key}[[:space:]:]+//"; fi
}

# ===========================================
# 配置内容生成
# ===========================================

generate_jm_config() {
  local parallelism="$1" jm_address="$2" object_reuse="$3" mini_batch="$4"
  cat << EOF
# Flink 推荐配置 - $(date +%Y-%m-%d_%H:%M:%S) - JobManager
jobmanager.rpc.address: ${jm_address}
parallelism.default: ${parallelism}
pipeline.object-reuse: ${object_reuse}
table.exec.mini-batch.enabled: ${mini_batch}
table.exec.mini-batch.allow-latency: 2s
table.exec.mini-batch.size: 50000
EOF
}

generate_tm_config() {
  local task_slots="$1" tm_memory="$2" jm_address="$3" object_reuse="$4" mini_batch="$5"
  cat << EOF
# Flink 推荐配置 - $(date +%Y-%m-%d_%H:%M:%S) - TaskManager
jobmanager.rpc.address: ${jm_address}
taskmanager.numberOfTaskSlots: ${task_slots}
taskmanager.memory.process.size: ${tm_memory}
pipeline.object-reuse: ${object_reuse}
table.exec.mini-batch.enabled: ${mini_batch}
table.exec.mini-batch.allow-latency: 2s
table.exec.mini-batch.size: 50000
EOF
}

# ===========================================
# JSON 辅助
# ===========================================

json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\n'/\\n}"
  echo "$s"
}

# ===========================================
# 主流程
# ===========================================

main() {
  local jm_container tm_containers jm_addr
  jm_container=$(get_jm_container)
  tm_containers=$(get_tm_containers)
  jm_addr=$(get_jm_address)

  local total_tm_cpus=0
  local tm_count=0
  if [[ -n "$tm_containers" ]]; then
    for tm in $tm_containers; do
      local cpus
      cpus=$(get_container_cpus "$tm")
      total_tm_cpus=$((total_tm_cpus + cpus))
      tm_count=$((tm_count + 1))
    done
  fi
  [[ "$tm_count" -lt 1 ]] && tm_count=1

  local obj_reuse mini_batch_enabled
  obj_reuse=$(resolve_object_reuse "$OBJECT_REUSE" "$STATE_BACKEND")
  mini_batch_enabled=$(resolve_mini_batch "$MINI_BATCH")

  local rec_parallelism
  rec_parallelism=$(calc_parallelism "$total_tm_cpus")

  echo "{"
  echo "  \"skill\": \"apply_flink_config\","
  echo "  \"environment\": {"
  echo "    \"jm_container\": \"${jm_container}\","
  echo "    \"tm_containers\": \"${tm_containers}\","
  echo "    \"tm_count\": ${tm_count},"
  echo "    \"total_tm_cpus\": ${total_tm_cpus},"
  echo "    \"jm_address\": \"${jm_addr}\","
  echo "    \"flink_home\": \"${FLINK_HOME}\","
  echo "    \"config_path\": \"${CONFIG_PATH}\""
  echo "  },"
  echo "  \"recommended_params\": {"
  echo "    \"parallelism.default\": \"${rec_parallelism}\","
  echo "    \"pipeline.object-reuse\": \"${obj_reuse}\","
  echo "    \"table.exec.mini-batch.enabled\": \"${mini_batch_enabled}\","
  echo "    \"table.exec.mini-batch.allow-latency\": \"2s\","
  echo "    \"table.exec.mini-batch.size\": \"50000\""
  echo "  },"
  echo "  \"actions\": ["

  local first_action=true

  # JM 容器配置动作
  if [[ -n "$jm_container" ]]; then
    local jm_config
    jm_config=$(generate_jm_config "$rec_parallelism" "$jm_addr" "$obj_reuse" "$mini_batch_enabled")

    local cur_parallelism
    cur_parallelism=$(get_current_config_value "$jm_container" "parallelism.default")

    [[ "$first_action" == false ]] && echo "    ,"
    echo "    {"
    echo "      \"action_id\": \"flink-config-${jm_container}\","
    echo "      \"action_type\": \"config_write\","
    echo "      \"description\": \"写入 Flink JM 推荐配置到 ${jm_container}:${CONFIG_PATH}\","
    echo "      \"role\": \"jobmanager\","
    echo "      \"config_content\": \"$(json_escape "$jm_config")\","
    echo "      \"current_parallelism\": \"${cur_parallelism}\","
    echo "      \"commands_execute\": ["
    echo "        \"docker exec ${jm_container} bash -c 'cp ${CONFIG_PATH} ${CONFIG_PATH}.bak.\\\$(date +%Y%m%d_%H%M%S) 2>/dev/null || true'\","
    echo "        \"printf '%s\\\\n' \\\"$(json_escape "$jm_config")\\\" | docker exec -i ${jm_container} bash -c 'cat > ${CONFIG_PATH}'\""
    echo "      ],"
    echo "      \"restart_required\": true,"
    echo "      \"restart_commands\": ["
    echo "        \"docker exec ${jm_container} bash -c '${FLINK_HOME}/bin/stop-cluster.sh 2>/dev/null || true'\","
    echo "        \"docker exec ${jm_container} bash -c '${FLINK_HOME}/bin/start-cluster.sh'\""
    echo "      ],"
    echo "      \"rollback\": ["
    echo "        \"docker exec ${jm_container} bash -c 'cp ${CONFIG_PATH}.bak.* ${CONFIG_PATH} 2>/dev/null || true'\""
    echo "      ],"
    echo "      \"risk\": \"medium\""
    echo "    }"
    first_action=false
  fi

  # TM 容器配置动作
  if [[ -n "$tm_containers" ]]; then
    for tm in $tm_containers; do
      local tm_cpus tm_mem tm_procs rec_slots rec_memory tm_config
      tm_cpus=$(get_container_cpus "$tm")
      tm_mem=$(get_container_memory_mb "$tm")
      tm_procs=$(get_tm_procs_in_container "$tm")
      [[ "$tm_procs" -lt 1 ]] && tm_procs=1

      if [[ "$TM_PER_CONTAINER" != "auto" ]]; then tm_procs="$TM_PER_CONTAINER"; fi

      rec_slots=$(calc_task_slots "$rec_parallelism" "$tm_count" "$tm_procs")
      rec_memory="$((tm_mem / tm_procs))m"

      tm_config=$(generate_tm_config "$rec_slots" "$rec_memory" "$jm_addr" "$obj_reuse" "$mini_batch_enabled")

      local cur_slots
      cur_slots=$(get_current_config_value "$tm" "taskmanager.numberOfTaskSlots")

      [[ "$first_action" == false ]] && echo "    ,"
      echo "    {"
      echo "      \"action_id\": \"flink-config-${tm}\","
      echo "      \"action_type\": \"config_write\","
      echo "      \"description\": \"写入 Flink TM 推荐配置到 ${tm}:${CONFIG_PATH}\","
      echo "      \"role\": \"taskmanager\","
      echo "      \"tm_cpus\": ${tm_cpus},"
      echo "      \"tm_memory_mb\": ${tm_mem},"
      echo "      \"tm_procs\": ${tm_procs},"
      echo "      \"config_content\": \"$(json_escape "$tm_config")\","
      echo "      \"current_task_slots\": \"${cur_slots}\","
      echo "      \"recommended_task_slots\": \"${rec_slots}\","
      echo "      \"recommended_tm_memory\": \"${rec_memory}\","
      echo "      \"commands_execute\": ["
      echo "        \"docker exec ${tm} bash -c 'cp ${CONFIG_PATH} ${CONFIG_PATH}.bak.\\\$(date +%Y%m%d_%H%M%S) 2>/dev/null || true'\","
      echo "        \"printf '%s\\\\n' \\\"$(json_escape "$tm_config")\\\" | docker exec -i ${tm} bash -c 'cat > ${CONFIG_PATH}'\""
      echo "      ],"
      echo "      \"restart_required\": true,"
      echo "      \"restart_commands\": ["
      echo "        \"docker exec ${tm} bash -c 'kill \\\$(ps aux | grep TaskManagerRunner | grep -v grep | awk \\\"{print \\\\\\\$2}\\\") 2>/dev/null || true'\","
      echo "        \"docker exec -d ${tm} bash -c 'nohup ${FLINK_HOME}/bin/taskmanager.sh start-foreground > /dev/null 2>&1 &'\""
      echo "      ],"
      echo "      \"rollback\": ["
      echo "        \"docker exec ${tm} bash -c 'cp ${CONFIG_PATH}.bak.* ${CONFIG_PATH} 2>/dev/null || true'\""
      echo "      ],"
      echo "      \"risk\": \"medium\""
      echo "    }"
      first_action=false
    done
  fi

  echo "  ]"
  echo "}"
}

main
