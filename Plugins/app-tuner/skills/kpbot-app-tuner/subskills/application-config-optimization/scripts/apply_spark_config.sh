#!/usr/bin/env bash
set -euo pipefail

# apply_spark_config.sh — Spark 环境检测 + 推荐参数计算 + 输出候选命令 JSON
#
# 推荐器模式：只分析环境并输出推荐命令，不直接执行任何修改操作。
# 主框架读取 JSON 后通过安全门控执行 commands_execute 中的命令。
#
# 用法:
#   --target           目标容器名或 SSH 主机
#   --apply-all        自动检测 Spark 容器并批量分析 (仅容器模式)
#   --spark-home       Spark 安装路径（默认 /usr/local/spark）
#   --config-file      配置文件名（默认 spark-defaults.conf）
#   --deploy-mode      部署方式: docker（默认）或 ssh（物理机单机）
#   --spark-mode       Spark 模式: yarn/standalone/auto（默认 auto）
#   --driver-memory    手动指定 driver 内存（可选）
#   --executor-instances 手动指定 executor 数量（可选）
#   --executor-cores   手动指定 executor 核数（可选）
#   --executor-memory  手动指定 executor 内存（可选）
#
# 输出: JSON 格式的候选动作列表（stdout）

TARGET=""
SPARK_HOME="/usr/local/spark"
CONFIG_FILE="spark-defaults.conf"
DEPLOY_MODE="docker"
SPARK_MODE="auto"
APPLY_ALL=false
MANUAL_DRIVER_MEMORY="auto"
MANUAL_EXECUTOR_INSTANCES="auto"
MANUAL_EXECUTOR_CORES="auto"
MANUAL_EXECUTOR_MEMORY="auto"

while [[ $# -gt 0 ]]; do
  case $1 in
    --target)             TARGET="$2"; shift 2 ;;
    --spark-home)         SPARK_HOME="$2"; shift 2 ;;
    --config-file)        CONFIG_FILE="$2"; shift 2 ;;
    --deploy-mode)        DEPLOY_MODE="$2"; shift 2 ;;
    --spark-mode)         SPARK_MODE="$2"; shift 2 ;;
    --apply-all)          APPLY_ALL=true; shift ;;
    --driver-memory)      MANUAL_DRIVER_MEMORY="$2"; shift 2 ;;
    --executor-instances) MANUAL_EXECUTOR_INSTANCES="$2"; shift 2 ;;
    --executor-cores)     MANUAL_EXECUTOR_CORES="$2"; shift 2 ;;
    --executor-memory)    MANUAL_EXECUTOR_MEMORY="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 --target <container|host> [options]"
      echo "       $0 --apply-all [options]  (仅容器模式)"
      echo ""
      echo "推荐器模式：输出候选命令 JSON，不执行修改。"
      echo "主框架读取 JSON 后通过安全门控执行。"
      exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$TARGET" && "$APPLY_ALL" != true ]]; then
  echo "Error: --target is required (or use --apply-all for auto-detection)" >&2
  exit 1
fi

CONFIG_PATH="${SPARK_HOME}/conf/${CONFIG_FILE}"

# ===========================================
# 环境检测函数
# ===========================================

detect_container() {
  local target="$1"
  if [[ "$DEPLOY_MODE" == "docker" ]]; then
    docker inspect "$target" &>/dev/null && return 0 || return 1
  fi
  ssh -o ConnectTimeout=5 "$target" 'cat /proc/1/cgroup 2>/dev/null' 2>/dev/null | grep -qE "(docker|containerd)" && return 0 || return 1
}

get_target_cpus() {
  local target="$1"
  if [[ "$DEPLOY_MODE" == "docker" ]]; then
    local nano_cpus
    nano_cpus=$(docker inspect "$target" --format '{{.HostConfig.NanoCpus}}' 2>/dev/null || echo "0")
    if [[ -n "$nano_cpus" && "$nano_cpus" != "0" && "$nano_cpus" != "<no value>" ]]; then
      echo $((nano_cpus / 1000000000)); return
    fi
    docker exec "$target" nproc 2>/dev/null || echo "0"
  else
    ssh "$target" nproc 2>/dev/null || echo "0"
  fi
}

