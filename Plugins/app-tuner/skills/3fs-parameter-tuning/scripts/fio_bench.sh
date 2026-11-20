#!/bin/bash
# ============================================================================
# fio_bench.sh — 3FS fio 压测统一入口
#
# 三个维度:
#   ENGINE=usrbio|fuse       usrbio: external 引擎直连 3FS(绕过 FUSE 内核)
#                            fuse  : libaio 经 FUSE 内核路径(写测试自动加 create_on_open=1)
#   MODE=single|multi        single: 本节点执行; multi: 按 NODES 清单 SSH 并行 + 集群总带宽汇总
#   WORKLOAD=micro|ai|mixed  micro: 顺序/随机读写微基准; ai: AI 训练负载模拟; mixed: 读写混合并行
#
# 用法: bash fio_bench.sh [TEST|all|cleanup]     (完整 env 旋钮见 --help)
#
# 密码纪律: 脚本内无任何明文密码。multi 模式优先 SSH key; 需要密码时
#           read -rs SSHPASS && export SSHPASS  (脚本通过 sshpass -e 读环境变量)
# ============================================================================

set -uo pipefail

# ---- 维度 ----
ENGINE="${ENGINE:-usrbio}"
MODE="${MODE:-single}"
WORKLOAD="${WORKLOAD:-micro}"

# ---- 路径 ----
FIO_BIN="${FIO_BIN:-/usr/local/bin/fio}"
USRBIO_SO="${USRBIO_SO:-/usr/lib64/hf3fs_usrbio.so}"
MOUNT_POINT="${MOUNT_POINT:-/3fs/stage}"
FIO_DIR="${FIO_DIR:-${MOUNT_POINT}/fio_bench}"
AI_BASE_DIR="${AI_BASE_DIR:-${MOUNT_POINT}/fio_workload}"
OUTPUT_DIR="${OUTPUT_DIR:-}"

# ---- micro 旋钮 ----
NUMJOBS="${NUMJOBS:-32}"
BS="${BS:-4M}"
IODEPTH="${IODEPTH:-8}"
IOR_DEPTH="${IOR_DEPTH:-0}"
IODEPTH_BATCH_SUBMIT="${IODEPTH_BATCH_SUBMIT:-8}"
IODEPTH_BATCH_COMPLETE_MIN="${IODEPTH_BATCH_COMPLETE_MIN:-8}"
IODEPTH_BATCH_COMPLETE_MAX="${IODEPTH_BATCH_COMPLETE_MAX:-8}"
WRITE_SIZE="${WRITE_SIZE:-512m}"
FILE_SIZE="${FILE_SIZE:-128m}"
RWMIXREAD="${RWMIXREAD:-70}"
TASKSET_CPU="${TASKSET_CPU:-}"

# ---- ai 旋钮 ----
RUNTIME="${RUNTIME:-}"                    # 空 = 各测试取默认(micro读20s/micro随机写30s/ai 60s)
AI_IODEPTH="${AI_IODEPTH:-}"              # 空 = usrbio:128, fuse:${IODEPTH}
PREFILL_IODEPTH="${PREFILL_IODEPTH:-32}"
DL_NUMJOBS="${DL_NUMJOBS:-16}";    DL_NRFILES="${DL_NRFILES:-500}";      DL_OPENFILES="${DL_OPENFILES:-128}"
PARQUET_NUMJOBS="${PARQUET_NUMJOBS:-8}"; PARQUET_NRFILES="${PARQUET_NRFILES:-50}"; PARQUET_OPENFILES="${PARQUET_OPENFILES:-16}"
CKPT_NUMJOBS="${CKPT_NUMJOBS:-4}"; CKPT_NRFILES="${CKPT_NRFILES:-4}"; CKPT_OPENFILES="${CKPT_OPENFILES:-4}"
CKPT_SIZE="${CKPT_SIZE:-4G}"; CKPT_MULTI_NRFILES="${CKPT_MULTI_NRFILES:-50}"; CKPT_MULTI_SIZE="${CKPT_MULTI_SIZE:-512m}"
PREP_NUMJOBS="${PREP_NUMJOBS:-16}"; PREP_NRFILES="${PREP_NRFILES:-200}"; PREP_OPENFILES="${PREP_OPENFILES:-64}"

# ---- multi 旋钮 ----
NODES="${NODES:-}"                        # 空格分隔的客户端清单
NODES_FILE="${NODES_FILE:-}"              # inventory 文件(每行一个 host, # 开头为注释)
SSH_USER="${SSH_USER:-root}"
LOCAL_NODE="${LOCAL_NODE:-}"              # NODES 中等于该值的节点本地执行, 不 SSH
REMOTE_JOB_DIR="${REMOTE_JOB_DIR:-/tmp/fio_bench_jobs}"
SSH_OPTS="${SSH_OPTS:--o StrictHostKeyChecking=no -o ConnectTimeout=10 -o LogLevel=ERROR}"

# ---- 其它 ----
CLEAN_DIR="${CLEAN_DIR:-0}"               # 1 = 测试前 rm -rf 测试目录(源脚本行为), 默认只删本测试文件
RDMA_MON_LIB="${RDMA_MON_LIB:-}"          # 指向 rdma_monitor.sh 时集成 RDMA 带宽监控
DEF_READ_RUNTIME=20; DEF_RAND_RUNTIME=30; DEF_AI_RUNTIME=60

PASS_COUNT=0; FAIL_COUNT=0
AGG_READ_BW=0; AGG_WRITE_BW=0; NODE_FAIL=0
RDMA_MON_READY=0; JOB_TMP=""
declare -a NODE_LIST=() SSH_OPTS_ARR=() FIO_ARGS=() ENGINE_ARGS=()
FIO_PREFIX=()
[ -n "$TASKSET_CPU" ] && FIO_PREFIX=(taskset -c "$TASKSET_CPU")

