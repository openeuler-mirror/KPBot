#!/usr/bin/env bash
#
# apply_spark_config.sh - 将推荐配置应用到目标 Spark 环境 (容器 + 物理机单机)
#
# 用法:
#   --target           目标容器名或 SSH 主机
#   --apply-all        自动检测 Spark 容器并批量应用配置 (仅容器模式)
#   --spark-home       Spark 安装路径（默认 /usr/local/spark）
#   --config-file      配置文件名（默认 spark-defaults.conf）
#   --deploy-mode      部署方式: docker（默认）或 ssh（物理机单机）
#   --spark-mode       Spark 模式: yarn/standalone/auto（默认 auto）
#   --detect-only      仅检测环境，不应用配置
#   --dry-run          仅输出命令，不执行
#   --restart          应用后重启 Spark
#   --driver-memory    手动指定 driver 内存（可选）
#   --executor-instances 手动指定 executor 数量（可选）
#   --executor-cores   手动指定 executor 核数（可选）
#   --executor-memory  手动指定 executor 内存（可选）
#   --no-compare       跳过当前配置 vs 推荐配置对比
#   --compare-only     仅对比不应用
#
# 示例:
#   ./apply_spark_config.sh --apply-all --dry-run
#   ./apply_spark_config.sh --apply-all --restart
#   ./apply_spark_config.sh --deploy-mode ssh --target 192.168.1.10 --dry-run

set -euo pipefail

# 默认值
TARGET=""
SPARK_HOME="/usr/local/spark"
CONFIG_FILE="spark-defaults.conf"
DEPLOY_MODE="docker"
SPARK_MODE="auto"
DETECT_ONLY=false
DRY_RUN=false
RESTART=false
APPLY_ALL=false
MANUAL_DRIVER_MEMORY="auto"
MANUAL_EXECUTOR_INSTANCES="auto"
MANUAL_EXECUTOR_CORES="auto"
MANUAL_EXECUTOR_MEMORY="auto"
COMPARE=true
COMPARE_ONLY=false

# 检测结果
DETECTED_ENV_TYPE=""
DETECTED_TOTAL_VCORES=""
DETECTED_TOTAL_MEMORY_MB=""

# 解析参数
while [[ $# -gt 0 ]]; do
  case $1 in
    --target)
      TARGET="$2"
      shift 2
      ;;
    --spark-home)
      SPARK_HOME="$2"
      shift 2
      ;;
    --config-file)
      CONFIG_FILE="$2"
      shift 2
      ;;
    --deploy-mode)
      DEPLOY_MODE="$2"
      shift 2
      ;;
    --spark-mode)
      SPARK_MODE="$2"
      shift 2
      ;;
    --detect-only)
      DETECT_ONLY=true
      shift
      ;;
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --restart)
      RESTART=true
      shift
      ;;
    --apply-all)
      APPLY_ALL=true
      shift
      ;;
    --driver-memory)
      MANUAL_DRIVER_MEMORY="$2"
      shift 2
      ;;
    --executor-instances)
      MANUAL_EXECUTOR_INSTANCES="$2"
      shift 2
      ;;
    --executor-cores)
      MANUAL_EXECUTOR_CORES="$2"
      shift 2
      ;;
    --executor-memory)
      MANUAL_EXECUTOR_MEMORY="$2"
      shift 2
      ;;
    --no-compare)
      COMPARE=false
      shift
      ;;
    --compare-only)
      COMPARE_ONLY=true
      DRY_RUN=true
      shift
      ;;
    -h|--help)
      echo "Usage: $0 --target <container|host> [options]"
      echo "       $0 --apply-all [options]  (仅容器模式)"
      echo ""
      echo "Options:"
      echo "  --target <name>              目标容器名或 SSH 主机地址"
      echo "  --apply-all                  自动检测 Spark 容器并批量应用配置"
      echo "  --spark-home <path>          Spark 安装路径（默认 /usr/local/spark）"
      echo "  --config-file <name>         配置文件名（默认 spark-defaults.conf）"
      echo "  --deploy-mode <mode>         部署方式: docker（默认）或 ssh（物理机单机）"
      echo "  --spark-mode <mode>          Spark 模式: yarn/standalone/auto（默认 auto）"
      echo "  --detect-only                仅检测环境，不应用配置"
      echo "  --dry-run                    仅输出命令，不执行"
      echo "  --restart                    应用后重启 Spark"
      echo "  --driver-memory <size>       手动指定 driver 内存（覆盖自动计算）"
      echo "  --executor-instances <n>     手动指定 executor 数量（覆盖自动计算）"
      echo "  --executor-cores <n>         手动指定 executor 核数（覆盖自动计算）"
      echo "  --executor-memory <size>     手动指定 executor 内存（覆盖自动计算）"
      echo "  --no-compare                 跳过当前配置 vs 推荐配置对比"
      echo "  --compare-only               仅对比当前配置与推荐配置，不应用"
      echo ""
      echo "Examples:"
      echo "  $0 --apply-all --dry-run                   # 预览推荐配置"
      echo "  $0 --apply-all --restart                    # 应用配置并重启"
      echo "  $0 --target spark-master --detect-only      # 仅检测单个容器"
      echo "  $0 --deploy-mode ssh --target 192.168.1.10 --dry-run  # SSH 物理机单机预览"
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      exit 1
      ;;
  esac