get_target_memory_mb() {
  local target="$1"
  if [[ "$DEPLOY_MODE" == "docker" ]]; then
    local memory_bytes
    memory_bytes=$(docker inspect "$target" --format '{{.HostConfig.Memory}}' 2>/dev/null || echo "0")
    if [[ "$memory_bytes" != "0" && -n "$memory_bytes" ]]; then
      echo $((memory_bytes / 1024 / 1024))
    else echo "0"; fi
  else
    local memory_kb
    memory_kb=$(ssh "$target" 'grep MemTotal /proc/meminfo' 2>/dev/null | awk '{print $2}' || echo "0")
    echo $((memory_kb / 1024))
  fi
}

get_target_ip() {
  local target="$1"
  if [[ "$DEPLOY_MODE" == "docker" ]]; then
    docker inspect "$target" --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' 2>/dev/null || echo "127.0.0.1"
  else
    echo "${target##*@}"
  fi
}

read_config_value() {
  local target="$1" config_path="$2" key="$3" val
  if [[ "$DEPLOY_MODE" == "docker" ]]; then
    val=$(docker exec "$target" bash -c "grep -E '^[[:space:]]*${key}[[:space:]]' ${config_path} 2>/dev/null | grep -v '^[[:space:]]*#' | head -1" 2>/dev/null || echo "")
  else
    val=$(ssh -o ConnectTimeout=5 "$target" "grep -E '^[[:space:]]*${key}[[:space:]]' ${config_path} 2>/dev/null | grep -v '^[[:space:]]*#' | head -1" 2>/dev/null || echo "")
  fi
  if [[ -z "$val" ]]; then echo "缺失"; else echo "$val" | sed -E "s/^[[:space:]]*${key}[[:space:]]+//"; fi
}

get_spark_containers() {
  docker ps --format '{{.Names}}' 2>/dev/null | grep -iE 'spark' | grep -vi 'velox' || echo ""
}

get_spark_primary() {
  if [[ "$DEPLOY_MODE" == "docker" ]]; then
    local master
    master=$(docker ps --format '{{.Names}}' 2>/dev/null | grep -iE 'spark.*master|spark.*driver' | grep -vi 'velox' | head -1)
    if [[ -n "$master" ]]; then echo "$master"; return; fi
    for c in $(docker ps --format '{{.Names}}' 2>/dev/null | grep -iE 'spark' | grep -vi 'velox'); do
      local ports
      ports=$(docker inspect "$c" --format '{{.NetworkSettings.Ports}}' 2>/dev/null || echo "")
      if echo "$ports" | grep -qE "7077|8080"; then echo "$c"; return; fi
    done
    get_spark_containers | head -1
  else
    echo "$TARGET"
  fi
}

get_spark_workers() {
  if [[ "$DEPLOY_MODE" == "docker" ]]; then
    docker ps --format '{{.Names}}' 2>/dev/null | grep -iE 'spark.*worker|spark.*slave' | grep -vi 'velox' || echo ""
  else echo ""; fi
}

detect_spark_mode() {
  if [[ "$SPARK_MODE" != "auto" ]]; then echo "$SPARK_MODE"; return; fi
  local workers
  workers=$(get_spark_workers)
  if [[ -n "$workers" ]]; then echo "standalone"; return; fi
  if [[ "$DEPLOY_MODE" == "docker" ]]; then
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -qiE 'yarn|nodemanager'; then echo "yarn"; return; fi
  else
    if [[ -n "$TARGET" ]] && ssh -o ConnectTimeout=5 "$TARGET" 'ps aux | grep -q [N]odeManager' 2>/dev/null; then echo "yarn"; return; fi
  fi
  echo "standalone"
}

# ===========================================
# 参数计算函数
# ===========================================

calc_driver_memory() {
  if [[ "$MANUAL_DRIVER_MEMORY" != "auto" ]]; then echo "$MANUAL_DRIVER_MEMORY"; return; fi
  echo "8g"
}

calc_executor_instances() {
  local total_vcores="$1" env_type="$2"
  if [[ "$MANUAL_EXECUTOR_INSTANCES" != "auto" ]]; then echo "$MANUAL_EXECUTOR_INSTANCES"; return; fi
  if [[ "$env_type" == "physical" ]]; then echo "24"
  elif [[ "$total_vcores" -ge 64 ]]; then echo "12"
  else
    local instances=$((total_vcores / 4))
    [[ "$instances" -lt 2 ]] && instances=2
    echo "$instances"
  fi
}

