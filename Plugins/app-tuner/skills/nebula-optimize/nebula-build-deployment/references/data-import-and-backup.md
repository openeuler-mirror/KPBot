# 数据导入、对账与备份恢复

## 1. 数据集选择

| 数据集 | 规模（参考） | 用途 | 备注 |
|--------|-------------|------|------|
| LDBC SF10 | 6.5 万 Person、190 万 KNOWS、约 2.07 亿行 | 功能 + 性能 | 单机常用 |
| LDBC SF100 | 2.83 亿顶点、17.8 亿边 | 大规模/混合负载 | 导入约 20–25 分钟（视磁盘/并行） |
| NBA | 86 顶点 / 244 边 | TCK/功能冒烟 | 官方示例 |
| 业务数据 | 按实际 | 生产验证 | 注意 VID 类型与索引 |

LDBC SNB 的核心实体：Person、Comment、Post、Forum、Place、Organisation、Tag、Tagclass；
边：KNOWS、LIKES、HAS_TAG、HAS_CREATOR、HAS_MEMBER、REPLY_OF、CONTAINER_OF、
IS_LOCATED_IN、STUDY_AT、WORK_AT、HAS_INTEREST、HAS_MODERATOR、HAS_TYPE、IS_PART_OF、
IS_SUBCLASS_OF 等。

## 2. CSV 准备

LDBC datagen 产出的 CSV 通常带表头，而 importer 的 `withHeader` 在部分版本有崩溃问题。
稳妥做法：统一去掉表头，使用无表头副本。

```bash
# 对每个 CSV 去表头（保留原始文件，另存 hdrless 目录）
mkdir -p "${DATA_ROOT}/csv_hdrless"
for f in "${CSV_DIR}"/*.csv; do
  base=$(basename "$f")
  tail -n +2 "$f" > "${DATA_ROOT}/csv_hdrless/${base}"
done

# 抽查：第一行应是数据，不是列名
head -1 "${DATA_ROOT}/csv_hdrless/person.csv"
# 期望形如：933|Mahinda|Perera|male|...
```

其他要点：

- 确认分隔符（LDBC 为 `|`）、日期格式（`yyyy-MM-dd'T'HH:mm:ss`）、空值表示。
- 字符串属性如果有索引，长度必须与 schema 一致（如 `name(64)`）。
- 数据文件与 importer YAML 中的路径用绝对路径，避免工作目录变化导致找不到。

## 3. Space 与 Schema

```ngql
CREATE SPACE IF NOT EXISTS ldbc_bench (
  partition_num = 96,
  replica_factor = 1,
  vid_type = FIXED_STRING(32)
);
USE ldbc_bench;

CREATE TAG IF NOT EXISTS Person(name string, birthday datetime, ...);
CREATE EDGE IF NOT EXISTS KNOWS(creationDate datetime, ...);
-- 其余 Tag/Edge 按数据集定义
```

选择建议：

| 决策 | 建议 |
|------|------|
| VID 类型 | 优先 INT64（哈希/比较/内存更省）；必须用字符串时选合适长度 |
| 分区数 | SF10 可用 24–96；SF100 常用 96；更多分区增加导入与元数据开销 |
| 副本数 | 单机验证用 1；生产按容灾要求 ≥3 |
| 索引 | 只建实际查询需要的；每个索引都增加写入放大 |

## 4. importer 配置

最小 YAML 结构（字段名以 importer v3.1.0 为准）：