done

if [[ -z "$TARGET" && "$APPLY_ALL" != true ]]; then
  echo "Error: --target is required (or use --apply-all for auto-detection)"
  echo "Usage: $0 --target <container|host> [options]"
  echo "       $0 --apply-all [options]"
  exit 1
fi

CONFIG_PATH="${SPARK_HOME}/conf/${CONFIG_FILE}"

# ===========================================
# 环境检测函数
# ===========================================

# 检测是否为容器环境
detect_container() {
  local target="$1"
  local deploy_mode="${2:-$DEPLOY_MODE}"

  if [[ "$deploy_mode" == "docker" ]]; then
    if docker inspect "$target" &>/dev/null; then
      return 0
    fi
    return 1
  fi

  # SSH 模式: 检查远程目标的 /proc/1/cgroup
  if ssh -o ConnectTimeout=5 "$target" 'cat /proc/1/cgroup 2>/dev/null' 2>/dev/null | grep -qE "(docker|containerd)"; then
    return 0
  fi

  return 1
}

# 获取目标 CPU 核数
get_target_cpus() {
  local target="$1"

  if [[ "$DEPLOY_MODE" == "docker" ]]; then
    local nano_cpus=$(docker inspect "$target" --format '{{.HostConfig.NanoCpus}}' 2>/dev/null || echo "0")
    if [[ -n "$nano_cpus" && "$nano_cpus" != "0" && "$nano_cpus" != "<no value>" ]]; then
      echo $((nano_cpus / 1000000000))
      return
    fi
    docker exec "$target" nproc 2>/dev/null || echo "0"
  else
    ssh "$target" nproc 2>/dev/null || echo "0"
  fi
}

# 获取目标内存（MB）
get_target_memory_mb() {
  local target="$1"

  if [[ "$DEPLOY_MODE" == "docker" ]]; then
    local memory_bytes=$(docker inspect "$target" --format '{{.HostConfig.Memory}}' 2>/dev/null || echo "0")
    if [[ "$memory_bytes" != "0" && -n "$memory_bytes" ]]; then
      echo $((memory_bytes / 1024 / 1024))
    else
      echo "0"
    fi
  else
    local memory_kb=$(ssh "$target" 'cat /proc/meminfo | grep MemTotal' 2>/dev/null | awk '{print $2}' || echo "0")
    echo $((memory_kb / 1024))
  fi
}

# ===========================================
# 统一分派辅助函数 (docker/ssh 双模式)
# ===========================================

exec_on_target() {
  local target="$1"; shift
  if [[ "$DEPLOY_MODE" == "docker" ]]; then
    docker exec "$target" bash -c "$*"
  else
    ssh -o ConnectTimeout=5 "$target" "$*"
  fi
}

exec_on_target_detached() {
  local target="$1"; shift
  if [[ "$DEPLOY_MODE" == "docker" ]]; then
    docker exec -d "$target" bash -c "$*"
  else
    ssh "$target" "nohup bash -c '$*' > /dev/null 2>&1 &"
  fi
}

