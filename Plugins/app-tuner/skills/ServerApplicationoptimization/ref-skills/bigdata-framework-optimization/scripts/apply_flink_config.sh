#!/usr/bin/env bash
#
# apply_flink_config.sh - 将推荐配置应用到目标 Flink 环境 (仅支持容器)
#
# 用法:
#   --target        目标容器名（如 flink_JM）
#   --flink-home    Flink 安装路径（默认 /usr/local/flink）
#   --config-file   配置文件名（默认 flink-conf.yaml）
#   --detect-only   仅检测环境，不应用配置
#   --dry-run       仅输出命令，不执行
#   --restart       应用后重启 Flink 进程
#   --parallelism   手动指定 parallelism.default（可选，自动检测）
#   --task-slots    手动指定 taskmanager.numberOfTaskSlots（可选，自动检测）
#   --object-reuse  启用 object-reuse: true/false/auto（默认 auto）
#   --mini-batch    启用 mini-batch: true/false/auto（默认 auto）
#
# 示例:
#   ./apply_flink_config.sh --apply-all --dry-run     # 预览所有容器的推荐配置
#   ./apply_flink_config.sh --apply-all --restart      # 应用配置并重启集群
#   ./apply_flink_config.sh --target flink_JM --detect-only  # 仅检测单个容器

set -euo pipefail

# 默认值
TARGET=""
FLINK_HOME="/usr/local/flink"
CONFIG_FILE="flink-conf.yaml"
DETECT_ONLY=false
DRY_RUN=false
RESTART=false
APPLY_ALL=false
PARALLELISM="auto"
TASK_SLOTS="auto"
OBJECT_REUSE="auto"
MINI_BATCH="auto"
STATE_BACKEND="auto"
ROLE="auto"
TM_PER_CONTAINER="auto"
COMPARE=true
COMPARE_ONLY=false

# 检测结果
DETECTED_ENV_TYPE=""
DETECTED_CPU_CORES=""
DETECTED_MEMORY_MB=""
DETECTED_CONTAINER_NUM=""

# 解析参数
while [[ $# -gt 0 ]]; do
  case $1 in
    --target)
      TARGET="$2"
      shift 2
      ;;
    --flink-home)
      FLINK_HOME="$2"
      shift 2
      ;;
    --config-file)
      CONFIG_FILE="$2"
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
    --parallelism)
      PARALLELISM="$2"
      shift 2
      ;;
    --task-slots)
      TASK_SLOTS="$2"
      shift 2
      ;;
    --object-reuse)
      OBJECT_REUSE="$2"
      shift 2
      ;;
    --mini-batch)
      MINI_BATCH="$2"
      shift 2
      ;;
    --state-backend)
      STATE_BACKEND="$2"
      shift 2
      ;;
    --role)
      ROLE="$2"
      shift 2
      ;;
    --tm-per-container)
      TM_PER_CONTAINER="$2"
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
      echo "Usage: $0 --target <container> [options]"
      echo "       $0 --apply-all [options]"
      echo ""
      echo "Options:"
      echo "  --target <name>           目标容器名"
      echo "  --apply-all               自动检测 JM+所有TM容器并批量应用配置"
      echo "  --flink-home <path>       Flink 安装路径（默认 /usr/local/flink）"
      echo "  --config-file <name>      配置文件名（默认 flink-conf.yaml）"
      echo "  --role <role>             容器角色: jobmanager/taskmanager/auto（默认 auto）"
      echo "  --tm-per-container <n>    每容器 TM 进程数（默认 auto，自动检测）"
      echo "  --no-compare              跳过当前配置 vs 推荐配置对比"
      echo "  --compare-only            仅对比当前配置与推荐配置，不应用"
      echo "  --detect-only             仅检测环境，不应用配置"
      echo "  --dry-run                 仅输出命令，不执行"
      echo "  --restart                 应用后重启 Flink 进程"
      echo "  --parallelism <n>         手动指定 parallelism（默认 auto）"
      echo "  --task-slots <n>          手动指定 task-slots（默认 auto）"
      echo "  --object-reuse <mode>     启用 object-reuse: true/false/auto（默认 auto）"
      echo "  --mini-batch <mode>       启用 mini-batch: true/false/auto（默认 auto）"
      echo "  --state-backend <type>    状态后端: memory/rocksdb（默认 auto=memory）"
      echo ""
      echo "Examples:"
      echo "  $0 --apply-all --dry-run              # 预览所有容器的推荐配置"
      echo "  $0 --apply-all --restart               # 应用配置并重启集群"
      echo "  $0 --target flink_JM --detect-only     # 仅检测单个容器"
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      exit 1
      ;;
  esac