```yaml
# 字段以 importer v3.1.0 为准：supportedVersions 只有 v1/v1rc1/v1rc2/v2（v3 是 v4 的
# 字段）；NebulaClientSettings 无 retentionInterval（v4 才引入）。address 支持逗号分隔
# 多个 graphd 地址（clientpool 按逗号拆分建池），多 graphd 拓扑应全部填上。
version: v2
description: LDBC SF10 import
clientSettings:
  concurrency: 32
  connection:
    address: 127.0.0.1:9670,127.0.0.1:9671
    user: root
    password: <由环境变量或密钥管理器渲染，不写明文>
  space: ldbc_bench
logPath: ./import.log
files:
  - path: /data/nebula/csv_hdrless/person.csv
    failDataPath: ./failed/person.csv
    batchSize: 256
    type: csv
    csv:
      withHeader: false
      delimiter: "|"
    schema:
      type: vertex
      vertex:
        vid:
          index: 0
          type: string
        tags:
          - name: Person
            props:
              - name: name
                type: string
                index: 1
              - name: birthday
                type: datetime
                index: 4
  - path: /data/nebula/csv_hdrless/person_knows_person.csv
    ...
    schema:
      type: edge
      edge:
        name: KNOWS
        srcVID: { index: 0, type: string }
        dstVID: { index: 1, type: string }
        props:
          - { name: creationDate, type: datetime, index: 2 }
```

执行：

```bash
"${IMPORTER_BIN}" -config "${CONF_DIR}/ldbc_sf10_import.yaml"
# 观察日志中的成功/失败计数与速率
```

注意事项：

- importer 的 address 填逗号分隔的全部 graphd 入口（clientpool 按逗号拆分建池），多 graphd
  拓扑只填一个入口会把导入时间人为拉长。
- `concurrency` 过高会打满 graphd/storaged；先小后大，观察失败率与 CPU。
- 导入期间关闭不必要的日志/监控，避免 I/O 干扰。
- 导入完成后执行 `SUBMIT JOB COMPACT`（或等价 compaction）再压测，减少后台 flush 干扰。

## 5. 导入后对账

必须做，不能只看 importer “成功”。

```ngql
USE ldbc_bench;
-- 顶点/边计数（按 Tag/Edge 分别查）
MATCH (n:Person) RETURN count(n);
MATCH ()-[e:KNOWS]->() RETURN count(e);

-- 抽查点属性
FETCH PROP ON Person '933' YIELD Person.name, Person.birthday;

-- 抽查单跳
GO 1 STEP FROM '933' OVER KNOWS YIELD KNOWS.creationDate;
```

对账要点：

- 记录每个 Tag/Edge 的期望行数与实际计数。
- LDBC 的 VID 数值区间可能重叠，导致少量顶点合并/同 key 边覆盖；`Space vertices`
  可能小于各 Tag 计数之和，但 Tag 计数应精确。出现差异先确认是否属于该固有现象。
- 索引重建完成后再次抽查 LOOKUP，确认索引可用。
- 保存导入日志、失败文件、对账 SQL 与结果，作为后续基线。

## 6. 备份

### 6.1 原则

- **停服备份**：至少保证 meta 与所有 storage 是同一时刻的一致快照。
- meta 与 storage 必须成对；只备份 storage 会导致 Space/Host 元数据丢失。
- 用 `cp -a --reflink=never`（或 `rsync --no-...`）做真实拷贝，不要依赖 COW 快照。
- 备份前记录二进制 md5、commit、RocksDB 版本、Space 列表与行数。

### 6.2 命令

> 目录口径必须与部署模板一致：`meta` 一份，storaged 每个实例一个 `storage${N}`
> （见 `topology-and-config.md` §7）。下面按 `STORAGE_INSTANCES` 遍历，不用不存在的
> 单一 `storage` 目录。