calc_executor_cores() {
  local total_vcores="$1" instances="$2"
  if [[ "$MANUAL_EXECUTOR_CORES" != "auto" ]]; then echo "$MANUAL_EXECUTOR_CORES"; return; fi
  local cores=$((total_vcores / instances))
  [[ "$cores" -lt 1 ]] && cores=1
  echo "$cores"
}

calc_executor_memory() {
  local total_memory_mb="$1" instances="$2" driver_mem_str="${3:-8g}"
  if [[ "$MANUAL_EXECUTOR_MEMORY" != "auto" ]]; then echo "$MANUAL_EXECUTOR_MEMORY"; return; fi
  local driver_mb
  if [[ "$driver_mem_str" =~ ^([0-9]+)g$ ]]; then driver_mb=$((${BASH_REMATCH[1]} * 1024))
  elif [[ "$driver_mem_str" =~ ^([0-9]+)m$ ]]; then driver_mb=${BASH_REMATCH[1]}
  else driver_mb=8192; fi
  local available_mb=$((total_memory_mb * 95 / 100 - driver_mb))
  local exec_mem=$((available_mb / instances))
  [[ "$exec_mem" -lt 1024 ]] && exec_mem=1024
  echo "${exec_mem}m"
}

# ===========================================
# 配置内容生成
# ===========================================

generate_spark_config() {
  local driver_memory="$1" executor_instances="$2" executor_cores="$3" executor_memory="$4"
  cat << EOF
# Spark 推荐配置 - $(date +%Y-%m-%d_%H:%M:%S)
spark.driver.memory                     ${driver_memory}
spark.executor.instances                ${executor_instances}
spark.executor.cores                    ${executor_cores}
spark.executor.memory                   ${executor_memory}
spark.sql.autoBroadcastJoinThreshold                                  100m
spark.sql.shuffle.partitions                                           600
spark.sql.optimizer.runtime.bloomFilter.applicationSideScanSizeThreshold  0
spark.sql.sources.parallelPartitionDiscovery.parallelism                60
spark.executor.extraJavaOptions        -XX:+UseG1GC -XX:ParallelGCThread=4 -XX:MetaspaceSize=256m -XX:+UseBiasedLocking
EOF
}