done

# 检查必需参数 (--apply-all 模式下不需要 --target)
if [[ -z "$TARGET" && "$APPLY_ALL" != true ]]; then
  echo "Error: --target is required (or use --apply-all for auto-detection)"
  echo "Usage: $0 --target <container> [options]"
  echo "       $0 --apply-all [options]"
  exit 1
fi

CONFIG_PATH="${FLINK_HOME}/conf/${CONFIG_FILE}"

# ===========================================
# 环境检测函数
# ===========================================

# 检测是否为容器环境
detect_container() {
  local target="$1"
  if docker inspect "$target" &>/dev/null; then
    return 0
  fi
  return 1
}

# 获取容器 CPU 核数
get_container_cpus() {
  local target="$1"

  local nano_cpus=$(docker inspect "$target" --format '{{.HostConfig.NanoCpus}}' 2>/dev/null || echo "0")
  if [[ -n "$nano_cpus" && "$nano_cpus" != "0" && "$nano_cpus" != "<no value>" ]]; then
    echo $((nano_cpus / 1000000000))
    return
  fi

  docker exec "$target" nproc 2>/dev/null || echo "0"
}

# 获取容器内存（MB）
get_container_memory_mb() {
  local target="$1"

  local memory_bytes=$(docker inspect "$target" --format '{{.HostConfig.Memory}}' 2>/dev/null || echo "0")
  if [[ "$memory_bytes" != "0" && -n "$memory_bytes" ]]; then
    echo $((memory_bytes / 1024 / 1024))
  else
    echo "0"
  fi
}

# 获取所有 TM 容器名列表
get_tm_containers() {
  docker ps --format '{{.Names}}' 2>/dev/null | grep -iE 'flink.*TM|flink.*taskmanager' | grep -vi 'velox' || echo ""
}

# 获取容器内 TaskManagerRunner 进程数
get_tm_procs_in_container() {
  local target="$1"

  local count
  count=$(docker exec "$target" bash -c 'ps aux | grep -c [T]askManagerRunner' 2>/dev/null || echo "0")
  count=$(echo "$count" | tail -1 | tr -d '[:space:]')
  echo "${count:-0}"
}

# 获取 JM 容器名
get_jm_container() {
  docker ps --format '{{.Names}}' 2>/dev/null | grep -iE 'flink.*JM|flink.*jobmanager' | grep -vi 'velox' | head -1 || echo ""
}

# 获取 Flink TaskManager 容器数量
get_tm_count() {
  get_tm_containers | wc -l
}

# ===========================================
# 角色识别函数
# ===========================================

# 识别容器角色：jobmanager 或 taskmanager
detect_role() {
  local target="$1"

  if [[ "$ROLE" != "auto" ]]; then
    echo "$ROLE"
    return
  fi

  if echo "$target" | grep -qiE 'flink.*jm|flink.*jobmanager'; then
    echo "jobmanager"
    return
  fi
  if echo "$target" | grep -qiE 'flink.*tm|flink.*taskmanager'; then
    echo "taskmanager"
    return
  fi

  local ports=$(docker inspect "$target" --format '{{.NetworkSettings.Ports}}' 2>/dev/null || echo "")
  if echo "$ports" | grep -q "8081"; then
    echo "jobmanager"
    return
  fi

  echo "taskmanager"
}

# 获取 JobManager IP 地址
get_jm_address() {
  local jm_container=$(docker ps --format '{{.Names}} {{.Ports}}' 2>/dev/null | grep "8081" | awk '{print $1}' | head -1)
  if [[ -n "$jm_container" ]]; then
    docker inspect "$jm_container" --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' 2>/dev/null
    return
  fi
  echo "localhost"
}

# ===========================================
# 参数计算函数
# ===========================================

# 计算推荐的 parallelism.default
# 整机: cores/2; 8U小规格(≤8核): cores
calc_parallelism() {
  local cores="$1"

  if [[ "$PARALLELISM" != "auto" ]]; then
    echo "$PARALLELISM"
    return
  fi

  if [[ "$cores" -le 8 ]]; then
    echo "$cores"
  else
    echo $((cores / 2))
  fi
}