# 从目标读取配置值 (Spark properties 格式: key<空格>value)
read_config_value() {
  local target="$1"
  local config_path="$2"
  local key="$3"
  local val
  if [[ "$DEPLOY_MODE" == "docker" ]]; then
    val=$(docker exec "$target" bash -c "grep -E '^[[:space:]]*${key}[[:space:]]' ${config_path} 2>/dev/null | grep -v '^[[:space:]]*#' | head -1" 2>/dev/null || echo "")
  else
    val=$(ssh -o ConnectTimeout=5 "$target" "grep -E '^[[:space:]]*${key}[[:space:]]' ${config_path} 2>/dev/null | grep -v '^[[:space:]]*#' | head -1" 2>/dev/null || echo "")
  fi
  if [[ -z "$val" ]]; then
    echo "缺失"
  else
    echo "$val" | sed -E "s/^[[:space:]]*${key}[[:space:]]+//"
  fi
}

# 获取目标 IP 地址
get_target_ip() {
  local target="$1"
  if [[ "$DEPLOY_MODE" == "docker" ]]; then
    docker inspect "$target" --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' 2>/dev/null || echo "127.0.0.1"
  else
    echo "${target##*@}"
  fi
}

# ===========================================
# 目标发现函数 (仅容器模式)
# ===========================================

# 发现所有 Spark 容器
get_spark_containers() {
  docker ps --format '{{.Names}}' 2>/dev/null | grep -iE 'spark' | grep -vi 'velox' || echo ""
}

# 识别 Spark 主容器（master / driver / submit node）
get_spark_primary() {
  if [[ "$DEPLOY_MODE" == "docker" ]]; then
    local master
    master=$(docker ps --format '{{.Names}}' 2>/dev/null | grep -iE 'spark.*master|spark.*driver' | grep -vi 'velox' | head -1)
    if [[ -n "$master" ]]; then
      echo "$master"
      return
    fi
    for c in $(docker ps --format '{{.Names}}' 2>/dev/null | grep -iE 'spark' | grep -vi 'velox'); do
      local ports=$(docker inspect "$c" --format '{{.NetworkSettings.Ports}}' 2>/dev/null || echo "")
      if echo "$ports" | grep -qE "7077|8080"; then
        echo "$c"
        return
      fi
    done
    get_spark_containers | head -1
  else
    # SSH 物理机单机: TARGET 就是 Spark 所在机器
    echo "$TARGET"
  fi
}

# 获取 Worker 容器列表 (仅容器模式有意义)
# SSH 单机物理机: worker 在同一台机器上，不返回远程 worker
get_spark_workers() {
  if [[ "$DEPLOY_MODE" == "docker" ]]; then
    docker ps --format '{{.Names}}' 2>/dev/null | grep -iE 'spark.*worker|spark.*slave' | grep -vi 'velox' || echo ""
  else
    # SSH 物理机单机: worker 和 master 在同一台机器，无需远程管理
    echo ""
  fi
}

# 检测 Spark 模式 (standalone / yarn)
detect_spark_mode() {
  if [[ "$SPARK_MODE" != "auto" ]]; then
    echo "$SPARK_MODE"
    return
  fi

  # 检查是否有 worker 容器 → standalone
  local workers
  workers=$(get_spark_workers)
  if [[ -n "$workers" ]]; then
    echo "standalone"
    return
  fi

  # 检查是否有 yarn 相关
  if [[ "$DEPLOY_MODE" == "docker" ]]; then
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -qiE 'yarn|nodemanager'; then
      echo "yarn"
      return
    fi
  else
    if [[ -n "$TARGET" ]]; then
      if ssh -o ConnectTimeout=5 "$TARGET" 'ps aux | grep -q [N]odeManager' 2>/dev/null; then
        echo "yarn"
        return
      fi
    fi
  fi

  echo "standalone"
}

# ===========================================
# 参数计算函数
# ===========================================

# 计算 spark.driver.memory
calc_driver_memory() {
  if [[ "$MANUAL_DRIVER_MEMORY" != "auto" ]]; then
    echo "$MANUAL_DRIVER_MEMORY"
    return
  fi
  echo "8g"
}