# ---- 日志 ----
log_info()  { echo -e "\033[32m[INFO]\033[0m  $(date '+%H:%M:%S') $*"; }
log_warn()  { echo -e "\033[33m[WARN]\033[0m  $*"; }
log_pass()  { echo -e "\033[32m[PASS]\033[0m  $*"; PASS_COUNT=$((PASS_COUNT + 1)); }
log_fail()  { echo -e "\033[31m[FAIL]\033[0m  $*"; FAIL_COUNT=$((FAIL_COUNT + 1)); }
log_title() { echo -e "\n\033[36m========== $* ==========\033[0m"; }
log_cmd()   { echo -e "\033[33m[CMD]\033[0m   $*"; }
log_total() { echo -e "\033[36m  >>> $*\033[0m"; }

usage() {
    cat <<'EOF'
用法: fio_bench.sh [TEST|all|cleanup]

维度(env): ENGINE=usrbio|fuse(默认 usrbio)  MODE=single|multi(默认 single)  WORKLOAD=micro|ai|mixed(默认 micro)

TEST 取值:
  micro : seq-write | seq-read | rand-write | rand-read | seq-write-multi
  ai    : dataloader | parquet-read | checkpoint-write | checkpoint-multi-write | data-prep-write
  mixed : mixed  (single=DataLoader读+Parquet读+Checkpoint写 三路并行; multi=各节点 randrw 混合并行)
  通用  : all(当前 WORKLOAD 全部) | cleanup(清理测试数据)

env 旋钮(括号内为默认值):
  路径   FIO_BIN(/usr/local/bin/fio) USRBIO_SO(/usr/lib64/hf3fs_usrbio.so) MOUNT_POINT(/3fs/stage)
         FIO_DIR(${MOUNT_POINT}/fio_bench) AI_BASE_DIR(${MOUNT_POINT}/fio_workload) OUTPUT_DIR(空=临时)
  micro  NUMJOBS(32) BS(4M) IODEPTH(8) IOR_DEPTH(0) IODEPTH_BATCH_SUBMIT(8)
         IODEPTH_BATCH_COMPLETE_MIN(8) IODEPTH_BATCH_COMPLETE_MAX(8) WRITE_SIZE(512m) FILE_SIZE(128m)
         RWMIXREAD(70) TASKSET_CPU(空=不绑核) CLEAN_DIR(0)
  ai     RUNTIME(空=读20s/随机写30s/ai 60s) AI_IODEPTH(空=usrbio:128,fuse:IODEPTH) PREFILL_IODEPTH(32)
         DL_NUMJOBS(16) DL_NRFILES(500) DL_OPENFILES(128) PARQUET_NUMJOBS(8) PARQUET_NRFILES(50)
         PARQUET_OPENFILES(16) CKPT_NUMJOBS(4) CKPT_NRFILES(4) CKPT_OPENFILES(4) CKPT_SIZE(4G)
         CKPT_MULTI_NRFILES(50) CKPT_MULTI_SIZE(512m) PREP_NUMJOBS(16) PREP_NRFILES(200) PREP_OPENFILES(64)
  multi  NODES("host1 host2") 或 NODES_FILE(inventory) SSH_USER(root) LOCAL_NODE(空)
         REMOTE_JOB_DIR(/tmp/fio_bench_jobs) SSH_OPTS SSHPASS(只能 read -rs + export 注入)
  监控   RDMA_MON_LIB(空=跳过) REMOTE_NODES / SSH_PASS / RDMA_MON_DEV / RDMA_MON_PORT(透传给 rdma_monitor.sh)

示例:
  bash fio_bench.sh seq-write
  NUMJOBS=32 BS=4M IODEPTH=8 bash fio_bench.sh all
  ENGINE=fuse WORKLOAD=ai RUNTIME=60 bash fio_bench.sh dataloader
  read -rs SSHPASS; export SSHPASS
  MODE=multi NODES="198.51.100.21 192.0.2.10" LOCAL_NODE=192.0.2.10 bash fio_bench.sh seq-write
  RDMA_MON_LIB=/path/to/rdma_monitor.sh REMOTE_NODES="192.0.2.11 192.0.2.12" bash fio_bench.sh seq-write
EOF
}

# ---- 工具函数 ----
size_to_bytes() {
    local num unit
    num=$(echo "$1" | grep -oE '^[0-9]+(\.[0-9]+)?')
    unit=$(echo "$1" | grep -oE '[A-Za-z]+$' | tr 'A-Z' 'a-z')
    awk -v n="${num:-0}" -v u="$unit" 'BEGIN{
        m=1; if(u=="k")m=1024; else if(u=="m")m=1048576;
        else if(u=="g")m=1073741824; else if(u=="t")m=1099511627776;
        printf "%.0f", n*m }'
}

mib_to_gib() { awk -v m="${1:-0}" 'BEGIN{printf "%.2f", m/1024}'; }

# 把 fio 汇总行带宽统一换算成 MiB/s 整数(GiB/MiB/KiB 混合输出必须归一后才能相加)
parse_bw_mib() {
    local output="$1" direction="$2" bw_str val unit
    bw_str=$(echo "$output" | grep "${direction}:" | grep -oE '[0-9]+\.?[0-9]*[[:space:]]*(GiB|MiB|KiB)/s' | head -1)
    if [ -z "$bw_str" ]; then echo 0; return; fi
    val=$(echo "$bw_str" | grep -oE '[0-9]+\.?[0-9]*' | head -1)
    unit=$(echo "$bw_str" | grep -oE '(GiB|MiB|KiB)' | head -1)
    awk -v v="$val" -v u="$unit" 'BEGIN{
        if(u=="GiB") printf "%.0f", v*1024; else if(u=="KiB") printf "%.0f", v/1024;
        else printf "%.0f", v }'
}