# 计算推荐的 taskmanager.numberOfTaskSlots
# 公式: parallelism.default / TM容器数 / 容器内TM进程数
calc_task_slots() {
  local parallelism="$1"
  local tm_container_count="$2"
  local tm_procs_per_container="$3"

  if [[ "$TASK_SLOTS" != "auto" ]]; then
    echo "$TASK_SLOTS"
    return
  fi

  if [[ "$tm_procs_per_container" -lt 1 ]]; then
    tm_procs_per_container=1
  fi
  local slots=$((parallelism / tm_container_count / tm_procs_per_container))
  [[ "$slots" -lt 1 ]] && slots=1
  echo "$slots"
}

# ===========================================
# 配置应用函数
# ===========================================

# 生成 YAML 配置内容
generate_yaml_config() {
  local role="$1"
  local parallelism="$2"
  local task_slots="$3"
  local tm_memory="$4"
  local jm_address="$5"
  local object_reuse="$6"
  local mini_batch="$7"

  if [[ "$role" == "jobmanager" ]]; then
    cat << EOF
# Flink 推荐配置 - $(date +%Y-%m-%d_%H:%M:%S)
# 角色: JobManager
# 自动生成 by apply_flink_config.sh

# JobManager 设置
jobmanager.rpc.address: ${jm_address}

# 并行度设置
parallelism.default: ${parallelism}

# 对象复用（内存状态后端推荐开，RocksDB状态后端建议关）
pipeline.object-reuse: ${object_reuse}

# Mini-batch 攒批（增加吞吐，劣化时延）
table.exec.mini-batch.enabled: ${mini_batch}
table.exec.mini-batch.allow-latency: 2s
table.exec.mini-batch.size: 50000
EOF
  else
    cat << EOF
# Flink 推荐配置 - $(date +%Y-%m-%d_%H:%M:%S)
# 角色: TaskManager
# 自动生成 by apply_flink_config.sh

# JobManager 连接地址
jobmanager.rpc.address: ${jm_address}

# TaskManager 设置（每 TM 进程）
taskmanager.numberOfTaskSlots: ${task_slots}
taskmanager.memory.process.size: ${tm_memory}

# 对象复用（内存状态后端推荐开，RocksDB状态后端建议关）
pipeline.object-reuse: ${object_reuse}

# Mini-batch 攒批（增加吞吐，劣化时延）
table.exec.mini-batch.enabled: ${mini_batch}
table.exec.mini-batch.allow-latency: 2s
table.exec.mini-batch.size: 50000
EOF
  fi
}

# 将 YAML 内容按 key 合并写入目标的 flink-conf.yaml
# 只更新脚本管理的 key，保留用户自定义的其他配置项
merge_config_to_container() {
  local target="$1"
  local config_path="$2"
  local yaml_content="$3"

  echo "备份原配置..."
  docker exec "$target" bash -c "cp ${config_path} ${config_path}.bak.\$(date +%Y%m%d_%H%M%S) 2>/dev/null || true"

  echo "合并写入配置..."
  while IFS=':' read -r key value; do
    [[ -z "$key" || "$key" =~ ^[[:space:]]*# ]] && continue
    key=$(echo "$key" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    value=$(echo "$value" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    [[ -z "$key" ]] && continue

    local escaped_key=$(echo "$key" | sed 's/\./\\\\./g')
    local escaped_value=$(echo "$value" | sed 's/[&/\]/\\&/g')

    if docker exec "$target" bash -c "grep -qE '^[[:space:]]*${escaped_key}:' ${config_path} 2>/dev/null"; then
      docker exec "$target" bash -c "sed -i 's|^[[:space:]]*${escaped_key}:.*|${escaped_key}: ${escaped_value}|' ${config_path}"
    else
      docker exec "$target" bash -c "echo '${key}: ${value}' >> ${config_path}"
    fi
  done <<< "$yaml_content"

  echo "  配置已合并到 ${target}:${config_path}"
}

# 应用配置到容器
apply_config_docker() {
  local target="$1"
  local config_path="$2"
  local yaml_content="$3"

  merge_config_to_container "$target" "$config_path" "$yaml_content"
}

# ===========================================
# 工具函数
# ===========================================

# 处理 object-reuse 值（按文档：内存状态后端开，RocksDB关）
resolve_object_reuse() {
  local obj_reuse="${1:-auto}"
  local state_backend="${2:-auto}"

  case "$obj_reuse" in
    true)   echo "true"; return ;;
    false)  echo "false"; return ;;
  esac

  case "$state_backend" in
    rocksdb|RocksDB|ROCKSDB) echo "false" ;;
    *)                      echo "true" ;;
  esac
}