# 计算 spark.executor.instances
calc_executor_instances() {
  local total_vcores="$1"
  local env_type="$2"

  if [[ "$MANUAL_EXECUTOR_INSTANCES" != "auto" ]]; then
    echo "$MANUAL_EXECUTOR_INSTANCES"
    return
  fi

  if [[ "$env_type" == "physical" ]]; then
    echo "24"
  elif [[ "$total_vcores" -ge 64 ]]; then
    echo "12"
  else
    local instances=$((total_vcores / 4))
    [[ "$instances" -lt 2 ]] && instances=2
    echo "$instances"
  fi
}

# 计算 spark.executor.cores
calc_executor_cores() {
  local total_vcores="$1"
  local instances="$2"

  if [[ "$MANUAL_EXECUTOR_CORES" != "auto" ]]; then
    echo "$MANUAL_EXECUTOR_CORES"
    return
  fi

  local cores=$((total_vcores / instances))
  [[ "$cores" -lt 1 ]] && cores=1
  echo "$cores"
}

# 计算 spark.executor.memory
calc_executor_memory() {
  local total_memory_mb="$1"
  local instances="$2"
  local driver_mem_str="${3:-8g}"

  if [[ "$MANUAL_EXECUTOR_MEMORY" != "auto" ]]; then
    echo "$MANUAL_EXECUTOR_MEMORY"
    return
  fi

  local driver_mb
  if [[ "$driver_mem_str" =~ ^([0-9]+)g$ ]]; then
    driver_mb=$((${BASH_REMATCH[1]} * 1024))
  elif [[ "$driver_mem_str" =~ ^([0-9]+)m$ ]]; then
    driver_mb=${BASH_REMATCH[1]}
  else
    driver_mb=8192
  fi

  local available_mb=$((total_memory_mb * 95 / 100 - driver_mb))
  local exec_mem=$((available_mb / instances))
  [[ "$exec_mem" -lt 1024 ]] && exec_mem=1024
  echo "${exec_mem}m"
}

# ===========================================
# 配置生成函数
# ===========================================

generate_spark_config() {
  local driver_memory="$1"
  local executor_instances="$2"
  local executor_cores="$3"
  local executor_memory="$4"

  cat << EOF
# Spark 推荐配置 - $(date +%Y-%m-%d_%H:%M:%S)
# 自动生成 by apply_spark_config.sh

# Driver 内存（调度、元数据管理、结果收集）
spark.driver.memory                     ${driver_memory}

# Executor 设置
spark.executor.instances                ${executor_instances}
spark.executor.cores                    ${executor_cores}
spark.executor.memory                   ${executor_memory}

# SQL 优化
spark.sql.autoBroadcastJoinThreshold                                  100m
spark.sql.shuffle.partitions                                           600
spark.sql.optimizer.runtime.bloomFilter.applicationSideScanSizeThreshold  0
spark.sql.sources.parallelPartitionDiscovery.parallelism                60

# JVM 参数（G1GC，减少 GC 时间，JDK<15 开启 BiasedLocking）
spark.executor.extraJavaOptions        -XX:+UseG1GC -XX:ParallelGCThread=4 -XX:MetaspaceSize=256m -XX:+UseBiasedLocking
EOF
}

# ===========================================
# 配置应用函数
# ===========================================

get_current_spark_value() {
  local container="$1"
  local key="$2"
  read_config_value "$container" "$CONFIG_PATH" "$key"
}