# ===========================================
# JSON 输出函数
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
  local spark_mode primary_container worker_containers=""

  if [[ "$APPLY_ALL" == true ]]; then
    if [[ "$DEPLOY_MODE" == "ssh" ]]; then
      echo '{"error": "--apply-all 仅支持容器模式 (docker)"}' >&2
      exit 1
    fi
    spark_mode=$(detect_spark_mode)
    primary_container=$(get_spark_primary)
    if [[ -z "$primary_container" ]]; then
      echo '{"error": "未找到 Spark 主容器"}' >&2
      exit 1
    fi
    if [[ "$spark_mode" == "standalone" ]]; then
      worker_containers=$(get_spark_workers)
    fi
  else
    spark_mode=$(detect_spark_mode)
    primary_container="$TARGET"
  fi

  # 环境检测
  local env_type="container"
  if ! detect_container "$primary_container" 2>/dev/null; then
    env_type="physical"
  fi

  local primary_cpus primary_mem
  primary_cpus=$(get_target_cpus "$primary_container")
  primary_mem=$(get_target_memory_mb "$primary_container")

  local total_vcores=$primary_cpus
  local total_memory_mb=$primary_mem

  if [[ -n "$worker_containers" ]]; then
    for w in $worker_containers; do
      local w_cpus w_mem
      w_cpus=$(get_target_cpus "$w")
      w_mem=$(get_target_memory_mb "$w")
      total_vcores=$((total_vcores + w_cpus))
      total_memory_mb=$((total_memory_mb + w_mem))
    done
  fi

  # 参数计算
  local rec_driver rec_instances rec_cores rec_memory
  rec_driver=$(calc_driver_memory)
  rec_instances=$(calc_executor_instances "$total_vcores" "$env_type")
  rec_cores=$(calc_executor_cores "$total_vcores" "$rec_instances")
  rec_memory=$(calc_executor_memory "$total_memory_mb" "$rec_instances" "$rec_driver")

  # 生成配置内容
  local config_content
  config_content=$(generate_spark_config "$rec_driver" "$rec_instances" "$rec_cores" "$rec_memory")

  # 当前配置对比（只读）
  local current_driver current_instances current_cores current_memory
  current_driver=$(read_config_value "$primary_container" "$CONFIG_PATH" "spark.driver.memory")
  current_instances=$(read_config_value "$primary_container" "$CONFIG_PATH" "spark.executor.instances")
  current_cores=$(read_config_value "$primary_container" "$CONFIG_PATH" "spark.executor.cores")
  current_memory=$(read_config_value "$primary_container" "$CONFIG_PATH" "spark.executor.memory")

  # 输出 JSON
  echo "{"
  echo "  \"skill\": \"apply_spark_config\","
  echo "  \"environment\": {"
  echo "    \"target\": \"${primary_container}\","
  echo "    \"deploy_mode\": \"${DEPLOY_MODE}\","
  echo "    \"spark_mode\": \"${spark_mode}\","
  echo "    \"env_type\": \"${env_type}\","
  echo "    \"total_vcores\": ${total_vcores},"
  echo "    \"total_memory_mb\": ${total_memory_mb},"
  if [[ -n "$worker_containers" ]]; then
    echo "    \"workers\": \"${worker_containers}\","
  fi
  echo "    \"spark_home\": \"${SPARK_HOME}\","
  echo "    \"config_path\": \"${CONFIG_PATH}\""
  echo "  },"
  echo "  \"recommended_params\": {"
  echo "    \"spark.driver.memory\": \"${rec_driver}\","
  echo "    \"spark.executor.instances\": \"${rec_instances}\","
  echo "    \"spark.executor.cores\": \"${rec_cores}\","
  echo "    \"spark.executor.memory\": \"${rec_memory}\""
  echo "  },"
  echo "  \"current_params\": {"
  echo "    \"spark.driver.memory\": \"${current_driver}\","
  echo "    \"spark.executor.instances\": \"${current_instances}\","
  echo "    \"spark.executor.cores\": \"${current_cores}\","
  echo "    \"spark.executor.memory\": \"${current_memory}\""
  echo "  },"
  echo "  \"config_content\": \"$(json_escape "$config_content")\","
  echo "  \"actions\": ["

  local restart_needed="true"
  if [[ "$spark_mode" == "yarn" ]]; then restart_needed="false"; fi

  # 主容器配置动作
  local backup_cmd write_cmd rollback_cmd
  local config_escaped
  config_escaped=$(json_escape "$config_content")
  if [[ "$DEPLOY_MODE" == "docker" ]]; then
    backup_cmd="docker exec ${primary_container} bash -c 'cp ${CONFIG_PATH} ${CONFIG_PATH}.bak.$(date +%Y%m%d_%H%M%S) 2>/dev/null || true'"
    write_cmd="printf '%s\\n' '${config_escaped}' | docker exec -i ${primary_container} bash -c 'cat > ${CONFIG_PATH}'"
    rollback_cmd="docker exec ${primary_container} bash -c 'cp ${CONFIG_PATH}.bak.* ${CONFIG_PATH} 2>/dev/null || true'"
  else
    backup_cmd="ssh ${primary_container} 'cp ${CONFIG_PATH} ${CONFIG_PATH}.bak 2>/dev/null || true'"
    write_cmd="printf '%s\\n' '${config_escaped}' | ssh ${primary_container} 'cat > ${CONFIG_PATH}'"
    rollback_cmd="ssh ${primary_container} 'cp ${CONFIG_PATH}.bak ${CONFIG_PATH} 2>/dev/null || true'"
  fi

  echo "    {"
  echo "      \"action_id\": \"spark-config-${primary_container}\","
  echo "      \"action_type\": \"config_write\","
  echo "      \"description\": \"写入 Spark 推荐配置到 ${primary_container}:${CONFIG_PATH}\","
  echo "      \"commands_execute\": ["
  echo "        \"$(json_escape "$backup_cmd")\","
  echo "        \"$(json_escape "$write_cmd")\""
  echo "      ],"
  echo "      \"restart_required\": ${restart_needed},"
  echo "      \"rollback\": ["
  echo "        \"$(json_escape "$rollback_cmd")\""
  echo "      ],"
  echo "      \"risk\": \"medium\","
  echo "      \"validation\": \"docker exec ${primary_container} cat ${CONFIG_PATH} | head -5\""
  echo "    }"
  echo "  ]"
  echo "}"
}

main
