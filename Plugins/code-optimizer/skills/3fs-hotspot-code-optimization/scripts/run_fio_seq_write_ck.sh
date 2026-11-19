#!/usr/bin/env bash
# =============================================================================
# 3FS 顺序写单句测试 + ClickHouse 各环节耗时收集
# 真源：本 skill（3fs-hotspot-code-optimization），阶段 1f 取证入口；
#       指标地图、统计口径与判读方法见 SKILL.md 阶段 1f
#
# 功能:
#   1. 在本机用 usrbio 引擎跑一次顺序写 fio (3FS FUSE 客户端节点)
#   2. 测试结束后立即从 ClickHouse 拉取:
#      - FUSE 侧: write/piov/iov 各阶段耗时
#      - Storage 侧: req_write -> req_update -> reliable_update -> do_update
#        -> engine_update/engine_commit/chunk_engine(pwrite,COW) 等全路径耗时
#   3. 汇总输出到终端，并落盘到 ${OUT_DIR}/
#
# 用法:
#   CK_PASS=... ./run_fio_seq_write_ck.sh              # 默认 size=4g bs=4M
#   SIZE=8g BS=4M CK_PASS=... ./run_fio_seq_write_ck.sh
#   FIO_OVERWRITE=1 ./run_fio_seq_write_ck.sh          # (默认已=1) 文件已存在时追加 --overwrite=1
#
# 安全注入 (敏感项无默认值; 避免明文进命令行/shell history):
#   read -rs CK_PASS && export CK_PASS                 # 交互输入 ClickHouse 密码
#   read -rs CK_SSH_PASS && export CK_SSH_PASS         # 交互输入 SSH 密码
#   SSH 密码非空时内部经 SSHPASS 环境变量走 sshpass -e, 绝不进命令行参数;
#   CK_SSH_PASS 留空则使用 SSH key 认证(优先推荐)。
#
# 依赖:
#   - 本机为 3FS FUSE 客户端且已挂载 ${MOUNT_POINT}
#   - ${USRBIO_SO} 存在 (默认 /usr/lib64/hf3fs_usrbio.so)
#   - ClickHouse 可达。分两种模式自动选择:
#       * 本机有 clickhouse-client: 直接本地查询
#       * 本机没有 clickhouse-client (如纯 FUSE 客户端节点): 通过 ssh 管道把 SQL
#         送到 CK_SSH_HOST (如 meta 节点) 上的 clickhouse-client 执行
#         (stdin 方式, 无需本机安装)
# =============================================================================
set -u

usage() {
    cat <<'EOF'
用法: [环境变量] run_fio_seq_write_ck.sh [-h|--help]

必填 (敏感项, 无默认值):
  CK_PASS        ClickHouse 密码。安全注入: read -rs CK_PASS && export CK_PASS

远程模式必填 (本机无 clickhouse-client 时):
  CK_SSH_HOST    ClickHouse 中转主机 (如 meta 节点地址; 文档示例用 192.0.2.x 网段)
  CK_SSH_PASS    SSH 密码 (无默认值; 留空 → SSH key 认证; 非空 → sshpass -e + SSHPASS)

可选 (默认值):
  SIZE=4g  BS=4M  CPU=12  MOUNT_POINT=/3fs/stage
  FIO_BIN=/usr/local/bin/fio  USRBIO_SO=/usr/lib64/hf3fs_usrbio.so
  CK_PORT=9123  CK_DB=3fs  CK_SSH_USER=root  OUT_DIR=./output
  CK_SETTLE_SECS=10  FIO_OVERWRITE=1
EOF
}

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
esac

# ---------- 可调参数 ----------
SIZE="${SIZE:-4g}"
BS="${BS:-4M}"
FIO_BIN="${FIO_BIN:-/usr/local/bin/fio}"
USRBIO_SO="${USRBIO_SO:-/usr/lib64/hf3fs_usrbio.so}"
MOUNT_POINT="${MOUNT_POINT:-/3fs/stage}"
TEST_DIR="$MOUNT_POINT/fio"
CPU="${CPU:-12}"
CK_PORT="${CK_PORT:-9123}"
CK_DB="${CK_DB:-3fs}"
CK_SSH_USER="${CK_SSH_USER:-root}"
OUT_DIR="${OUT_DIR:-./output}"
# 测试结束后的等待秒数，覆盖监控上报周期
CK_SETTLE_SECS="${CK_SETTLE_SECS:-10}"
# 默认覆盖已有文件; 设为 0 时若文件已存在 fio 会报错(即"清空/首次跑"不需要
# 额外参数; 若想强制"清空再跑", 用法: FIO_OVERWRITE=0 且先手动删除文件)
FIO_OVERWRITE="${FIO_OVERWRITE:-1}"