show_spark_comparison() {
  local container="$1"
  local driver_memory="$2"
  local executor_instances="$3"
  local executor_cores="$4"
  local executor_memory="$5"

  echo ""
  echo "### ${container}"
  echo ""

  printf "  %-60s | %-12s | %-22s | %-12s
" "参数" "当前值" "推荐值" "是否一致"
  printf "  %-60s-|-%-12s-|-%-22s-|-%-12s
" "------------------------------------------------------------" "------------" "----------------------" "------------"

  local cur_val rec_val match

  cur_val=$(get_current_spark_value "$container" "spark.driver.memory")
  rec_val="$driver_memory"
  if [[ "$cur_val" == "$rec_val" ]]; then match="✓ 一致"; else match="✗ 差异"; fi
  printf "  %-60s | %-12s | %-22s | %-12s
" "spark.driver.memory" "$cur_val" "$rec_val" "$match"

  cur_val=$(get_current_spark_value "$container" "spark.executor.instances")
  rec_val="$executor_instances"
  if [[ "$cur_val" == "$rec_val" ]]; then match="✓ 一致"; else match="✗ 差异"; fi
  printf "  %-60s | %-12s | %-22s | %-12s
" "spark.executor.instances" "$cur_val" "$rec_val" "$match"

  cur_val=$(get_current_spark_value "$container" "spark.executor.cores")
  rec_val="$executor_cores"
  if [[ "$cur_val" == "$rec_val" ]]; then match="✓ 一致"; else match="✗ 差异"; fi
  printf "  %-60s | %-12s | %-22s | %-12s
" "spark.executor.cores" "$cur_val" "$rec_val" "$match"

  cur_val=$(get_current_spark_value "$container" "spark.executor.memory")
  rec_val="$executor_memory"
  if [[ "$cur_val" == "$rec_val" ]]; then match="✓ 一致"; else match="✗ 差异"; fi
  printf "  %-60s | %-12s | %-22s | %-12s
" "spark.executor.memory" "$cur_val" "$rec_val" "$match"

  cur_val=$(get_current_spark_value "$container" "spark.sql.autoBroadcastJoinThreshold")
  rec_val="100m"
  if [[ "$cur_val" == "$rec_val" ]]; then match="✓ 一致"; else match="✗ 差异"; fi
  printf "  %-60s | %-12s | %-22s | %-12s
" "spark.sql.autoBroadcastJoinThreshold" "$cur_val" "$rec_val" "$match"

  cur_val=$(get_current_spark_value "$container" "spark.sql.shuffle.partitions")
  rec_val="600"
  if [[ "$cur_val" == "$rec_val" ]]; then match="✓ 一致"; else match="✗ 差异"; fi
  printf "  %-60s | %-12s | %-22s | %-12s
" "spark.sql.shuffle.partitions" "$cur_val" "$rec_val" "$match"

  cur_val=$(get_current_spark_value "$container" "spark.sql.optimizer.runtime.bloomFilter.applicationSideScanSizeThreshold")
  rec_val="0"
  if [[ "$cur_val" == "$rec_val" ]]; then match="✓ 一致"; else match="✗ 差异"; fi
  printf "  %-60s | %-12s | %-22s | %-12s
" "spark.sql.optimizer.runtime.bloomFilter.applicationSideScanSizeThreshold" "$cur_val" "$rec_val" "$match"

  cur_val=$(get_current_spark_value "$container" "spark.sql.sources.parallelPartitionDiscovery.parallelism")
  rec_val="60"
  if [[ "$cur_val" == "$rec_val" ]]; then match="✓ 一致"; else match="✗ 差异"; fi
  printf "  %-60s | %-12s | %-22s | %-12s
" "spark.sql.sources.parallelPartitionDiscovery.parallelism" "$cur_val" "$rec_val" "$match"

  cur_val=$(get_current_spark_value "$container" "spark.executor.extraJavaOptions")
  rec_val="-XX:+UseG1GC -XX:ParallelGCThread=4 -XX:MetaspaceSize=256m -XX:+UseBiasedLocking"
  if [[ "$cur_val" == "$rec_val" ]]; then match="✓ 一致"; else match="✗ 差异"; fi
  printf "  %-60s | %-12s | %-22s | %-12s
" "spark.executor.extraJavaOptions" "$cur_val" "$rec_val" "$match"

  echo ""
}

# 写入配置到 Docker 容器
apply_config_docker() {
  local target="$1"
  local config_path="$2"
  local content="$3"

  echo "备份原配置..."
  docker exec "$target" bash -c "cp ${config_path} ${config_path}.bak.\$(date +%Y%m%d_%H%M%S) 2>/dev/null || true"

  echo "写入新配置..."
  printf '%s\n' "$content" | docker exec -i "$target" bash -c "cat > ${config_path}"
}