bw_display() {  # bw_display <output> [READ|WRITE]
    local pat='(READ|WRITE):'
    [ -n "${2:-}" ] && pat="${2}:"
    echo "$1" | grep -E "$pat" | grep -oE '[0-9]+\.?[0-9]*[[:space:]]*(GiB|MiB|KiB)/s' | head -1
}

node_suffix() {  # IP 取末段, 主机名取序号
    local s="${1##*.}"
    if [[ "$s" =~ ^[0-9]+$ ]]; then echo "$s"; else echo "$2"; fi
}

ssh_run() {
    local node="$1"; shift
    if [ -n "${SSHPASS:-}" ]; then sshpass -e ssh "${SSH_OPTS_ARR[@]}" "${SSH_USER}@${node}" "$@"
    else ssh "${SSH_OPTS_ARR[@]}" "${SSH_USER}@${node}" "$@"; fi
}

scp_to() {
    if [ -n "${SSHPASS:-}" ]; then sshpass -e scp "${SSH_OPTS_ARR[@]}" "$1" "${SSH_USER}@${2}:${3}"
    else scp "${SSH_OPTS_ARR[@]}" "$1" "${SSH_USER}@${2}:${3}"; fi
}

# ---- RDMA 监控(可选集成) ----
# rdma_monitor.sh 真源在 3fs-hotspot-code-optimization skill, 本脚本只按接口调用:
#   start_rdma_monitor / stop_rdma_monitor <label>; env: REMOTE_NODES/SSH_PASS/RDMA_MON_DEV/RDMA_MON_PORT
if [ -n "$RDMA_MON_LIB" ] && [ -f "$RDMA_MON_LIB" ]; then
    # shellcheck disable=SC1090
    source "$RDMA_MON_LIB"
    RDMA_MON_READY=1
fi
mon_start() { [ "$RDMA_MON_READY" = "1" ] && start_rdma_monitor; return 0; }
mon_stop()  { [ "$RDMA_MON_READY" = "1" ] && stop_rdma_monitor "${1:-test}"; return 0; }

# ---- 环境检查 ----
load_nodes() {  # 从 NODES_FILE(inventory) 或 NODES(空格分隔) 装载客户端清单
    NODE_LIST=()
    if [ -n "$NODES_FILE" ] && [ -f "$NODES_FILE" ]; then
        local line
        while IFS= read -r line || [ -n "$line" ]; do
            line="${line%%#*}"; line="${line//[[:space:]]/}"
            [ -n "$line" ] && NODE_LIST+=("$line")
        done < "$NODES_FILE"
    elif [ -n "$NODES" ]; then
        read -r -a NODE_LIST <<< "$NODES"
    fi
}

check_env() {
    case "$ENGINE" in usrbio|fuse) ;; *) log_fail "ENGINE 非法: $ENGINE (usrbio|fuse)"; exit 1 ;; esac
    case "$MODE" in single|multi) ;; *) log_fail "MODE 非法: $MODE (single|multi)"; exit 1 ;; esac
    case "$WORKLOAD" in micro|ai|mixed) ;; *) log_fail "WORKLOAD 非法: $WORKLOAD (micro|ai|mixed)"; exit 1 ;; esac

    df "${MOUNT_POINT}" &>/dev/null || { log_fail "挂载点 ${MOUNT_POINT} 不可用"; exit 1; }
    [ -x "${FIO_BIN}" ] || { log_fail "fio 不可执行: ${FIO_BIN} (用 FIO_BIN= 指定)"; exit 1; }
    if [ "$ENGINE" = "usrbio" ] && [ ! -f "${USRBIO_SO}" ]; then
        log_fail "USRBIO 引擎不存在: ${USRBIO_SO} (用 USRBIO_SO= 指定)"; exit 1
    fi
    if [ "$WORKLOAD" = "mixed" ] && [ "$MODE" = "single" ] && ! command -v python3 &>/dev/null; then
        log_warn "未找到 python3, mixed 单节点模式无法解析 JSON 结果"
    fi

    read -r -a SSH_OPTS_ARR <<< "$SSH_OPTS"
    if [ "$MODE" = "multi" ]; then
        [ "${#NODE_LIST[@]}" -gt 0 ] || { log_fail "MODE=multi 需要 NODES=\"host1 host2\" 或 NODES_FILE=<inventory>"; exit 1; }
        if [ -n "${SSHPASS:-}" ]; then
            export SSHPASS
            command -v sshpass &>/dev/null || { log_fail "设置了 SSHPASS 但未安装 sshpass"; exit 1; }
        else
            log_info "未设置 SSHPASS, 使用 SSH key 认证 (需要密码时: read -rs SSHPASS && export SSHPASS)"
        fi
        local chk="test -x ${FIO_BIN} && df ${MOUNT_POINT} >/dev/null 2>&1 && mkdir -p ${REMOTE_JOB_DIR}"
        [ "$ENGINE" = "usrbio" ] && chk="${chk} && test -f ${USRBIO_SO}"
        local node
        for node in "${NODE_LIST[@]}"; do
            if [ -n "$LOCAL_NODE" ] && [ "$node" = "$LOCAL_NODE" ]; then
                log_info "  ${node}: 本地节点, 跳过 SSH 检查"; continue
            fi
            if ssh_run "$node" "$chk" >/dev/null 2>&1; then log_info "  ${node}: SSH/fio/引擎/挂载点检查通过"
            else log_fail "  ${node}: 检查失败 (SSH 认证? fio=${FIO_BIN}? 引擎=${USRBIO_SO}? 挂载点=${MOUNT_POINT}?)"; exit 1; fi
        done
    fi
    [ "$RDMA_MON_READY" = "1" ] || log_warn "未提供 RDMA_MON_LIB, 跳过 RDMA 带宽监控 (指向 rdma_monitor.sh 即可启用)"

    mkdir -p "$FIO_DIR"
    if [ "$WORKLOAD" = "ai" ] || [ "$WORKLOAD" = "mixed" ]; then
        mkdir -p "${AI_BASE_DIR}"/{dataloader,parquet,checkpoint,checkpoint_multi,data_prep} 2>/dev/null || true
    fi
    JOB_TMP="$(mktemp -d /tmp/fio_bench_XXXXXX)"
    log_info "环境检查通过: fio=$(${FIO_BIN} --version), engine=${ENGINE}, mode=${MODE}, workload=${WORKLOAD}, 挂载点=${MOUNT_POINT}"
}