```bash
TS=$(date +%Y%m%d_%H%M%S)
BK="${BACKUP_ROOT}/.stage_${TS}_$$"        # 先在独立暂存目录完成整套拷贝与校验
FINAL="${BACKUP_ROOT}/${TS}"                # 校验通过后原子切换到最终目录
STORAGE_INSTANCES="${STORAGE_INSTANCES:-4}"
STOP_TIMEOUT="${STOP_TIMEOUT:-120}"
PID_DIR="${PID_DIR:-/data/nebula/pids}"
# 同名最终目录已存在（同秒重跑等）时拒绝：不覆盖、不合并旧备份
[ ! -e "$FINAL" ] || { echo "ERROR: ${FINAL} 已存在，请更换时间戳后重跑" >&2; exit 1; }
mkdir -p "$BK"

# 停止（反序），并等待进程真正退出；固定 sleep 不能保证 RocksDB 已收尾。
# 仅支持分布式三进程形态；standalone 单进程会“无 PID 文件可停”而误判已停，直接拒绝。
command -v pgrep >/dev/null 2>&1 || { echo "ERROR: 缺少 pgrep，无法确认部署形态" >&2; exit 1; }
# -f 匹配完整命令行：comm 15 字符截断使 pgrep -x 匹配不到 nebula-standalone。
pgrep -f nebula-standalone >/dev/null 2>&1 && {
  echo "ERROR: 检测到 nebula-standalone 进程，本流程不适用于 standalone 形态" >&2
  exit 1
}
stop_component() {
  # 只停本次部署的实例：从 ${PID_DIR} 的 pid 文件定位（进程经 --pid_file 自写、
  # 必然准确——daemonize 下 shell 的 $! 是已退出的父进程，不可用），核对
  # /proc/PID/exe 属于本 BUILD_DIR 后才发 TERM；禁止按进程名 pkill。
  local name="$1" timeout="${2:-120}" waited=0 pid exe pidfile alive=0
  local -a pids=()
  for pidfile in "${PID_DIR}/${name}"*.pid; do
    [ -e "$pidfile" ] || {
      echo "ERROR: 未找到 ${PID_DIR}/${name}*.pid（进程自写的 --pid_file），无法定位本次部署的 ${name}" >&2
      return 1
    }
    pid=$(cat "$pidfile")
    # kill -0 失败≠进程已退出：无 /proc/<pid> 条目才算退出（清理 pidfile）；
    # 进程存在但无信号权限（EPERM）时无法确认退出，保留 pidfile 并中止。
    if ! kill -0 "$pid" 2>/dev/null; then
      if [ ! -d "/proc/${pid}" ]; then
        rm -f "$pidfile"; continue
      fi
      echo "ERROR: PID ${pid}（${pidfile}）存在但无信号权限，无法确认退出，中止——请核对运行身份后重试" >&2
      return 1
    fi
    exe=$(readlink -f "/proc/${pid}/exe" 2>/dev/null || true)
    case "$exe" in
      "${BUILD_DIR}/bin/${name}") : ;;
      *) echo "ERROR: ${pidfile} 中 PID ${pid} 的可执行文件(${exe:-未知})不属于本次部署，拒绝停止" >&2; return 1 ;;
    esac
    # 第二重核对：flagfile 必属本次 CONF_DIR（两套部署共用 BUILD_DIR 时仅凭 exe 不够）
    cmdline=$(tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null || true)
    case "$cmdline" in
      *"--flagfile=${CONF_DIR}/"*|*"--flagfile ${CONF_DIR}/"*) pids+=("$pid") ;;
      "") pids+=("$pid") ;;
      *) echo "ERROR: PID ${pid} 的 flagfile 不在本次 CONF_DIR(${CONF_DIR})——疑似另一实例的进程，拒绝停止" >&2; return 1 ;;
    esac
  done
  [ "${#pids[@]}" -gt 0 ] || return 0
  kill -TERM "${pids[@]}" 2>/dev/null || true
  while :; do
    alive=0
    for pid in "${pids[@]}"; do [ -d "/proc/${pid}" ] && alive=1; done   # 按 /proc 存在性判活，避免 kill -0 权限失败误判为已退出
    [ "$alive" -eq 0 ] && break
    if [ "$waited" -ge "$timeout" ]; then
      echo "ERROR: ${name} 未在 ${timeout}s 内退出，停止备份" >&2
      return 1
    fi
    sleep 1
    waited=$((waited + 1))
  done
  rm -f "${PID_DIR}/${name}"*.pid
}
stop_component nebula-storaged "$STOP_TIMEOUT" || exit 1
stop_component nebula-graphd   "$STOP_TIMEOUT" || exit 1
stop_component nebula-metad    "$STOP_TIMEOUT" || exit 1

# 拷贝前校验源目录完整，缺一个即中止；符号链接布局一律拒绝——cp -a 会把链接本身
# 复制进备份（绝对链接也能通过非空检查），服务重启后备份随在线数据变化、不再是独立副本
for src in "${DATA_ROOT}/meta" $(seq 1 "$STORAGE_INSTANCES" | sed "s/^/${DATA_ROOT}\/storage/"); do
  [ -d "$src" ] || { echo "ERROR: 缺少 ${src}" >&2; exit 1; }
  [ -L "$src" ] && {
    echo "ERROR: ${src} 是符号链接（cp -a 只复制链接本身，备份不独立于在线目录），拒绝此布局；请用真实目录（或将数据迁移到真实目录）后重做备份" >&2
    exit 1
  }
done

# 成对拷贝；逐条检查返回码，失败即中止
cp -a --reflink=never "${DATA_ROOT}/meta" "$BK/meta" || exit 1
for i in $(seq 1 "$STORAGE_INSTANCES"); do
  cp -a --reflink=never "${DATA_ROOT}/storage${i}" "$BK/storage${i}" || exit 1
done

# 备份完整性：meta 非空、每个 storage 目录存在且非空
[ -n "$(ls -A "$BK/meta")" ] || { echo "ERROR: 备份 meta 为空" >&2; exit 1; }
for i in $(seq 1 "$STORAGE_INSTANCES"); do
  [ -n "$(ls -A "$BK/storage${i}")" ] || {
    echo "ERROR: 备份 storage${i} 为空" >&2
    exit 1
  }
done

# 记录身份
md5sum "${BUILD_DIR}/bin/nebula-"{metad,graphd,storaged} > "$BK/bin.md5"
git -C "${NEBULA_HOME}" rev-parse --short HEAD > "$BK/commit.txt"

# 暂存目录校验全部通过后，原子切换到最终目录（BACKUP_ROOT 与数据目录同文件
# 系统时 mv 为原子改名）；失败/中断时暂存目录留在原地，可人工检查后删除。
mv "$BK" "$FINAL" || { echo "ERROR: 切换最终备份目录失败，暂存目录保留在 ${BK}" >&2; exit 1; }
echo "备份完成: ${FINAL}"
du -sh "$FINAL"/*
```