# 写入配置到 SSH 主机
apply_config_ssh() {
  local target="$1"
  local config_path="$2"
  local content="$3"

  echo "备份原配置..."
  ssh "$target" "cp ${config_path} ${config_path}.bak 2>/dev/null || true"

  echo "写入新配置..."
  printf '%s\n' "$content" | ssh "$target" "cat > ${config_path}"
}

# 重启 Spark (standalone)
restart_spark_standalone() {
  local master="$1"
  local workers="$2"

  echo ""
  echo "重启 Spark 集群 (Standalone)..."

  # 停止所有 worker
  for w in $workers; do
    echo "  停止 ${w} 中的 Worker..."
    exec_on_target "$w" 'kill $(ps aux | grep "org.apache.spark.deploy.worker.Worker" | grep -v grep | awk "{print \$2}") 2>/dev/null || true'
  done
  sleep 2

  # 停止 master
  echo "  停止 ${master} 中的 Master..."
  exec_on_target "$master" 'kill $(ps aux | grep "org.apache.spark.deploy.master.Master" | grep -v grep | awk "{print \$2}") 2>/dev/null || true'
  sleep 3

  # 启动 master
  echo "  启动 ${master} 中的 Master..."
  exec_on_target_detached "$master" "nohup ${SPARK_HOME}/sbin/start-master.sh > /dev/null 2>&1 &"
  sleep 5

  # 等待 master 就绪
  local master_ready=0
  for i in $(seq 1 15); do
    if exec_on_target "$master" 'curl -s http://localhost:8080/json/' 2>/dev/null | grep -q 'url'; then
      master_ready=1
      echo "  Spark Master 已就绪"
      break
    fi
    sleep 3
  done
  if [[ "$master_ready" == 0 ]]; then
    echo "  WARNING: Spark Master 启动超时，请手动检查"
  fi

  # 启动 worker
  local master_ip
  master_ip=$(get_target_ip "$master")
  for w in $workers; do
    echo "  启动 ${w} 中的 Worker..."
    exec_on_target_detached "$w" "nohup ${SPARK_HOME}/sbin/start-slave.sh spark://${master_ip}:7077 > /dev/null 2>&1 &"
    sleep 2
  done

  # 验证 worker 注册
  echo "  等待 Worker 注册..."
  sleep 10
  local worker_count
  local worker_json
  worker_json=$(exec_on_target "$master" 'curl -s http://localhost:8080/json/' 2>/dev/null || echo "")
  worker_count=$(echo "$worker_json" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('workers',[])))" 2>/dev/null || echo "$worker_json" | grep -o '"workers"' | wc -l || echo "0")
  local expected_workers
  expected_workers=$(echo "$workers" | wc -w)
  echo "  已注册 Worker: ${worker_count}/${expected_workers}"
  if [[ "$worker_count" != "$expected_workers" ]]; then
    echo "  WARNING: Worker 注册数量不符，预期 ${expected_workers}，实际 ${worker_count}"
  fi

  echo "  Spark 集群重启完成"
}