# ---------- 敏感项: 一律无默认值 ----------
CK_PASS="${CK_PASS:-}"
CK_SSH_HOST="${CK_SSH_HOST:-}"
CK_SSH_PASS="${CK_SSH_PASS:-}"

if [ -z "$CK_PASS" ]; then
    echo "[ERROR] CK_PASS 未设置 (ClickHouse 密码, 无默认值)。" >&2
    echo "        安全注入: read -rs CK_PASS && export CK_PASS" >&2
    echo "" >&2
    usage >&2
    exit 1
fi

# ---------- ClickHouse 执行器 (双模式) ----------
CK_MODE="local"
if ! command -v clickhouse-client >/dev/null 2>&1; then
    CK_MODE="remote"
    if [ -z "$CK_SSH_HOST" ]; then
        echo "[ERROR] 本机无 clickhouse-client, 远程模式必须设置 CK_SSH_HOST (无默认值)。" >&2
        echo "        示例: CK_SSH_HOST=192.0.2.10 (文档网段示例; 替换为实际 meta 节点地址)" >&2
        exit 1
    fi
    if [ -n "$CK_SSH_PASS" ] && ! command -v sshpass >/dev/null 2>&1; then
        echo "[ERROR] 设置了 CK_SSH_PASS 但本机无 sshpass;" >&2
        echo "        请安装 sshpass, 或清空 CK_SSH_PASS 改用 SSH key 认证。" >&2
        exit 1
    fi
fi

# SSH 封装: CK_SSH_PASS 为空 → key 认证; 非空 → sshpass -e (密码走 SSHPASS 环境变量)
ck_ssh() {
    if [ -n "$CK_SSH_PASS" ]; then
        SSHPASS="$CK_SSH_PASS" sshpass -e ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 "$@"
    else
        ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 "$@"
    fi
}

# run_ck <SQL>
# 本地模式: 直接执行
# 远程模式: stdout 经 ssh 管道送入远端 clickhouse-client 的 stdin
run_ck() {
    local sql="$1"
    if [ "$CK_MODE" = "local" ]; then
        printf '%s\n' "$sql" | clickhouse-client --port "$CK_PORT" --password "$CK_PASS"
    else
        printf '%s\n' "$sql" | ck_ssh "${CK_SSH_USER}@${CK_SSH_HOST}" \
            "clickhouse-client --port $CK_PORT --password $(printf '%q' "$CK_PASS")"
    fi
}

# ---------- 路径与基础检查 ----------
TS_STAMP="$(date +%Y%m%d_%H%M%S)"
OUT_FILE="$OUT_DIR/seq_write_ck_${TS_STAMP}.txt"
mkdir -p "$OUT_DIR"
touch "$OUT_FILE"
exec 1> >(tee -a "$OUT_FILE") 2>&1

echo "=============================================================================="
echo " 3FS 顺序写测试 + ClickHouse 耗时收集  ${TS_STAMP}"
echo "-----------------------------------------------------------------------------"
echo " 命令:  taskset -c $CPU $FIO_BIN --name=seq-write-smoke --directory=$TEST_DIR"
echo "         --size=$SIZE --bs=$BS --rw=write --ioengine=external:$USRBIO_SO"
echo "         --mountpoint=$MOUNT_POINT --numjobs=1 --direct=1 --group_reporting"
echo " CK 模式: $CK_MODE"
echo "=============================================================================="

[ -x "$FIO_BIN" ] || { echo "[ERROR] 找不到 fio: $FIO_BIN"; exit 1; }
[ -f "$USRBIO_SO" ] || { echo "[ERROR] 找不到 usrbio: $USRBIO_SO"; exit 1; }
mkdir -p "$TEST_DIR" || { echo "[ERROR] 无法创建测试目录 $TEST_DIR"; exit 1; }
run_ck "SELECT 1" >/dev/null 2>&1 \
  || { echo "[ERROR] ClickHouse 不可达 (本地端口 $CK_PORT / 远端 ${CK_SSH_HOST:-N/A});" >&2
       echo "        密码经 read -rs 注入, SSH 优先用 key (CK_SSH_PASS 留空)"; exit 1; }