要点：

- `STORAGE_INSTANCES` 必须与实际启动的 storaged 数一致；默认 4 仅对应参考拓扑。
- 不允许只备份 `meta` 或只备份部分 `storage${N}`；恢复时必须来自同一份备份。
- 先在暂存目录完成整套拷贝与校验，通过后才切换为正式备份；目标目录已存在一律拒绝，
  避免同秒重跑把两轮内容混进同一目录。
- 备份完成后按 `metad → graphd → storaged` 顺序重启服务。

### 6.3 增量/在线备份

- Nebula 支持 snapshot/checkpoint 类管理命令，但跨 meta+storage 的一致性仍需停写或
  使用框架级快照；没有把握时用停服备份。
- 在线拷贝可能得到不一致的 WAL/SST 组合，恢复后表现为 `cluster.id` 不匹配、分区
  OFFLINE 或数据缺失。

## 7. 恢复

```bash
STORAGE_INSTANCES="${STORAGE_INSTANCES:-4}"
STOP_TIMEOUT="${STOP_TIMEOUT:-120}"
PID_DIR="${PID_DIR:-/data/nebula/pids}"
# 要恢复的备份：指向 §6.2 校验通过后切换出的最终备份目录（时间戳目录名）
RESTORE_TS="${RESTORE_TS:?须指定备份时间戳目录名，如 20261123_101500}"
BK="${BACKUP_ROOT}/${RESTORE_TS}"

command -v pgrep >/dev/null 2>&1 || { echo "ERROR: 缺少 pgrep，无法确认部署形态" >&2; exit 1; }
# -f 匹配完整命令行：comm 15 字符截断使 pgrep -x 匹配不到 nebula-standalone。
pgrep -f nebula-standalone >/dev/null 2>&1 && {
  echo "ERROR: 检测到 nebula-standalone 进程，本流程不适用于 standalone 形态" >&2
  exit 1
}
stop_component() {
  local name="$1" timeout="${2:-120}" waited=0 pid exe pidfile alive=0
  local -a pids=()
  for pidfile in "${PID_DIR}/${name}"*.pid; do
    [ -e "$pidfile" ] || {
      echo "ERROR: 未找到 ${PID_DIR}/${name}*.pid（进程自写的 --pid_file），无法定位本次部署的 ${name}" >&2
      return 1
    }
    pid=$(cat "$pidfile")
    # kill -0 失败≠进程已退出：无 /proc/<pid> 条目才算退出（清理 pidfile）；
    # 进程存在但无信号权限（EPERM）时无法确认退出，保留 pidfile 并中止。
    if ! kill -0 "$pid" 2>/dev/null; then
      if [ ! -d "/proc/${pid}" ]; then
        rm -f "$pidfile"; continue
      fi
      echo "ERROR: PID ${pid}（${pidfile}）存在但无信号权限，无法确认退出，中止——请核对运行身份后重试" >&2
      return 1
    fi
    exe=$(readlink -f "/proc/${pid}/exe" 2>/dev/null || true)
    case "$exe" in
      "${BUILD_DIR}/bin/${name}") : ;;
      *) echo "ERROR: ${pidfile} 中 PID ${pid} 的可执行文件(${exe:-未知})不属于本次部署，拒绝停止" >&2; return 1 ;;
    esac
    # 第二重核对：flagfile 必属本次 CONF_DIR（两套部署共用 BUILD_DIR 时仅凭 exe 不够）
    cmdline=$(tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null || true)
    case "$cmdline" in
      *"--flagfile=${CONF_DIR}/"*|*"--flagfile ${CONF_DIR}/"*) pids+=("$pid") ;;
      "") pids+=("$pid") ;;
      *) echo "ERROR: PID ${pid} 的 flagfile 不在本次 CONF_DIR(${CONF_DIR})——疑似另一实例的进程，拒绝停止" >&2; return 1 ;;
    esac
  done
  [ "${#pids[@]}" -gt 0 ] || return 0
  kill -TERM "${pids[@]}" 2>/dev/null || true
  while :; do
    alive=0
    for pid in "${pids[@]}"; do [ -d "/proc/${pid}" ] && alive=1; done   # 按 /proc 存在性判活，避免 kill -0 权限失败误判为已退出
    [ "$alive" -eq 0 ] && break
    if [ "$waited" -ge "$timeout" ]; then
      echo "ERROR: ${name} 未在 ${timeout}s 内退出，停止恢复" >&2
      return 1
    fi
    sleep 1
    waited=$((waited + 1))
  done
  rm -f "${PID_DIR}/${name}"*.pid
}
stop_component nebula-storaged "$STOP_TIMEOUT" || exit 1
stop_component nebula-graphd   "$STOP_TIMEOUT" || exit 1
stop_component nebula-metad    "$STOP_TIMEOUT" || exit 1

# 恢复前校验备份完整（存在且非空；空目录同样无效）；缺一个即中止，
# 避免删了旧数据却没有可用副本
[ -d "${BK}/meta" ] && [ -n "$(ls -A "${BK}/meta")" ] || {
  echo "ERROR: 备份缺少 meta 或为空" >&2; exit 1;
}
for i in $(seq 1 "$STORAGE_INSTANCES"); do
  [ -d "${BK}/storage${i}" ] && [ -n "$(ls -A "${BK}/storage${i}")" ] || {
    echo "ERROR: 备份缺少 storage${i} 或为空（STORAGE_INSTANCES=${STORAGE_INSTANCES}）" >&2
    exit 1
  }
done

# 原数据整体改名保留（原子操作），恢复验收通过后才清理；不要 rm -rf 原目录——
# 拷贝中途失败时还能原样退回，避免“复制失败、原数据已删”。
# mv 前先预检源目录（与备份段的源校验对称），缺失即停，避免“挪了一半”的中间态
[ -d "${DATA_ROOT}/meta" ] || { echo "ERROR: 缺少源目录 ${DATA_ROOT}/meta" >&2; exit 1; }
for i in $(seq 1 "$STORAGE_INSTANCES"); do
  [ -d "${DATA_ROOT}/storage${i}" ] || {
    echo "ERROR: 缺少源目录 ${DATA_ROOT}/storage${i}（STORAGE_INSTANCES=${STORAGE_INSTANCES} 与实际不符？）" >&2
    exit 1
  }
done
TS=$(date +%Y%m%d_%H%M%S)
OLD="${DATA_ROOT}/.pre_restore_${TS}_$$"
mkdir -p "$OLD"
mv "${DATA_ROOT}/meta" "${OLD}/meta" || {
  echo "ERROR: mv meta 失败；已挪动的目录保留在 ${OLD}，可从那里原样退回" >&2; exit 1;
}
for i in $(seq 1 "$STORAGE_INSTANCES"); do
  mv "${DATA_ROOT}/storage${i}" "${OLD}/storage${i}" || {
    echo "ERROR: mv storage${i} 失败；已挪动的目录保留在 ${OLD}，可从那里原样退回" >&2; exit 1;
  }
done

# 成对恢复（同一份备份）。失败进入回退流程：保持停服，先把本轮已恢复/半恢复的目录
# 整体移到独立隔离目录，再逐项把 ${OLD} 中的原目录改回原名并核对布局——绝不能把
# 原目录直接 mv 回仍存在的目标位置（mv 进入已存在目录会产生
# ${DATA_ROOT}/storage2/storage2 嵌套，破坏布局），也不要丢掉半恢复现场。
FAILDIR="${DATA_ROOT}/.failed_restore_${TS}_$$"
restore_rollback() {
  echo "ERROR: 恢复失败，进入回退（保持停服）：半恢复目录移至 ${FAILDIR}，原数据从 ${OLD} 逐项改回原名" >&2
  mkdir -p "${FAILDIR}" || { echo "ERROR: 无法创建隔离目录 ${FAILDIR}，请手工回退（原数据在 ${OLD}）" >&2; exit 1; }
  for d in meta $(seq 1 "$STORAGE_INSTANCES" | sed "s/^/storage/"); do
    [ -e "${DATA_ROOT}/${d}" ] && mv "${DATA_ROOT}/${d}" "${FAILDIR}/${d}"
  done
  for d in meta $(seq 1 "$STORAGE_INSTANCES" | sed "s/^/storage/"); do
    mv "${OLD}/${d}" "${DATA_ROOT}/${d}" || \
      echo "ERROR: 回退 mv ${d} 失败：原数据仍在 ${OLD}/${d}，请手工核对后恢复" >&2
  done
  for d in meta $(seq 1 "$STORAGE_INSTANCES" | sed "s/^/storage/"); do
    { [ -d "${DATA_ROOT}/${d}" ] && [ -n "$(ls -A "${DATA_ROOT}/${d}")" ]; } || \
      echo "ERROR: 回退后布局不完整：${DATA_ROOT}/${d} 缺失或为空（原数据见 ${OLD}，半恢复现场见 ${FAILDIR}）" >&2
  done
  echo "回退完成：半恢复现场保留在 ${FAILDIR}（确认无误后可删除）；保持停服，排除故障后重试" >&2
  exit 1
}
# 恢复副本先写入目标文件系统的独立暂存目录（.restore_stage_*，与 DATA_ROOT 同盘、
# 后续 mv 为原子 rename），整套复制+校验通过后才切换到正式路径——直接往正式路径
# 复制时，若 storage2 中途写满，正式路径上只剩部分恢复数据、必须额外回退。
STAGE="${DATA_ROOT}/.restore_stage_${TS}_$$"
mkdir "$STAGE" || { echo "ERROR: 无法创建恢复暂存目录 ${STAGE}（已存在/写满？）" >&2; exit 3; }
cp -a --reflink=never "${BK}/meta" "${STAGE}/meta" || { rm -rf "$STAGE"; restore_rollback; }
for i in $(seq 1 "$STORAGE_INSTANCES"); do
  cp -a --reflink=never "${BK}/storage${i}" "${STAGE}/storage${i}" || { rm -rf "$STAGE"; restore_rollback; }
done
# 暂存副本校验：整套存在且非空，缺一个即废弃暂存并回退（此时正式路径未被触碰）
[ -d "${STAGE}/meta" ] && [ -n "$(ls -A "${STAGE}/meta")" ] || { rm -rf "$STAGE"; restore_rollback; }
for i in $(seq 1 "$STORAGE_INSTANCES"); do
  [ -d "${STAGE}/storage${i}" ] && [ -n "$(ls -A "${STAGE}/storage${i}")" ] || { rm -rf "$STAGE"; restore_rollback; }
done
# 校验通过，原子切换：暂存目录逐项 rename 到正式路径（同文件系统）
mv "${STAGE}/meta" "${DATA_ROOT}/meta" || { rm -rf "$STAGE"; restore_rollback; }
for i in $(seq 1 "$STORAGE_INSTANCES"); do
  mv "${STAGE}/storage${i}" "${DATA_ROOT}/storage${i}" || { rm -rf "$STAGE"; restore_rollback; }
done
rmdir "$STAGE" 2>/dev/null || true

# 关键：删除集群身份文件
rm -f "${NEBULA_HOME}/cluster.id"

# 按顺序启动并验收
cd "${NEBULA_HOME}"
# metad → graphd → storaged
```