# ---- 测试记录表 ----
# 字段(15): name|dir|rw|bs|size|filesize|nrfiles|openfiles|numjobs|depthclass|runtime|fmt|direction|prefill|preclean
#   depthclass: micro(用 IODEPTH/IOR_DEPTH/BATCH_* 旋钮) | ai(用 AI_IODEPTH) | prefill(用 PREFILL_IODEPTH)
#   runtime   : 空(不用 time_based) | read|rand|ai(取对应默认, RUNTIME 可覆盖) | 数字
record_for() {
    case "$1" in
        seq-write)        echo "seq-write-smoke|${FIO_DIR}|write|${BS}|${WRITE_SIZE}||||${NUMJOBS}|micro||seq-write.\$jobnum.0|WRITE||" ;;
        seq-read)         echo "seq-read|${FIO_DIR}|read|${BS}|${FILE_SIZE}||||${NUMJOBS}|micro|read|seq-read.\$jobnum.0|READ|read|" ;;
        rand-write)       echo "rand-write-4k|${FIO_DIR}|randwrite|4k|${FILE_SIZE}||||${NUMJOBS}|micro|rand|rand-write-4k.\$jobnum.0|WRITE||" ;;
        rand-read)        echo "rand-read-4k|${FIO_DIR}|randread|4k|${FILE_SIZE}||||${NUMJOBS}|micro|read|seq-read.\$jobnum.0|READ|read|" ;;
        seq-write-multi)  echo "seq-write-multi|${FIO_DIR}|write|${BS}|${WRITE_SIZE}||||${NUMJOBS}|micro||seq-write-multi.\$jobnum.0|WRITE||" ;;
        dataloader)       echo "dataloader-read|${AI_BASE_DIR}/dataloader|randread|4k||4k-256k|${DL_NRFILES}|${DL_OPENFILES}|${DL_NUMJOBS}|ai|ai|dl_sample.\$jobnum.\$filenum|READ|dataloader|" ;;
        parquet-read)     echo "parquet-read|${AI_BASE_DIR}/parquet|read|4M||16m-256m|${PARQUET_NRFILES}|${PARQUET_OPENFILES}|${PARQUET_NUMJOBS}|ai|ai|pq_data.\$jobnum.\$filenum|READ|parquet|" ;;
        checkpoint-write) echo "checkpoint-write|${AI_BASE_DIR}/checkpoint|write|16M|${CKPT_SIZE}||${CKPT_NRFILES}|${CKPT_OPENFILES}|${CKPT_NUMJOBS}|ai|ai|ckpt.\$jobnum.\$filenum|WRITE||ckpt.*" ;;
        checkpoint-multi-write) echo "ckpt-multi-write|${AI_BASE_DIR}/checkpoint_multi|write|16M||${CKPT_MULTI_SIZE}|${CKPT_MULTI_NRFILES}|${CKPT_OPENFILES}|${CKPT_NUMJOBS}|ai|ai|ckpt_multi.\$jobnum.\$filenum|WRITE||ckpt_multi.*" ;;
        data-prep-write)  echo "data-prep-write|${AI_BASE_DIR}/data_prep|write|64k||4k-1M|${PREP_NRFILES}|${PREP_OPENFILES}|${PREP_NUMJOBS}|ai|ai|prep.\$jobnum.\$filenum|WRITE||prep.*" ;;
        mixed-multi)      echo "mixed-randrw|${FIO_DIR}|randrw|${BS}|${FILE_SIZE}||||${NUMJOBS}|micro|read|seq-read.\$jobnum.0|BOTH|read|" ;;
        *) return 1 ;;
    esac
}

set_job() {
    IFS='|' read -r J_NAME J_DIR J_RW J_BS J_SIZE J_FILESIZE J_NRFILES J_OPENFILES \
        J_NUMJOBS J_DEPTHCLASS J_RUNTIME_SPEC J_FMT J_DIRECTION J_PREFILL J_PRECLEAN <<< "$1"
    J_RWMIXREAD=""
    [ "$J_RW" = "randrw" ] && J_RWMIXREAD="$RWMIXREAD"
    case "$J_DEPTHCLASS" in
        micro)
            J_IODEPTH="$IODEPTH"; J_IOR="$IOR_DEPTH"; J_BSUB="$IODEPTH_BATCH_SUBMIT"
            J_BMIN="$IODEPTH_BATCH_COMPLETE_MIN"; J_BMAX="$IODEPTH_BATCH_COMPLETE_MAX" ;;
        ai)
            if [ -n "$AI_IODEPTH" ]; then J_IODEPTH="$AI_IODEPTH"
            elif [ "$ENGINE" = "usrbio" ]; then J_IODEPTH=128
            else J_IODEPTH="$IODEPTH"; fi
            J_IOR=0; J_BSUB="$J_IODEPTH"; J_BMIN="$J_IODEPTH"; J_BMAX="$J_IODEPTH" ;;
        prefill)
            J_IODEPTH="$PREFILL_IODEPTH"; J_IOR=0
            J_BSUB="$PREFILL_IODEPTH"; J_BMIN="$PREFILL_IODEPTH"; J_BMAX="$PREFILL_IODEPTH" ;;
    esac
    case "$J_RUNTIME_SPEC" in
        "")   J_TIME_BASED=""; J_RUNTIME="" ;;
        read) J_TIME_BASED=1; J_RUNTIME="${RUNTIME:-$DEF_READ_RUNTIME}" ;;
        rand) J_TIME_BASED=1; J_RUNTIME="${RUNTIME:-$DEF_RAND_RUNTIME}" ;;
        ai)   J_TIME_BASED=1; J_RUNTIME="${RUNTIME:-$DEF_AI_RUNTIME}" ;;
        *)    J_TIME_BASED=1; J_RUNTIME="$J_RUNTIME_SPEC" ;;
    esac
    return 0
}