# ---------- 1. 记录测试开始时间 ----------
T0="$(date '+%Y-%m-%d %H:%M:%S')"
T0_UNIX="$(date -d "$T0" +%s)"
echo "[$(date '+%H:%M:%S')] 测试开始时间: $T0"

# 获取 ClickHouse 服务器时钟，计算本机与 CK 的偏移
# 原因: storage 指标的时间戳由 storage 节点(≈META 时钟)上报，而本机若为 FUSE 客户端节点
#       时钟可能与 META 偏差数分钟。storage 查询窗口必须用 CK 时钟修正，否则查空。
CK_NOW="$(run_ck "SELECT toUnixTimestamp(now()) FORMAT TSVRaw" 2>/dev/null | tail -1 | tr -d '[:space:]')"
CK_OFFSET=0
if [ -n "$CK_NOW" ] && [ "$CK_NOW" -gt 0 ] 2>/dev/null; then
  CK_OFFSET=$((CK_NOW - $(date +%s)))
fi
echo "[$(date '+%H:%M:%S')] ClickHouse 时钟偏移: ${CK_OFFSET}s (CK时间 - 本机时间; storage 窗口将加上该偏移)"

# ---------- 2. 运行 fio ----------
FIO_CMD="taskset -c $CPU $FIO_BIN --name=seq-write-smoke \
  --directory=$TEST_DIR \
  --size=$SIZE \
  --bs=$BS \
  --rw=write \
  --ioengine=external:$USRBIO_SO \
  --mountpoint=$MOUNT_POINT \
  --numjobs=1 \
  --direct=1 \
  --group_reporting \
  --filename_format='seq-write.\$jobnum.0'"
# [ "${FIO_OVERWRITE:-1}" = "1" ] && FIO_CMD="$FIO_CMD --overwrite=1"

echo "[$(date '+%H:%M:%S')] 执行: $FIO_CMD"
echo "------------------------------------------------------------------------------"
time eval "$FIO_CMD"
FIO_RC=$?
echo "------------------------------------------------------------------------------"
echo "[$(date '+%H:%M:%S')] fio 返回码: $FIO_RC"

T1="$(date '+%Y-%m-%d %H:%M:%S')"
T1_UNIX="$(date -d "$T1" +%s)"
echo "[$(date '+%H:%M:%S')] 测试结束时间: $T1"

# ---------- 3. 等待监控数据上报 ----------
echo "[$(date '+%H:%M:%S')] 等待 $CK_SETTLE_SECS 秒让监控数据落库 ..."
sleep "$CK_SETTLE_SECS"

# 查询窗口: 测试开始前 30s ~ 结束后 60s，确保覆盖上报延迟
# BEG/END  = 本机时钟窗口(供 fuse 指标查询，fuse 由本机上报)
# BEG_CK/END_CK = 经时钟偏移修正后的窗口(供 storage 指标查询，storage 按 META 时钟上报)
BEG="$(date -d "@$((T0_UNIX - 30))" '+%Y-%m-%d %H:%M:%S')"
END="$(date -d "@$((T1_UNIX + 60))" '+%Y-%m-%d %H:%M:%S')"
BEG_CK="$(date -d "@$((T0_UNIX + CK_OFFSET - 30))" '+%Y-%m-%d %H:%M:%S')"
END_CK="$(date -d "@$((T1_UNIX + CK_OFFSET + 60))" '+%Y-%m-%d %H:%M:%S')"
echo "[$(date '+%H:%M:%S')] ClickHouse 查询窗口: $BEG ~ $END (fuse,本机时钟)"
echo "[$(date '+%H:%M:%S')] ClickHouse 查询窗口: $BEG_CK ~ $END_CK (storage,META时钟)"
echo

# ---------- 4. 汇总查询 ----------
echo "=============================================================================="
echo " [1/5] FUSE 侧写路径耗时 (fuse.iov.* / fuse.piov.* 系列, 口径辨析见 SKILL.md 阶段 1f)"
echo "       时间单位: us (原始 ns /1000); mean 为 count 加权平均"
echo "       fuse.iov.latency.ib_reg = iov 缓冲 IB 注册/注销延迟(脚本沿用为窗口时间线)"
echo "       fuse.iov.latency.map    = IoVector 映射/注册阶段时延"
echo "=============================================================================="
run_ck "
WITH
  '$BEG' AS begin_time,
  '$END' AS end_time