# 处理 mini-batch 值
resolve_mini_batch() {
  case "${1:-auto}" in
    auto|true)  echo "true" ;;
    false)      echo "false" ;;
    *)          echo "true" ;;
  esac
}

# 获取容器当前 flink-conf.yaml 中某个参数的值
get_current_config_value() {
  local container="$1"
  local key="$2"
  local val
  val=$(docker exec "$container" bash -c "grep -E '^[[:space:]]*${key}[[:space:]:]' ${FLINK_HOME}/conf/${CONFIG_FILE} 2>/dev/null | grep -v '^[[:space:]]*#' | head -1" 2>/dev/null || echo "")
  if [[ -z "$val" ]]; then
    echo "缺失"
  else
    echo "$val" | sed -E "s/^[[:space:]]*${key}[[:space:]:]+//"
  fi
}

# 显示当前配置 vs 推荐配置对比表
show_comparison_table() {
  local container="$1"
  local role="$2"
  local parallelism="$3"
  local task_slots="$4"
  local tm_memory="$5"
  local obj_reuse="$6"
  local mini_batch="$7"

  echo ""
  echo "### ${container} (角色: ${role})"
  echo ""

  printf "  %-45s | %-12s | %-22s | %-12s | %s
" "参数" "当前值" "推荐值" "是否一致" "formula"
  printf "  %-45s-|-%-12s-|-%-22s-|-%-12s-|-%-7s
" "---------------------------------------------" "------------" "----------------------" "------------" "-------"

  local cur_val rec_val formula match

  if [[ "$role" == "jobmanager" ]]; then
    cur_val=$(get_current_config_value "$container" "parallelism.default")
    rec_val="$parallelism"
    formula="是"
    if [[ "$cur_val" == "$rec_val" ]]; then match="✓ 一致"; else match="✗ 差异"; fi
    printf "  %-45s | %-12s | %-22s | %-12s | %-7s
" "parallelism.default" "$cur_val" "$rec_val" "$match" "$formula"
  else
    cur_val=$(get_current_config_value "$container" "taskmanager.numberOfTaskSlots")
    rec_val="$task_slots"
    formula="是"
    if [[ "$cur_val" == "$rec_val" ]]; then match="✓ 一致"; else match="✗ 差异"; fi
    printf "  %-45s | %-12s | %-22s | %-12s | %-7s
" "taskmanager.numberOfTaskSlots" "$cur_val" "$rec_val" "$match" "$formula"

    cur_val=$(get_current_config_value "$container" "taskmanager.memory.process.size")
    rec_val="$tm_memory"
    formula="是"
    if [[ "$cur_val" == "$rec_val" ]]; then match="✓ 一致"; else match="✗ 差异"; fi
    printf "  %-45s | %-12s | %-22s | %-12s | %-7s
" "taskmanager.memory.process.size" "$cur_val" "$rec_val" "$match" "$formula"
  fi

  cur_val=$(get_current_config_value "$container" "pipeline.object-reuse")
  rec_val="$obj_reuse"
  formula="否"
  if [[ "$cur_val" == "$rec_val" ]]; then match="✓ 一致"; else match="✗ 差异"; fi
  printf "  %-45s | %-12s | %-22s | %-12s | %-7s
" "pipeline.object-reuse" "$cur_val" "$rec_val" "$match" "$formula"

  cur_val=$(get_current_config_value "$container" "table.exec.mini-batch.enabled")
  rec_val="$mini_batch"
  formula="否"
  if [[ "$cur_val" == "$rec_val" ]]; then match="✓ 一致"; else match="✗ 差异"; fi
  printf "  %-45s | %-12s | %-22s | %-12s | %-7s
" "table.exec.mini-batch.enabled" "$cur_val" "$rec_val" "$match" "$formula"

  cur_val=$(get_current_config_value "$container" "table.exec.mini-batch.allow-latency")
  rec_val="2s"
  formula="否"
  if [[ "$cur_val" == "$rec_val" ]]; then match="✓ 一致"; else match="✗ 差异"; fi
  printf "  %-45s | %-12s | %-22s | %-12s | %-7s
" "table.exec.mini-batch.allow-latency" "$cur_val" "$rec_val" "$match" "$formula"

  cur_val=$(get_current_config_value "$container" "table.exec.mini-batch.size")
  rec_val="50000"
  formula="否"
  if [[ "$cur_val" == "$rec_val" ]]; then match="✓ 一致"; else match="✗ 差异"; fi
  printf "  %-45s | %-12s | %-22s | %-12s | %-7s
" "table.exec.mini-batch.size" "$cur_val" "$rec_val" "$match" "$formula"

  echo ""
}