build_job() {
    local rec
    rec="$(record_for "$1")" || { log_fail "WORKLOAD=${WORKLOAD} 下无测试 $1"; return 1; }
    set_job "$rec"
}

build_prefill_job() {
    case "$1" in
        read)
            set_job "prewrite-read|${FIO_DIR}|write|${BS}|${FILE_SIZE}||||${NUMJOBS}|micro||seq-read.\$jobnum.0|WRITE||"
            P_GLOB="seq-read.*.0"; P_EXPECT="$NUMJOBS"; P_CHECK_SIZE=1 ;;
        dataloader)
            set_job "dl-prefill|${AI_BASE_DIR}/dataloader|write|4k||4k-256k|${DL_NRFILES}||${DL_NUMJOBS}|prefill||dl_sample.\$jobnum.\$filenum|WRITE||"
            P_GLOB="dl_sample.*.*"; P_EXPECT=$((DL_NUMJOBS * DL_NRFILES)); P_CHECK_SIZE=0 ;;
        parquet)
            set_job "pq-prefill|${AI_BASE_DIR}/parquet|write|4M||16m-256m|${PARQUET_NRFILES}||${PARQUET_NUMJOBS}|prefill||pq_data.\$jobnum.\$filenum|WRITE||"
            P_GLOB="pq_data.*.*"; P_EXPECT=$((PARQUET_NUMJOBS * PARQUET_NRFILES)); P_CHECK_SIZE=0 ;;
        *) log_fail "未知预填充类型: $1"; return 1 ;;
    esac
}

# ---- fio 参数装配 ----
assemble_args() {
    ENGINE_ARGS=()
    if [ "$ENGINE" = "usrbio" ]; then
        ENGINE_ARGS+=(--ioengine="external:${USRBIO_SO}" --mountpoint="${MOUNT_POINT}"
            --ior_depth="${J_IOR}" --iodepth_batch_submit="${J_BSUB}"
            --iodepth_batch_complete_min="${J_BMIN}" --iodepth_batch_complete_max="${J_BMAX}")
    else
        ENGINE_ARGS+=(--ioengine=libaio)
        case "$J_RW" in write|randwrite) ENGINE_ARGS+=(--create_on_open=1) ;; esac
    fi
    ENGINE_ARGS+=(--iodepth="${J_IODEPTH}")
    FIO_ARGS=(--name="${J_NAME}" --directory="${J_DIR}" "${ENGINE_ARGS[@]}"
        --direct=1 --fallocate=none --group_reporting
        --rw="${J_RW}" --bs="${J_BS}" --numjobs="${J_NUMJOBS}")
    [ -n "$J_SIZE" ]      && FIO_ARGS+=(--size="${J_SIZE}")
    [ -n "$J_FILESIZE" ]  && FIO_ARGS+=(--filesize="${J_FILESIZE}")
    [ -n "$J_NRFILES" ]   && FIO_ARGS+=(--nrfiles="${J_NRFILES}")
    [ -n "$J_OPENFILES" ] && FIO_ARGS+=(--openfiles="${J_OPENFILES}")
    [ -n "$J_RWMIXREAD" ] && FIO_ARGS+=(--rwmixread="${J_RWMIXREAD}")
    [ -n "$J_TIME_BASED" ] && FIO_ARGS+=(--time_based --runtime="${J_RUNTIME}")
    # filename_format 必须原样传给 fio: $jobnum/$filenum 由 fio 展开, 若被 shell 展开
    # 则所有 job 写同一个文件, 会得到假结果(见 references/benchmarking.md 口径纪律)
    FIO_ARGS+=(--filename_format="${J_FMT}")
    return 0
}

# ---- 预填充 ----
prefill_local() {
    local count first sz need out rc=0
    mkdir -p "$J_DIR"
    count=$(find "$J_DIR" -maxdepth 1 -name "$P_GLOB" -type f 2>/dev/null | wc -l)
    if [ "$count" -ge "$P_EXPECT" ]; then
        if [ "$P_CHECK_SIZE" != "1" ]; then
            log_info "预填充数据已存在 (${count}/${P_EXPECT} 个文件), 跳过"; return 0
        fi
        first=$(find "$J_DIR" -maxdepth 1 -name "$P_GLOB" -type f 2>/dev/null | head -1)
        sz=$(stat -c %s "$first" 2>/dev/null || echo 0); need=$(size_to_bytes "$FILE_SIZE")
        if [ "$sz" -ge "$need" ]; then
            log_info "读测试数据已存在 (${count} 个 ${P_GLOB}, ${sz}B ≥ ${need}B), 跳过预写入"; return 0
        fi
        log_info "读测试数据尺寸不匹配 (${sz}B < ${need}B), 重新预写入"
    fi
    rm -f "${J_DIR}"/${P_GLOB} 2>/dev/null || true
    assemble_args
    log_info "预填充 ${J_NAME}: ${J_NUMJOBS} jobs → ${J_DIR}"
    out="$("${FIO_PREFIX[@]}" "${FIO_BIN}" "${FIO_ARGS[@]}" 2>&1)" || rc=$?
    if [ "$rc" -ne 0 ]; then log_fail "预填充失败 (${J_NAME}, rc=${rc})"; echo "$out" | tail -5; return 1; fi
    count=$(find "$J_DIR" -maxdepth 1 -name "$P_GLOB" -type f 2>/dev/null | wc -l)
    if [ "$count" -lt "$P_EXPECT" ]; then log_fail "预填充不完整: 期望 ${P_EXPECT} 个, 实际 ${count} 个"; return 1; fi
    log_info "预填充完成: ${count}/${P_EXPECT} 个文件"
}