恢复后检查：

```ngql
SHOW HOSTS;      -- 全部 ONLINE，分区数正确
SHOW SPACES;     -- Space 列表与备份前一致
USE ldbc_bench; MATCH (n:Person) RETURN count(n);
```

- 上述验收通过后，才清理原数据保留目录：`rm -rf "${OLD}"`；验收未通过时用
  `${OLD}` 中的目录原样退回（停服 → mv 回原路径 → 启动），不要提前删除。

常见错误：

| 现象 | 原因 | 处置 |
|------|------|------|
| `Machine not existed!` | storaged 机器未在 metad 注册（ADD HOSTS 未执行/元数据被清） | 检查并重新执行 `ADD HOSTS`（与 `cluster.id` 无关，rm 了也没用） |
| `Reject wrong cluster host` | `cluster.id` 不匹配/meta 与 storage 不来自同一份备份 | `rm -f cluster.id`；从同一份备份成对恢复 |
| 部分 Space 缺失 | 只恢复了 storage，或 meta 来自另一份备份 | 用成对备份重做恢复；核对 `SHOW SPACES` 与备份清单 |
| 分区 OFFLINE | 备份时未停写/未成对，或端口/地址变化 |
| 启动崩溃 | 二进制 RocksDB 版本低于数据版本（不向后兼容） |