# ===========================================
# --apply-all 模式主流程 (仅容器模式)
# ===========================================
run_apply_all() {
  echo "=========================================="
  echo "Spark 集群批量配置 (--apply-all)"
  echo "=========================================="

  if [[ "$DEPLOY_MODE" == "ssh" ]]; then
    echo "Error: --apply-all 仅支持容器模式 (docker)"
    echo "  物理机单机请使用: $0 --deploy-mode ssh --target <host>"
    exit 1
  fi

  # 1. 发现容器
  local spark_mode
  spark_mode=$(detect_spark_mode)

  local primary_container
  primary_container=$(get_spark_primary)
  if [[ -z "$primary_container" ]]; then
    echo "Error: 未找到 Spark 主容器"
    echo "  确保 Spark 容器正在运行 (docker ps)"
    exit 1
  fi

  echo "Spark 模式: ${spark_mode}"
  echo "主容器: ${primary_container}"

  local worker_containers=""
  if [[ "$spark_mode" == "standalone" ]]; then
    worker_containers=$(get_spark_workers)
    echo "Worker 容器: ${worker_containers:-无}"
  fi
  echo ""

  # 2. 环境检测
  echo "[1/5] 环境检测..."

  DETECTED_ENV_TYPE="container"
  if ! detect_container "$primary_container"; then
    DETECTED_ENV_TYPE="physical"
  fi

  local primary_cpus primary_mem
  primary_cpus=$(get_target_cpus "$primary_container")
  primary_mem=$(get_target_memory_mb "$primary_container")
  echo "  ${primary_container}: CPU=${primary_cpus}, 内存=${primary_mem}MB"

  # 计算总资源：primary + 所有 worker
  local total_vcores=$primary_cpus
  local total_memory_mb=$primary_mem

  if [[ -n "$worker_containers" ]]; then
    for w in $worker_containers; do
      local w_cpus w_mem
      w_cpus=$(get_target_cpus "$w")
      w_mem=$(get_target_memory_mb "$w")
      total_vcores=$((total_vcores + w_cpus))
      total_memory_mb=$((total_memory_mb + w_mem))
      echo "  ${w}: CPU=${w_cpus}, 内存=${w_mem}MB"
    done
  fi

  echo "  集群总资源: vcores=${total_vcores}, 内存=${total_memory_mb}MB"
  echo ""

  # 3. 计算推荐参数
  echo "[2/5] 计算推荐参数..."

  local REC_DRIVER_MEMORY
  REC_DRIVER_MEMORY=$(calc_driver_memory)

  local REC_EXECUTOR_INSTANCES
  REC_EXECUTOR_INSTANCES=$(calc_executor_instances "$total_vcores" "$DETECTED_ENV_TYPE")

  local REC_EXECUTOR_CORES
  REC_EXECUTOR_CORES=$(calc_executor_cores "$total_vcores" "$REC_EXECUTOR_INSTANCES")

  local REC_EXECUTOR_MEMORY
  REC_EXECUTOR_MEMORY=$(calc_executor_memory "$total_memory_mb" "$REC_EXECUTOR_INSTANCES" "$REC_DRIVER_MEMORY")

  echo "  spark.driver.memory                                  = ${REC_DRIVER_MEMORY}"
  echo "  spark.executor.instances                             = ${REC_EXECUTOR_INSTANCES}"
  echo "  spark.executor.cores   = ${total_vcores}/${REC_EXECUTOR_INSTANCES} = ${REC_EXECUTOR_CORES}"
  echo "  spark.executor.memory  = (${total_memory_mb}M * 0.95 - ${REC_DRIVER_MEMORY}) / ${REC_EXECUTOR_INSTANCES} = ${REC_EXECUTOR_MEMORY}"
  echo ""

  # 4. 当前配置 vs 推荐配置对比
  if [[ "$COMPARE" == true ]]; then
    echo "[3/5] 当前配置 vs 推荐配置对比..."

    show_spark_comparison "$primary_container" \
      "$REC_DRIVER_MEMORY" \
      "$REC_EXECUTOR_INSTANCES" \
      "$REC_EXECUTOR_CORES" \
      "$REC_EXECUTOR_MEMORY"

    if [[ "$COMPARE_ONLY" == true ]]; then
      echo "[compare-only 模式] 仅对比，不应用配置。"
      echo "=========================================="
      echo "对比完成"
      echo "=========================================="
      exit 0
    fi
  fi

  # 5. 应用配置
  echo "[4/5] 应用配置..."

  local config_content
  config_content=$(generate_spark_config \
    "$REC_DRIVER_MEMORY" \
    "$REC_EXECUTOR_INSTANCES" \
    "$REC_EXECUTOR_CORES" \
    "$REC_EXECUTOR_MEMORY")

  if [[ "$DRY_RUN" == true ]]; then
    echo ""
    echo "--- [${primary_container}] ---"
    echo "[DRY-RUN] 将写入 ${CONFIG_PATH}:"
    echo "$config_content"
  else
    apply_config_docker "$primary_container" "$CONFIG_PATH" "$config_content"
    echo "  配置已写入 ${primary_container}:${CONFIG_PATH}"
  fi

  echo ""
  echo "[5/5] 配置应用完成"

  # 6. 重启
  if [[ "$RESTART" == true && "$DRY_RUN" != true ]]; then
    if [[ "$spark_mode" == "standalone" ]]; then
      restart_spark_standalone "$primary_container" "$worker_containers"
    else
      echo ""
      echo "提示: YARN 模式无需重启 Spark 集群，新提交的作业将自动使用新配置"
    fi
  elif [[ "$DRY_RUN" != true ]]; then
    echo ""
    echo "提示: 配置已应用，使用 --restart 重启集群使配置生效（YARN 模式无需重启）"
  fi

  echo ""
  echo "=========================================="
  echo "批量配置完成"
  echo "=========================================="
}