do_prefill() {
    build_prefill_job "$1" || return 1
    if [ "$MODE" = "multi" ]; then
        log_info "预填充 $1 数据 (${#NODE_LIST[@]} 节点并行)..."
        local saved_pass="$PASS_COUNT"
        run_on_all_nodes
        PASS_COUNT="$saved_pass"   # 预填充不计入测试通过数, 失败仍计数
        [ "$NODE_FAIL" -gt 0 ] && { log_fail "预填充 $1 在部分节点失败"; return 1; }
    else
        prefill_local || return 1
    fi
    return 0
}

# ---- 单节点执行 ----
run_local_test() {
    assemble_args
    log_cmd "${FIO_PREFIX[*]} ${FIO_BIN} ${FIO_ARGS[*]}"
    local out rc=0 bw iops
    out="$("${FIO_PREFIX[@]}" "${FIO_BIN}" "${FIO_ARGS[@]}" 2>&1)" || rc=$?
    if [ -n "$OUTPUT_DIR" ]; then
        mkdir -p "$OUTPUT_DIR"; echo "$out" > "${OUTPUT_DIR}/${J_NAME}_$(date '+%Y%m%d_%H%M%S').log"
    fi
    bw="$(bw_display "$out")"
    iops=$(echo "$out" | grep -oE 'IOPS=[0-9]+\.?[0-9]*[kKmM]?' | head -1)
    if [ "$rc" -eq 0 ] && [ -n "$bw" ]; then
        log_pass "${J_NAME}: BW=${bw}, ${iops:-IOPS=N/A}"
        echo "$out" | grep -E 'READ:|WRITE:|bw \(|slat \(|clat \(|lat \(' || true
    else
        log_fail "${J_NAME} 测试失败 (rc=${rc})"; echo "$out" | tail -10
    fi
}

json_stat() {  # json_stat <file> <read|write>
    python3 - "$1" "$2" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))['jobs'][0][sys.argv[2]]
out = ["%.1fMiB/s" % (r['bw'] / 1024), "IOPS=%.0f" % r['iops']]
for k in ('slat', 'clat'):
    s, unit = r.get(k + '_ns'), 'ns'
    if s is None: s, unit = r.get(k), 'us'
    if not s: continue
    p = (s.get('percentile') or {}).get('99.000000')
    out.append("%s: avg=%.0f%s%s" % (k, s.get('mean', 0), unit,
               ", P99=%.0f%s" % (p, unit) if p else ""))
print(" | ".join(out))
PY
}

test_mixed_single() {
    local rt="${RUNTIME:-$DEF_AI_RUNTIME}" tmp t jf p i ok=1
    local -a pids=() labels=() files=() dirs=()
    log_title "混合工作负载 (DataLoader随机读 + Parquet顺序读 + Checkpoint顺序写 并行, runtime=${rt}s)"
    do_prefill dataloader || { log_fail "混合负载: DataLoader 预填充失败"; return; }
    do_prefill parquet    || { log_fail "混合负载: Parquet 预填充失败"; return; }
    tmp="$(mktemp -d "${JOB_TMP}/mixed_XXXXXX")"
    mon_start
    for t in dataloader parquet-read checkpoint-write; do
        build_job "$t"
        [ -n "$J_PRECLEAN" ] && rm -f "${J_DIR}"/${J_PRECLEAN} 2>/dev/null
        assemble_args
        jf="${tmp}/${J_NAME}.json"
        FIO_ARGS+=(--output-format=json --output="$jf")
        log_cmd "${FIO_PREFIX[*]} ${FIO_BIN} ${FIO_ARGS[*]}"
        "${FIO_PREFIX[@]}" "${FIO_BIN}" "${FIO_ARGS[@]}" >/dev/null 2>&1 &
        pids+=($!); labels+=("$t"); files+=("$jf"); dirs+=("$J_DIRECTION")
    done
    log_info "等待 ${#pids[@]} 个工作负载完成..."
    for p in "${pids[@]}"; do wait "$p" || ok=0; done
    mon_stop "混合负载"
    if [ "$ok" -eq 1 ]; then log_pass "混合负载全部完成"; else log_fail "混合负载中部分任务失败"; fi
    for i in "${!labels[@]}"; do
        echo "    ${labels[$i]}: $(json_stat "${files[$i]}" "${dirs[$i],,}" 2>/dev/null || echo "N/A")"
    done
    rm -rf "$tmp"
}