## 8. 重置与回归测试

每次性能对比前建议从同一份干净备份恢复，避免上一轮写放大/compaction 影响：

```bash
# 停 → 清 → 恢复 → 删 cluster.id → 启动 → 校验
# 若团队有重置脚本则调用；否则按 §7 手工执行
```

重置后至少确认：

- `SHOW HOSTS` 全部 ONLINE；
- 关键 Tag/Edge 计数与基线一致；
- 目标查询返回非空且属性正确；
- 预热一轮（读场景）再开始正式测量。

## 9. 版本兼容与升级

- RocksDB 只向后兼容：新版本写的数据，旧版本二进制可能打不开；升级前必须备份。
- 升级后如果数据格式不兼容，需要重新导入或使用官方升级工具/流程。
- 切换优化/原生二进制时，若编译参数不同但数据格式一致，可直接复用数据；
  仍然建议先重置到干净备份再对比，避免上一轮残留影响。

## 10. 输出记录模板

| 字段 | 内容 |
|------|------|
| 数据集/版本 | 如 LDBC SF10 v0.3.3 |
| CSV | 路径、是否去表头、行数 |
| Space | 名称、VID 类型、分区数、副本数 |
| 导入 | importer 版本、并发、耗时、成功/失败数 |
| 对账 | 每个 Tag/Edge 期望 vs 实际 |
| 备份 | 路径、大小、是否停服、bin md5/commit |
| 恢复 | 恢复耗时、`SHOW HOSTS`/Space/计数结果 |