SELECT
  metricName,
  sum(count) AS total_count,
  round(sum(mean * count) / greatest(sum(count), 1) / 1000, 1) AS weighted_mean_us,
  round(max(p50) / 1000, 1) AS interval_p50_us,
  round(max(p90) / 1000, 1) AS interval_p90_us,
  round(max(p95) / 1000, 1) AS interval_p95_us,
  round(max(p99) / 1000, 1) AS interval_p99_us,
  round(max(max) / 1000, 1) AS max_seen_us
FROM \`$CK_DB\`.distributions
WHERE TIMESTAMP BETWEEN begin_time AND end_time
  AND mount_name = '$MOUNT_POINT'
  AND metricName IN (
    'fuse.iov.latency.ib_reg',
    'fuse.iov.latency.map',
    'fuse.write.latency',
    'fuse.piov.overall.write',
    'fuse.piov.prepare.write',
    'fuse.piov.submit.write',
    'fuse.piov.complete.write'
  )
GROUP BY metricName
  ORDER BY indexOf(
      ['fuse.iov.latency.ib_reg','fuse.iov.latency.map','fuse.write.latency',
       'fuse.piov.overall.write','fuse.piov.prepare.write','fuse.piov.submit.write',
       'fuse.piov.complete.write'],
      metricName
  ), metricName
FORMAT PrettyCompact
"
echo

echo "=============================================================================="
echo " [2/5] FUSE 侧写入量 (distributions 中的 fuse.iov.bytes, 窗口内合计)"
echo "       数据量单位: byte"
echo "=============================================================================="
run_ck "
WITH
  '$BEG' AS begin_time,
  '$END' AS end_time
SELECT
  metricName,
  sum(count) AS report_times,
  round(sum(mean * count) / greatest(sum(count), 1), 0) AS weighted_avg_bytes,
  sum(mean * count) AS total_bytes
FROM \`$CK_DB\`.distributions
WHERE TIMESTAMP BETWEEN begin_time AND end_time
  AND mount_name = '$MOUNT_POINT'
  AND metricName IN ('fuse.iov.bytes', 'fuse.iov.bytes.ib_reg', 'fuse.iov.times')
GROUP BY metricName
ORDER BY metricName
FORMAT PrettyCompact
"
echo

echo "=============================================================================="
echo " [3/5] Storage 写路径各环节耗时 (req_write -> update -> engine)"
echo "       时间单位: us (原始 ns /1000); 窗口使用 META/ClickHouse 时钟(BEG_CK/END_CK)"
echo "=============================================================================="
run_ck "
WITH
  '$BEG_CK' AS begin_time,
  '$END_CK' AS end_time
SELECT
  host,
  metricName,
  sum(count) AS total_count,
  round(sum(mean * count) / greatest(sum(count), 1) / 1000, 1) AS weighted_mean_us,
  round(max(p50) / 1000, 1) AS interval_p50_us,
  round(max(p90) / 1000, 1) AS interval_p90_us,
  round(max(p95) / 1000, 1) AS interval_p95_us,
  round(max(p99) / 1000, 1) AS interval_p99_us,
  round(max(max) / 1000, 1) AS max_seen_us
FROM \`$CK_DB\`.distributions
WHERE TIMESTAMP BETWEEN begin_time AND end_time
  AND metricName IN (
    'storage.req_write.succ_latency',        -- 收到写请求
    'storage.req_update.succ_latency',       -- 可靠更新请求入口
    'storage.reliable_update.succ_latency',  -- 副本转发更新
    'storage.update_remote.succ_latency',    -- 转发到远程副本
    'storage.reliable_forward.succ_latency', -- 副本转发
    'storage.do_update.succ_latency',        -- UpdateWorker 处理
    'storage.do_commit.succ_latency',        -- commit 阶段
    'storage.engine_update.succ_latency',    -- chunk-engine update
    'storage.engine_commit.succ_latency',    -- chunk-engine commit
    'storage.chunk_engine.pwrite_latency',   -- 数据落盘
    'storage.chunk_engine.copy_on_write_latency',
    'storage.chunk_engine.allocate_latency',
    'storage.pwrite.succ_latency',
    'storage.chunk_update.succ_latency',
    'storage.chunk_commit.succ_latency',
    'storage.wait_chunk_lock.succ_latency',
    'storage.wait_channel_lock.succ_latency',
    'storage.wait_post.succ_latency',
    'storage.wait_sem.succ_latency',
    'storage.update.queue_latency',
    'storage.sync_start.succ_latency',
    'storage.sync_done.succ_latency'
  )
GROUP BY host, metricName
  ORDER BY host,
    indexOf(
      ['storage.req_write.succ_latency',
       'storage.req_update.succ_latency',
       'storage.reliable_update.succ_latency',
       'storage.update_remote.succ_latency',
       'storage.reliable_forward.succ_latency',
       'storage.do_update.succ_latency',
       'storage.do_commit.succ_latency',
       'storage.engine_update.succ_latency',
       'storage.engine_commit.succ_latency',
       'storage.chunk_engine.pwrite_latency',
       'storage.chunk_engine.copy_on_write_latency',
       'storage.chunk_engine.allocate_latency',
       'storage.pwrite.succ_latency',
       'storage.chunk_update.succ_latency',
       'storage.chunk_commit.succ_latency',
       'storage.wait_chunk_lock.succ_latency',
       'storage.wait_channel_lock.succ_latency',
       'storage.wait_post.succ_latency',
       'storage.wait_sem.succ_latency',
       'storage.update.queue_latency',
       'storage.sync_start.succ_latency',
       'storage.sync_done.succ_latency'],
       metricName
     ), metricName
FORMAT PrettyCompact
"
echo

echo "=============================================================================="
echo " [4/5] Storage 写路径计数与字节 (counters)"
echo "       计数单位: 次; 数据量单位: byte (*.bytes / write_bytes 列)"
echo "       窗口使用 META/ClickHouse 时钟(BEG_CK/END_CK)"
echo "=============================================================================="
run_ck "
WITH
  '$BEG_CK' AS begin_time,
  '$END_CK' AS end_time
SELECT
  host,
  metricName,
  sum(val) AS window_total,
  round(avg(val), 2) AS avg_per_report,
  max(val) AS max_per_report
FROM \`$CK_DB\`.counters
WHERE TIMESTAMP BETWEEN begin_time AND end_time
  AND metricName IN (
    'storage.req_write.total',
    'storage.req_update.total',
    'storage.req_update.bytes',
    'storage.reliable_update.total',
    'storage.do_update.total',
    'storage.do_commit.total',
    'storage.engine_update.total',
    'storage.engine_commit.total',
    'storage.chunk_update.total',
    'storage.chunk_commit.total',
    'storage.reliable_forward.total',
    'storage.forward.write_bytes'
  )
GROUP BY host, metricName
  ORDER BY host,
    indexOf(
      ['storage.req_write.total',
       'storage.req_update.total',
       'storage.req_update.bytes',
       'storage.reliable_update.total',
       'storage.reliable_forward.total',
       'storage.do_update.total',
       'storage.do_commit.total',
       'storage.engine_update.total',
       'storage.engine_commit.total',
       'storage.chunk_update.total',
       'storage.chunk_commit.total',
       'storage.forward.write_bytes'],
       metricName
     ), metricName
FORMAT PrettyCompact
"
echo

echo "=============================================================================="
echo " [5/5] FUSE 写请求时延时间线 (fuse.iov.latency.ib_reg, 每秒, 确认测试窗口)"
echo "       时间单位: us (原始 ns /1000)"
echo "=============================================================================="
run_ck "
WITH
  '$BEG' AS begin_time,
  '$END' AS end_time
SELECT
  TIMESTAMP AS ts,
  sum(count) AS write_count,
  round(sum(mean * count) / greatest(sum(count), 1) / 1000, 1) AS weighted_mean_us,
  round(max(p99) / 1000, 1) AS interval_p99_us
FROM \`$CK_DB\`.distributions
WHERE TIMESTAMP BETWEEN begin_time AND end_time
  AND mount_name = '$MOUNT_POINT'
  AND metricName = 'fuse.iov.latency.ib_reg'
GROUP BY ts
ORDER BY ts
FORMAT PrettyCompact
"
echo
echo "结果已保存: $OUT_FILE"