# ---- 多节点执行 ----
gen_job_file() {  # gen_job_file <path> <suffix>
    local path="$1" multi_fmt="${J_FMT/\$jobnum/\$jobnum.node${2}}"
    {
        echo "[global]"
        if [ "$ENGINE" = "usrbio" ]; then
            echo "ioengine=external:${USRBIO_SO}"
            echo "mountpoint=${MOUNT_POINT}"
            echo "ior_depth=${J_IOR}"
            echo "iodepth_batch_submit=${J_BSUB}"
            echo "iodepth_batch_complete_min=${J_BMIN}"
            echo "iodepth_batch_complete_max=${J_BMAX}"
        else
            echo "ioengine=libaio"
            case "$J_RW" in write|randwrite) echo "create_on_open=1" ;; esac
        fi
        echo "direct=1"; echo "fallocate=none"; echo "group_reporting"
        [ -n "$J_RWMIXREAD" ] && echo "rwmixread=${J_RWMIXREAD}"
        echo ""; echo "[${J_NAME}]"
        echo "directory=${J_DIR}"; echo "rw=${J_RW}"; echo "bs=${J_BS}"
        [ -n "$J_SIZE" ]     && echo "size=${J_SIZE}"
        [ -n "$J_FILESIZE" ] && echo "filesize=${J_FILESIZE}"
        [ -n "$J_NRFILES" ]  && echo "nrfiles=${J_NRFILES}"
        [ -n "$J_OPENFILES" ] && echo "openfiles=${J_OPENFILES}"
        echo "iodepth=${J_IODEPTH}"; echo "numjobs=${J_NUMJOBS}"
        echo "filename_format=${multi_fmt}"
        if [ -n "$J_TIME_BASED" ]; then echo "time_based"; echo "runtime=${J_RUNTIME}"; fi
    } > "$path"
}

launch_node() {  # launch_node <node> <local_job> <remote_job> <outfile>
    local node="$1" ljob="$2" rjob="$3" out="$4"
    if [ -n "$LOCAL_NODE" ] && [ "$node" = "$LOCAL_NODE" ]; then
        mkdir -p "$J_DIR"
        ( "${FIO_PREFIX[@]}" "${FIO_BIN}" "$ljob" > "$out" 2>&1 ) &
    else
        ssh_run "$node" "mkdir -p ${J_DIR} && ${TASKSET_CPU:+taskset -c ${TASKSET_CPU} }${FIO_BIN} ${rjob}" > "$out" 2>&1 &
    fi
}

run_on_all_nodes() {
    local -a pids=() outfiles=() used=()
    local i=0 node suffix job out pid k output rbw wbw disp
    AGG_READ_BW=0; AGG_WRITE_BW=0; NODE_FAIL=0
    for node in "${NODE_LIST[@]}"; do
        suffix="$(node_suffix "$node" "$i")"
        job="${JOB_TMP}/fio_${ENGINE}_${J_NAME}_node${suffix}.job"
        out="${JOB_TMP}/out_${J_NAME}_node${suffix}.log"
        gen_job_file "$job" "$suffix"
        if [ -n "$LOCAL_NODE" ] && [ "$node" = "$LOCAL_NODE" ]; then
            launch_node "$node" "$job" "$job" "$out"
        else
            scp_to "$job" "$node" "${REMOTE_JOB_DIR}/$(basename "$job")" >/dev/null 2>&1 || {
                log_fail "  ${node}: job 文件分发失败"; NODE_FAIL=$((NODE_FAIL + 1)); i=$((i + 1)); continue; }
            launch_node "$node" "$job" "${REMOTE_JOB_DIR}/$(basename "$job")" "$out"
        fi
        pids+=($!); outfiles+=("$out"); used+=("$node"); i=$((i + 1))
    done
    log_info "  等待 ${#pids[@]} 个节点并行执行 (${J_NAME})..."
    for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null; done
    for k in "${!used[@]}"; do
        output="$(cat "${outfiles[$k]}" 2>/dev/null)"
        if [ -n "$OUTPUT_DIR" ]; then mkdir -p "$OUTPUT_DIR"; cp "${outfiles[$k]}" "${OUTPUT_DIR}/" 2>/dev/null; fi
        rbw="$(parse_bw_mib "$output" READ)"; wbw="$(parse_bw_mib "$output" WRITE)"
        disp=""
        case "$J_DIRECTION" in
            READ)  disp="读 $(bw_display "$output" READ)" ;;
            WRITE) disp="写 $(bw_display "$output" WRITE)" ;;
            BOTH)  disp="读 $(bw_display "$output" READ) / 写 $(bw_display "$output" WRITE)" ;;
        esac
        if [ "$rbw" -gt 0 ] || [ "$wbw" -gt 0 ]; then
            log_pass "  ${used[$k]}: ${disp:-N/A}"
            AGG_READ_BW=$((AGG_READ_BW + rbw)); AGG_WRITE_BW=$((AGG_WRITE_BW + wbw))
        else
            log_fail "  ${used[$k]}: 测试失败"; echo "$output" | tail -5; NODE_FAIL=$((NODE_FAIL + 1))
        fi
    done
}

report_multi() {
    case "$J_DIRECTION" in
        READ)  log_total "集群总读带宽: ${AGG_READ_BW} MiB/s ($(mib_to_gib "$AGG_READ_BW") GiB/s)" ;;
        WRITE) log_total "集群总写带宽: ${AGG_WRITE_BW} MiB/s ($(mib_to_gib "$AGG_WRITE_BW") GiB/s)" ;;
        BOTH)  log_total "集群总混合带宽: 读 ${AGG_READ_BW} + 写 ${AGG_WRITE_BW} = $((AGG_READ_BW + AGG_WRITE_BW)) MiB/s ($(mib_to_gib $((AGG_READ_BW + AGG_WRITE_BW))) GiB/s)" ;;
    esac
}