# 应用配置到一个容器
apply_to_container() {
  local container="$1"
  local role="$2"
  local parallelism="$3"
  local task_slots="$4"
  local tm_memory="$5"
  local jm_addr="$6"
  local obj_reuse="$7"
  local mini_batch="$8"

  echo ""
  echo "--- [${container}] (角色: ${role}) ---"

  local yaml_content
  yaml_content=$(generate_yaml_config "$role" "$parallelism" "$task_slots" "$tm_memory" "$jm_addr" "$obj_reuse" "$mini_batch")

  if [[ "$DRY_RUN" == true ]]; then
    echo "[DRY-RUN] 将写入 ${CONFIG_PATH}:"
    echo "$yaml_content"
    return
  fi

  apply_config_docker "$container" "$CONFIG_PATH" "$yaml_content"
  echo "  配置已写入 ${container}:${CONFIG_PATH}"
}

# ===========================================
# --apply-all 模式：自动检测并配置所有容器
# ===========================================
run_apply_all() {
  echo "=========================================="
  echo "Flink 集群批量配置 (--apply-all)"
  echo "=========================================="

  # 1. 发现所有容器
  local jm_container
  jm_container=$(get_jm_container)
  if [[ -z "$jm_container" ]]; then
    echo "Error: 未找到 JobManager 容器"
    echo "  确保 Flink 容器正在运行 (docker ps)"
    exit 1
  fi

  local tm_containers
  tm_containers=$(get_tm_containers)
  if [[ -z "$tm_containers" ]]; then
    echo "Error: 未找到 TaskManager 容器"
    exit 1
  fi

  local tm_container_count
  tm_container_count=$(echo "$tm_containers" | wc -l)

  echo "JM 容器: ${jm_container}"
  echo "TM 容器 (${tm_container_count} 个): ${tm_containers}"
  echo ""

  # 2. 采集各容器资源
  local jm_cpus jm_mem_mb
  jm_cpus=$(get_container_cpus "$jm_container")
  jm_mem_mb=$(get_container_memory_mb "$jm_container")

  echo "[1/5] 环境检测..."
  echo "  ${jm_container}: CPU=${jm_cpus}, 内存=${jm_mem_mb}MB"

  local total_tm_cpus=0
  local tm_cpus_list=()
  local tm_mem_list=()
  local tm_procs_list=()

  for tm in $tm_containers; do
    local cpus mem procs
    cpus=$(get_container_cpus "$tm")
    mem=$(get_container_memory_mb "$tm")
    if [[ "$TM_PER_CONTAINER" != "auto" ]]; then
      procs="$TM_PER_CONTAINER"
    else
      procs=$(get_tm_procs_in_container "$tm")
      if [[ "$procs" -le 0 ]]; then
        procs=1
      fi
    fi
    total_tm_cpus=$((total_tm_cpus + cpus))
    tm_cpus_list+=("$cpus")
    tm_mem_list+=("$mem")
    tm_procs_list+=("$procs")
    echo "  ${tm}: CPU=${cpus}, 内存=${mem}MB, TM进程数=${procs}"
  done

  local jm_addr
  jm_addr=$(docker inspect "$jm_container" --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' 2>/dev/null || echo "127.0.0.1")
  echo "  JobManager IP: ${jm_addr}"
  echo ""

  # 3. 计算参数
  echo "[2/5] 计算推荐参数..."

  local OBJ_REUSE
  OBJ_REUSE=$(resolve_object_reuse "$OBJECT_REUSE" "$STATE_BACKEND")
  local MINI_BATCH_ENABLED
  MINI_BATCH_ENABLED=$(resolve_mini_batch "$MINI_BATCH")

  local RECOMMENDED_PARALLELISM
  if [[ "$PARALLELISM" != "auto" ]]; then
    RECOMMENDED_PARALLELISM="$PARALLELISM"
  elif [[ "$total_tm_cpus" -le 8 ]]; then
    RECOMMENDED_PARALLELISM="$total_tm_cpus"
    echo "  parallelism.default = ${total_tm_cpus} (8U小规格, 不除以2)"
  else
    RECOMMENDED_PARALLELISM=$((total_tm_cpus / 2))
    echo "  parallelism.default = ${total_tm_cpus} / 2 = ${RECOMMENDED_PARALLELISM}"
  fi
  [[ "$RECOMMENDED_PARALLELISM" -lt 1 ]] && RECOMMENDED_PARALLELISM=1

  echo ""

  # 4. 计算每个 TM 容器的配置
  echo "[3/5] 逐容器计算 TM 参数..."

  local tm_configs=()
  local idx=0
  for tm in $tm_containers; do
    local cpus="${tm_cpus_list[$idx]}"
    local mem="${tm_mem_list[$idx]}"
    local procs="${tm_procs_list[$idx]}"

    local slots
    if [[ "$TASK_SLOTS" != "auto" ]]; then
      slots="$TASK_SLOTS"
    else
      slots=$((RECOMMENDED_PARALLELISM / tm_container_count / procs))
      [[ "$slots" -lt 1 ]] && slots=1
    fi

    local tm_mem_mb
    tm_mem_mb=$((mem / procs))

    echo "  ${tm}: slots=${slots} (=${RECOMMENDED_PARALLELISM}/${tm_container_count}/${procs}), memory=${tm_mem_mb}m (=${mem}M/${procs})"

    tm_configs+=("${tm}|${slots}|${tm_mem_mb}m")
    idx=$((idx + 1))
  done
  echo ""

  # 5. 当前配置 vs 推荐配置对比
  if [[ "$COMPARE" == true ]]; then
    echo "[4/6] 当前配置 vs 推荐配置对比..."

    show_comparison_table "$jm_container" "jobmanager" \
      "$RECOMMENDED_PARALLELISM" "" "" \
      "$OBJ_REUSE" "$MINI_BATCH_ENABLED"

    for cfg in "${tm_configs[@]}"; do
      local container slots memory
      container=$(echo "$cfg" | cut -d'|' -f1)
      slots=$(echo "$cfg" | cut -d'|' -f2)
      memory=$(echo "$cfg" | cut -d'|' -f3)

      show_comparison_table "$container" "taskmanager" \
        "$RECOMMENDED_PARALLELISM" "$slots" "$memory" \
        "$OBJ_REUSE" "$MINI_BATCH_ENABLED"
    done

    if [[ "$COMPARE_ONLY" == true ]]; then
      echo "[compare-only 模式] 仅对比，不应用配置。"
      echo "=========================================="
      echo "对比完成"
      echo "=========================================="
      exit 0
    fi
  fi

  # 6. 应用配置
  echo "[5/6] 应用配置..."

  apply_to_container "$jm_container" "jobmanager" \
    "$RECOMMENDED_PARALLELISM" "" "" "$jm_addr" \
    "$OBJ_REUSE" "$MINI_BATCH_ENABLED"

  for cfg in "${tm_configs[@]}"; do
    local container slots memory
    container=$(echo "$cfg" | cut -d'|' -f1)
    slots=$(echo "$cfg" | cut -d'|' -f2)
    memory=$(echo "$cfg" | cut -d'|' -f3)

    apply_to_container "$container" "taskmanager" \
      "$RECOMMENDED_PARALLELISM" "$slots" "$memory" "$jm_addr" \
      "$OBJ_REUSE" "$MINI_BATCH_ENABLED"
  done

  echo ""
  echo "[6/6] 配置应用完成"

  # 重启
  if [[ "$RESTART" == true ]]; then
    echo ""
    echo "重启 Flink 集群..."

    for tm in $tm_containers; do
      echo "  停止 ${tm} 中的 TM 进程..."
      docker exec "$tm" bash -c 'kill $(ps aux | grep TaskManagerRunner | grep -v grep | awk "{print \$2}") 2>/dev/null || true'
    done
    sleep 3

    echo "  重启 ${jm_container}..."
    docker exec "$jm_container" bash -c 'kill $(ps aux | grep StandaloneSessionClusterEntrypoint | grep -v grep | awk "{print \$2}") 2>/dev/null || true'
    sleep 3
    docker exec -d "$jm_container" bash -c "nohup ${FLINK_HOME}/bin/jobmanager.sh start-foreground > /dev/null 2>&1 &"
    sleep 5

    echo "  等待 JM 就绪..."
    local jm_ready=0
    for i in $(seq 1 20); do
      if docker exec "$jm_container" bash -c 'curl -s http://localhost:8081/overview' 2>/dev/null | grep -q 'taskmanagers'; then
        jm_ready=1
        echo "  JM 已就绪"
        break
      fi
      sleep 3
    done
    if [[ "$jm_ready" == 0 ]]; then
      echo "  WARNING: JM 启动超时，请手动检查"
    fi

    local idx=0
    for tm in $tm_containers; do
      local procs="${tm_procs_list[$idx]}"
      echo "  启动 ${tm} 中的 ${procs} 个 TM 进程..."
      for ((p=1; p<=procs; p++)); do
        docker exec -d "$tm" bash -c "nohup ${FLINK_HOME}/bin/taskmanager.sh start-foreground > /dev/null 2>&1 &"
        sleep 2
      done
      idx=$((idx + 1))
    done

    echo "  等待 TM 注册..."
    sleep 15
    local tm_count
    local tm_json
    tm_json=$(docker exec "$jm_container" bash -c 'curl -s http://localhost:8081/taskmanagers' 2>/dev/null || echo "")
    tm_count=$(echo "$tm_json" | python3 -c "import json,sys; print(len(json.load(sys.stdin).get('taskmanagers',[])))" 2>/dev/null || echo "$tm_json" | grep -o '"id"' | wc -l || echo "0")
    local expected_tm=0
    for p in "${tm_procs_list[@]}"; do
      expected_tm=$((expected_tm + p))
    done
    echo "  已注册 TM: ${tm_count}/${expected_tm}"
    if [[ "$tm_count" != "$expected_tm" ]]; then
      echo "  WARNING: TM 注册数量不符，预期 ${expected_tm}，实际 ${tm_count}，请手动检查"
    fi

    echo "  集群重启完成"
  else
    echo ""
    echo "提示: 配置已应用，使用 --restart 重启集群使配置生效"
  fi

  echo ""
  echo "=========================================="
  echo "批量配置完成"
  echo "=========================================="
}

# ===========================================
# 单目标模式主流程
# ===========================================
run_single_target() {
  echo "=========================================="
  echo "Flink 配置应用工具"
  echo "=========================================="
  echo "目标: ${TARGET}"
  echo "Flink Home: ${FLINK_HOME}"
  echo "配置文件: ${CONFIG_PATH}"
  echo "=========================================="
  echo ""

  # 检测环境
  echo "[1/4] 检测环境..."
  if detect_container "$TARGET"; then
    DETECTED_ENV_TYPE="container"
    echo "  环境类型: 容器"
  else
    echo "Error: 目标 ${TARGET} 不是容器，本脚本仅支持容器环境"
    exit 1
  fi

  DETECTED_CPU_CORES=$(get_container_cpus "$TARGET")
  DETECTED_MEMORY_MB=$(get_container_memory_mb "$TARGET")
  DETECTED_ROLE=$(detect_role "$TARGET")
  DETECTED_JM_ADDRESS=$(get_jm_address)

  local TM_PROCS_IN_CONTAINER
  if [[ "$DETECTED_ROLE" == "taskmanager" ]]; then
    TM_PROCS_IN_CONTAINER=$(get_tm_procs_in_container "$TARGET")
  else
    TM_PROCS_IN_CONTAINER=0
  fi

  local TOTAL_TM_CONTAINERS
  TOTAL_TM_CONTAINERS=$(get_tm_count)
  [[ "$TOTAL_TM_CONTAINERS" -lt 1 ]] && TOTAL_TM_CONTAINERS=1

  echo "  CPU 核心数: ${DETECTED_CPU_CORES}"
  echo "  内存大小: ${DETECTED_MEMORY_MB} MB"
  echo "  容器角色: ${DETECTED_ROLE}"
  echo "  JobManager 地址: ${DETECTED_JM_ADDRESS}"
  if [[ "$DETECTED_ROLE" == "taskmanager" ]]; then
    echo "  容器内 TM 进程数: ${TM_PROCS_IN_CONTAINER}"
    echo "  全局 TM 容器数: ${TOTAL_TM_CONTAINERS}"
  fi
  echo ""

  if [[ "$DETECT_ONLY" == true ]]; then
    echo "[检测完成] 使用 --detect-only 模式，未应用配置"
    exit 0
  fi

  # 计算推荐参数
  echo "[2/4] 计算推荐参数..."

  local OBJ_REUSE MINI_BATCH_ENABLED
  OBJ_REUSE=$(resolve_object_reuse "$OBJECT_REUSE" "$STATE_BACKEND")
  MINI_BATCH_ENABLED=$(resolve_mini_batch "$MINI_BATCH")

  local TOTAL_TM_CPUS=0
  local tm_list
  tm_list=$(get_tm_containers)
  if [[ -n "$tm_list" ]]; then
    for tm in $tm_list; do
      local cpus
      cpus=$(get_container_cpus "$tm")
      TOTAL_TM_CPUS=$((TOTAL_TM_CPUS + cpus))
    done
  else
    TOTAL_TM_CPUS=$DETECTED_CPU_CORES
  fi

  RECOMMENDED_PARALLELISM=$(calc_parallelism "$TOTAL_TM_CPUS")

  if [[ "$DETECTED_ROLE" == "taskmanager" ]]; then
    if [[ "$TM_PER_CONTAINER" == "auto" ]]; then
      TM_PROCS=$TM_PROCS_IN_CONTAINER
      [[ "$TM_PROCS" -lt 1 ]] && TM_PROCS=1
    else
      TM_PROCS="$TM_PER_CONTAINER"
    fi

    RECOMMENDED_TASK_SLOTS=$(calc_task_slots "$RECOMMENDED_PARALLELISM" "$TOTAL_TM_CONTAINERS" "$TM_PROCS")

    tm_mem=$((DETECTED_MEMORY_MB / TM_PROCS))
    RECOMMENDED_TM_MEMORY="${tm_mem}m"

    echo "  parallelism.default = ${TOTAL_TM_CPUS}/2 = ${RECOMMENDED_PARALLELISM}"
    echo "  taskmanager.numberOfTaskSlots = ${RECOMMENDED_PARALLELISM}/${TOTAL_TM_CONTAINERS}/${TM_PROCS} = ${RECOMMENDED_TASK_SLOTS}"
    echo "  taskmanager.memory.process.size = ${DETECTED_MEMORY_MB}/${TM_PROCS} = ${RECOMMENDED_TM_MEMORY}"
  else
    RECOMMENDED_TASK_SLOTS=""
    RECOMMENDED_TM_MEMORY=""
    echo "  parallelism.default: ${RECOMMENDED_PARALLELISM}"
  fi
  echo "  pipeline.object-reuse: ${OBJ_REUSE} (状态后端: ${STATE_BACKEND})"
  echo "  table.exec.mini-batch.enabled: ${MINI_BATCH_ENABLED}"
  echo ""

  # 生成配置
  echo "[3/4] 生成配置..."
  YAML_CONTENT=$(generate_yaml_config \
    "$DETECTED_ROLE" \
    "$RECOMMENDED_PARALLELISM" \
    "$RECOMMENDED_TASK_SLOTS" \
    "$RECOMMENDED_TM_MEMORY" \
    "$DETECTED_JM_ADDRESS" \
    "$OBJ_REUSE" \
    "$MINI_BATCH_ENABLED")

  echo "生成的配置内容:"
  echo "----------------------------------------"
  echo "$YAML_CONTENT"
  echo "----------------------------------------"
  echo ""

  # 应用配置
  if [[ "$DRY_RUN" == true ]]; then
    echo "[4/4] [DRY-RUN] 未实际执行配置应用"
    echo ""
    echo "[DRY-RUN] 以下是将要执行的命令:"
    echo "docker exec ${TARGET} bash -c 'cp ${CONFIG_PATH} ${CONFIG_PATH}.bak'"
    echo "docker exec ${TARGET} bash -c 'cat > ${CONFIG_PATH}' << 'FLINK_EOF'"
    echo "$YAML_CONTENT"
    echo "FLINK_EOF"
  else
    echo "[4/4] 应用配置..."
    apply_config_docker "$TARGET" "$CONFIG_PATH" "$YAML_CONTENT"
    echo "配置已应用完成。"

    if [[ "$RESTART" == true ]]; then
      echo ""
      echo "重启 Flink 进程..."
      docker exec "$TARGET" bash -c "${FLINK_HOME}/bin/stop-cluster.sh 2>/dev/null || true"
      sleep 2
      docker exec "$TARGET" bash -c "${FLINK_HOME}/bin/start-cluster.sh"
      echo "Flink 已重启。"
    else
      echo ""
      echo "提示: 配置已应用，使用 --restart 重启使配置生效"
    fi
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