# ===========================================
# 单目标模式主流程 (容器 + 物理机单机)
# ===========================================
run_single_target() {
  echo "=========================================="
  echo "Spark 配置应用工具"
  echo "=========================================="
  echo "目标: ${TARGET}"
  echo "Spark Home: ${SPARK_HOME}"
  echo "配置文件: ${CONFIG_PATH}"
  echo "部署模式: ${DEPLOY_MODE}"
  echo "=========================================="
  echo ""

  # 检测环境
  echo "[1/3] 检测环境..."
  if detect_container "$TARGET" "$DEPLOY_MODE"; then
    DETECTED_ENV_TYPE="container"
    echo "  环境类型: 容器"
  else
    DETECTED_ENV_TYPE="physical"
    echo "  环境类型: 物理机"
  fi

  local cpus mem
  cpus=$(get_target_cpus "$TARGET")
  mem=$(get_target_memory_mb "$TARGET")
  echo "  CPU: ${cpus}"
  echo "  内存: ${mem}MB"
  echo ""

  # 计算参数
  echo "[2/3] 计算推荐参数..."

  local REC_DRIVER_MEMORY
  REC_DRIVER_MEMORY=$(calc_driver_memory)

  local REC_EXECUTOR_INSTANCES
  REC_EXECUTOR_INSTANCES=$(calc_executor_instances "$cpus" "$DETECTED_ENV_TYPE")

  local REC_EXECUTOR_CORES
  REC_EXECUTOR_CORES=$(calc_executor_cores "$cpus" "$REC_EXECUTOR_INSTANCES")

  local REC_EXECUTOR_MEMORY
  REC_EXECUTOR_MEMORY=$(calc_executor_memory "$mem" "$REC_EXECUTOR_INSTANCES" "$REC_DRIVER_MEMORY")

  echo "  spark.driver.memory      = ${REC_DRIVER_MEMORY}"
  echo "  spark.executor.instances = ${REC_EXECUTOR_INSTANCES}"
  echo "  spark.executor.cores     = ${cpus}/${REC_EXECUTOR_INSTANCES} = ${REC_EXECUTOR_CORES}"
  echo "  spark.executor.memory    = (${mem}M * 0.95 - ${REC_DRIVER_MEMORY}) / ${REC_EXECUTOR_INSTANCES} = ${REC_EXECUTOR_MEMORY}"
  echo ""

  # 生成配置
  local config_content
  config_content=$(generate_spark_config \
    "$REC_DRIVER_MEMORY" \
    "$REC_EXECUTOR_INSTANCES" \
    "$REC_EXECUTOR_CORES" \
    "$REC_EXECUTOR_MEMORY")

  if [[ "$DRY_RUN" == true ]]; then
    echo "[3/3] [DRY-RUN] 将写入 ${CONFIG_PATH}:"
    echo "----------------------------------------"
    echo "$config_content"
    echo "----------------------------------------"
  else
    echo "[3/3] 应用配置..."
    if [[ "$DEPLOY_MODE" == "docker" ]]; then
      apply_config_docker "$TARGET" "$CONFIG_PATH" "$config_content"
    else
      apply_config_ssh "$TARGET" "$CONFIG_PATH" "$config_content"
    fi
    echo "配置已应用完成。"
    echo ""
    echo "提示: 使用 --restart 重启使配置生效（YARN 模式无需重启）"
  fi

  echo ""
  echo "=========================================="
  echo "配置应用完成"
  echo "=========================================="
}

# ===========================================
# 入口
# ===========================================
if [[ "$APPLY_ALL" == true ]]; then
  run_apply_all
else
  run_single_target
fi