# ---- 调度 ----
run_test() {
    local test="$1" conc rt="" n
    if [ "$test" = "mixed" ] && [ "$MODE" = "single" ]; then test_mixed_single; return; fi
    [ "$test" = "mixed" ] && test="mixed-multi"
    build_job "$test" || return 1
    if [ -n "$J_PREFILL" ]; then
        do_prefill "$J_PREFILL" || return 1
        build_job "$test"
    fi
    conc="$J_NUMJOBS"; [ "$MODE" = "multi" ] && conc="${J_NUMJOBS}×${#NODE_LIST[@]}节点"
    [ -n "$J_TIME_BASED" ] && rt=", runtime=${J_RUNTIME}s"
    log_title "${J_NAME} (engine=${ENGINE}, mode=${MODE}, bs=${J_BS}, numjobs=${conc}, iodepth=${J_IODEPTH}${rt})"

    if [ "$CLEAN_DIR" = "1" ]; then
        case "$J_DIR" in "$MOUNT_POINT"|"$MOUNT_POINT"/) log_fail "CLEAN_DIR=1 拒绝清理挂载点根目录"; return 1 ;; esac
        log_warn "CLEAN_DIR=1: 清空 ${J_DIR}"
        if [ "$MODE" = "multi" ]; then
            for n in "${NODE_LIST[@]}"; do ssh_run "$n" "rm -rf ${J_DIR} && mkdir -p ${J_DIR}" >/dev/null 2>&1 & done; wait
        fi
        rm -rf "$J_DIR"; mkdir -p "$J_DIR"
    elif [ -n "$J_PRECLEAN" ]; then
        if [ "$MODE" = "multi" ]; then
            for n in "${NODE_LIST[@]}"; do
                if [ -n "$LOCAL_NODE" ] && [ "$n" = "$LOCAL_NODE" ]; then rm -f "${J_DIR}"/${J_PRECLEAN} 2>/dev/null
                else ssh_run "$n" "rm -f ${J_DIR}/${J_PRECLEAN}" >/dev/null 2>&1 & fi
            done; wait
        else
            rm -f "${J_DIR}"/${J_PRECLEAN} 2>/dev/null || true
        fi
    fi

    mon_start
    if [ "$MODE" = "multi" ]; then run_on_all_nodes; report_multi; else run_local_test; fi
    mon_stop "$J_NAME"
}

do_cleanup() {
    log_info "清理测试数据: ${FIO_DIR} 与 ${AI_BASE_DIR}"
    if [ "$MODE" = "multi" ]; then
        local node
        for node in "${NODE_LIST[@]}"; do
            ssh_run "$node" "rm -rf ${FIO_DIR} ${AI_BASE_DIR} ${REMOTE_JOB_DIR}" >/dev/null 2>&1 &
        done
        wait
    fi
    rm -rf "$FIO_DIR" "$AI_BASE_DIR"
    log_info "清理完成"
}

tests_for_workload() {
    case "$WORKLOAD" in
        micro) echo "seq-write seq-read rand-write rand-read seq-write-multi" ;;
        ai)    echo "dataloader parquet-read checkpoint-write checkpoint-multi-write data-prep-write" ;;
        mixed) echo "mixed" ;;
    esac
}

on_exit() {
    if [ "$MODE" = "multi" ] && [ "${#NODE_LIST[@]}" -gt 0 ]; then
        local node pat="${REMOTE_JOB_DIR}/fio_${ENGINE}_"
        for node in "${NODE_LIST[@]}"; do
            [ -n "$LOCAL_NODE" ] && [ "$node" = "$LOCAL_NODE" ] && continue
            ssh_run "$node" "pkill -f '${pat}' 2>/dev/null; true" >/dev/null 2>&1 &
        done
        wait
    fi
    [ -n "$JOB_TMP" ] && [ -d "$JOB_TMP" ] && rm -rf "$JOB_TMP"
    return 0
}

banner() {
    echo "============================================================"
    echo "  3FS fio 压测 (ENGINE=${ENGINE}, MODE=${MODE}, WORKLOAD=${WORKLOAD})"
    echo "  时间: $(date '+%Y-%m-%d %H:%M:%S')   节点: $(hostname)"
    echo "  fio:  $(${FIO_BIN} --version 2>/dev/null)"
    echo "  引擎: $([ "$ENGINE" = usrbio ] && echo "USRBIO ${USRBIO_SO}" || echo "libaio (经 FUSE 内核)")"
    echo "  挂载点: ${MOUNT_POINT}   测试目录: ${FIO_DIR}"
    [ "$MODE" = "multi" ] && echo "  客户端: ${NODE_LIST[*]}   本地节点: ${LOCAL_NODE:-无}"
    if [ "$WORKLOAD" = "micro" ]; then
        echo "  并发: ${NUMJOBS}  块大小: ${BS}  iodepth: ${IODEPTH}  ior_depth: ${IOR_DEPTH}  batch: ${IODEPTH_BATCH_SUBMIT}/${IODEPTH_BATCH_COMPLETE_MIN}-${IODEPTH_BATCH_COMPLETE_MAX}"
    else
        echo "  运行时长: ${RUNTIME:-默认(读20s/随机写30s/ai 60s)}  AI iodepth: ${AI_IODEPTH:-自动}"
    fi
    [ -n "$TASKSET_CPU" ] && echo "  绑核: taskset -c ${TASKSET_CPU}"
    [ -n "$OUTPUT_DIR" ] && echo "  原始输出: ${OUTPUT_DIR}"
    echo "============================================================"
}

main() {
    local test="${1:-all}" t valid
    case "$test" in -h|--help|help) usage; exit 0 ;; esac
    load_nodes
    banner
    check_env
    trap on_exit EXIT
    if [ "$test" = "cleanup" ]; then
        do_cleanup
    elif [ "$test" = "all" ]; then
        for t in $(tests_for_workload); do run_test "$t"; done
    else
        valid="$(tests_for_workload)"
        if [[ " ${valid} " != *" ${test} "* ]]; then
            log_fail "WORKLOAD=${WORKLOAD} 下不支持 TEST=${test} (可选: ${valid} all cleanup)"
            exit 1
        fi
        run_test "$test"
    fi
    echo ""
    echo "============================================================"
    echo "  测试结果汇总   通过: ${PASS_COUNT}    失败: ${FAIL_COUNT}"
    echo "============================================================"
    [ "${FAIL_COUNT}" -gt 0 ] && exit 1
    return 0
}

main "$@"
