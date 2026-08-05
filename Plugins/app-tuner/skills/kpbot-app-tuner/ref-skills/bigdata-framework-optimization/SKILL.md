---
name: bigdata-framework-optimization
description: 提供 Spark、Flink 等大数据框架的参数推荐，支持自动检测容器/物理机环境并应用推荐参数。被 application-config-optimization 按需引用。
---

# Big Data Framework Optimization

> **本文件是底层能力定义，不应被主流程或外部工具直接引用。** 上层调用应通过 `skills/kpbot-app-tuner/subskills/application-config-optimization/SKILL.md`（统一入口适配层）进行，该适配层负责决定是否委派给本文件以及如何回退。

当检测到工作负载为 Spark 或 Flink 等大数据框架时，使用本 skill 提供的参数推荐。

## 识别条件

当满足以下条件时，应调用本 skill：

- `workload_type` 包含 `spark` 或 `flink`
- 或应用名称/进程名匹配 `spark`、`flink`、`spark-submit`、`flink run`
- 或检测到相关配置文件（如 `spark-defaults.conf`、`flink-conf.yaml`）
- 或检测到 Flink/Spark 容器或进程

## 环境自动检测

本 skill 支持**自动检测运行环境**并智能计算推荐参数。

### 检测规则

| 识别方式 | 物理机 | 容器 |
|---------|--------|------|
| `/proc/1/cgroup` | 无 docker/containerd | 包含 docker 或 containerd |
| Docker inspect | 无对应容器 | 存在对应容器名 |
| NanoCpus / CpuQuota | 无限制 | 有明确限制（如 NanoCpus=8000000000 表示 8 核） |
| cpuset | 无限制或很大 | 有明确范围（如 0-31），**仅为 CPU 亲和性，不代表核数** |
| memory limit | 无限制 | 有明确限制（如 34359738368 = 32 GiB） |

> **关键**：容器 CPU 核数以 `NanoCpus`（或 `CpuQuota/CpuPeriod`）为准，**不是 cpuset 范围**。cpuset 只定义允许运行在哪些核上，实际配额由 NanoCpus 决定。

### 自动检测流程

```
1. 检测目标是否为容器（通过 Docker API 或 cgroup）
2. 获取 CPU 核心数：容器场景优先用 `docker inspect --format '{{.HostConfig.NanoCpus}}'`（除以 1e9 得核数），其次 CpuQuota/CpuPeriod；物理机用 nproc
3. 获取内存大小：容器场景用 `docker inspect --format '{{.HostConfig.Memory}}'`；物理机用 `/proc/meminfo`
4. 获取容器内 TaskManager 进程数（`docker exec <tm-container> ps aux | grep -c TaskManagerRunner`）
5. 根据环境类型计算推荐参数
```

### 必需输入

| 输入字段 | 类型 | 说明 |
|---------|------|------|
| `target` | string | 目标容器名、主机或进程标识 |
| `workload_type` | string | spark 或 flink（默认自动检测） |
| `deploy_mode` | string | docker 或 ssh（默认 docker） |

### 可选输入

| 输入字段 | 类型 | 说明 |
|---------|------|------|
| `flink_home` | string | Flink 安装路径（默认 /usr/local/flink） |
| `spark_home` | string | Spark 安装路径（默认 /usr/local/spark） |
| `manual_parallelism` | integer | 手动指定 parallelism（覆盖自动计算） |
| `manual_task_slots` | integer | 手动指定 task slots（覆盖自动计算） |
| `restart_after_apply` | boolean | 应用后是否重启（默认 false） |

## Spark 参数推荐（ARM）

| 参数 | 社区默认值 | 推荐值 |
|------|-----------|--------|
| spark.driver.memory | 1g | 8g |
| spark.executor.instances | 2 | 整机：24 <br/> 64U 容器：12 |
| spark.executor.cores | 1 | = 容器核数 / instances |
| spark.executor.memory | 1g | = (容器内存 × 95% - driver memory) / instances |
| spark.sql.autoBroadcastJoinThreshold | 10m | 100m |
| spark.sql.shuffle.partitions | 200 | 600 |
| spark.sql.optimizer.runtime.bloomFilter.applicationSideScanSizeThreshold | 10GB | 0 |
| spark.sql.sources.parallelPartitionDiscovery.parallelism | 表分区数 | 60 |
| spark.executor.extraJavaOptions | none | -XX:+UseG1GC <br/> -XX:ParallelGCThread=4 <br/> -XX:MetaspaceSize=256m <br/> -XX:+UseBiasedLocking（JDK<15） |

**参数说明：**

- **spark.driver.memory**：Driver 端内存，主要用于任务调度、元数据管理、结果收集等；输出结果较多时，可以再适当增加此内存
- **spark.executor.instances**：Executor 数量，运行 Spark 任务（Task），执行具体的计算逻辑，存储缓存数据和中间计算结果，每个都是独立的 JVM 进程
- **spark.executor.cores**：Executor 核数，每个 executor 可以使用的核心数，每个核心可以处理 1 个 task
- **spark.executor.memory**：每个 executor 可以使用的内存
- **spark.sql.autoBroadcastJoinThreshold**：大小表 join 时，小表的最大阈值
- **spark.sql.shuffle.partitions**：默认 shuffle 分区数，提高分区数可以减少 GC 和数据倾斜
- **spark.sql.optimizer.runtime.bloomFilter.applicationSideScanSizeThreshold**：触发 bloomfilter 阈值，设置为 0，可以在一些小表查询时使能 bloomfilter
- **spark.sql.sources.parallelPartitionDiscovery.parallelism**：扫描表时的任务并行度，表分区较多时，降低此并行度，可以减少小任务数量
- **spark.executor.extraJavaOptions**：JVM 参数设置，合理设置 GC 等参数，可以减少 GC 时间

## Flink 参数推荐（ARM）

### 计算公式

参数分两级计算：

```
第一级：parallelism.default = cores / 2 （8U小规格(≤8核)时：parallelism.default = cores）
第二级：taskmanager.numberOfTaskSlots = parallelism.default / TM容器数 / 容器内TaskManager进程数
```

- `cores` = **所有 TM 容器的 CPU 核数之和**（容器场景取 NanoCpus/1e9，物理机取 nproc）
- `TM容器数` = 运行 TaskManager 的容器数量（不包含 JobManager 容器）
- `容器内TaskManager进程数` = 单个容器内运行的 TaskManagerRunner 进程数

即 slots 在 parallelism 基础上按 TM 容器数和容器内 TM 进程数均分。

### taskmanager.memory.process.size

```
容器场景：taskmanager.memory.process.size = 容器内存 / 该容器内 TM 进程数
物理机场景：taskmanager.memory.process.size = 机器总内存 / TM 进程总数
```

> 容器场景按**单个容器的内存**除以该容器内的 TM 进程数，不同容器可独立计算。

### JobManager 容器

| 参数 | 推荐值 | 说明 |
|------|--------|------|
| `pipeline.object-reuse` | 开 | 通用参数，高吞吐推荐 |
| `table.exec.mini-batch.enabled` | 开 | 通用参数，高吞吐推荐 |

### TaskManager 容器

| 参数 | 计算公式 | 推荐值 | 说明 |
|------|----------|--------|------|
| `taskmanager.numberOfTaskSlots` | parallelism.default / TM容器数 / 容器内TM进程数 | **由公式计算** | 示例：8 / 2 / 4 = 1 |
| `taskmanager.memory.process.size` | 容器内存 / 容器内TM进程数 | **由公式计算** | 示例：32 GiB / 4 = 8192m |
| `pipeline.object-reuse` | - | 内存状态后端：**true**；RocksDB：**false** | 内存后端开启减少GC；RocksDB开启可能导致状态不一致 |
| `table.exec.mini-batch.enabled` | - | **true** | 消息攒批，增加吞吐(劣化时延) |
| `table.exec.mini-batch.allow-latency` | - | **2s** | mini-batch 必须项：等待时间 |
| `table.exec.mini-batch.size` | - | **50000** | mini-batch 必须项：缓存条数 |

> **mini-batch 必须同时设置 `allow-latency` 和 `size`，否则无法生效。** 这两个参数是 mini-batch 功能的必要条件。

**注意**：
- JM 容器不运行 TaskManager 进程，不受 `taskmanager.*` 参数影响
- TM 容器不运行 JobManager 进程，不受 `jobmanager.*` 参数影响
- `taskmanager.numberOfTaskSlots` 由公式计算得出
- `pipeline.object-reuse` 需根据状态后端类型选择，脚本支持 `--state-backend` 参数自动判断

## 适用条件

| 组件 | 平台 | 关键约束 |
|------|------|---------|
| Spark | ARM | executor cores 需根据容器核数计算；内存需考虑 driver 预留 |
| Flink | ARM | TM 内存按容器内TaskManager进程数均分；RocksDB 状态后端建议关闭 object-reuse |

## 扩展性

可扩展支持更多大数据组件：

- Kafka（JVM、线程、内存参数）
- Hive（MapJoin、Shuffle 参数）
- Trino/Presto（Query 内存、并发参数）

扩展时，只需在识别逻辑中增加组件标识匹配，并增加对应参数表格。

## Benchmark 执行流程

执行 Nexmark 等 benchmark 测试前，必须进行完整的环境检查和清理，避免残留进程导致测试失败。

### Benchmark 执行前检查清单

```bash
# 1. 清理所有残留的 benchmark 进程
pkill -9 -f Benchmark
pkill -9 -f CpuMetricSender
pkill -9 -f CpuMetricReceiver
pkill -9 -f zdl.sh

# 2. 清理端口占用（9098 为 CpuMetricSender 默认端口）
lsof -i :9098 | grep -v COMMAND | awk '{print $2}' | xargs -r kill -9

# 3. 重启 Flink 集群清理僵尸 TaskManager 注册
stop-cluster.sh
start-cluster.sh

# 4. 验证集群状态
curl -s http://<jm-address>:8081/taskmanagers | python3 -c \
  'import sys,json; d=json.load(sys.stdin); tms=d["taskmanagers"]; print(f"TM: {len(tms)}, slots: {sum(t["slotsNumber"] for t in tms)}")'

# 5. 验证无残留进程
ps aux | grep -E 'Benchmark|zdl|CpuMetric' | grep -v grep || echo "Clean"
```

### 常见问题及解决方案

| 问题 | 原因 | 解决方案 |
|------|------|----------|
| `Address already in use (Bind failed)` on port 9098 | 旧 benchmark 进程未清理 | `kill -9 <pid>` 强制终止占用端口的进程 |
| `Could not acquire the minimum required resources` | Flink slot 被僵尸 job 占用 | 重启集群 `stop-cluster.sh && start-cluster.sh` |
| 显示 21 TMs 但实际只有 8 个 | metric 采集错误或僵尸注册 | 重启集群清理注册的 TM |
| CpuMetricSender 报 cores=0 | metric 收集失败 | 检查 9098 端口是否被占用 |

## Spark 资源自动计算（YARN）

环境检测规则见上方[环境自动检测](#环境自动检测)。以下为 Spark on YARN 的资源计算公式。

### 自动计算规则

#### 物理机场景

```
executor_instances = 24  # 固定值
executor_cores = nodemanager_vcores / executor_instances
executor_memory_mb = (nodemanager_memory_mb * 0.95 - driver_memory_mb) / executor_instances
driver_memory_mb = min(8192, nodemanager_memory_mb * 0.05)
```

#### 容器场景

```
# 64U 容器
executor_instances = 12
# 小规格容器（< 64U）
executor_instances = max(2, nodemanager_vcores / 4)

executor_cores = nodemanager_vcores / executor_instances
executor_memory_mb = (nodemanager_memory_mb * 0.95 - driver_memory_mb) / executor_instances
driver_memory_mb = min(8192, nodemanager_memory_mb * 0.05)
```

### YARN on Spark 资源规划原则

1. **资源预留**：NodeManager 预留 5% 内存给系统和其他进程
2. **Executor 规划**：总 vcore 和 memory 至少能容纳 2 个 executor
3. **Driver 规划**：集群模式预留 4-8GB 或总内存 5%
4. **instances 选择**：
   - 物理机：固定 24
   - 64U 容器：固定 12
   - 其他容器：根据 vcore 计算
5. **Cores 计算**：executor_cores = nodemanager_vcores / executor_instances

### 示例计算

**物理机（128核/502GB）**：
```
executor_instances = 24
executor_cores = 128 / 24 ≈ 5
executor_memory_mb = (502000 * 0.95 - 8000) / 24 ≈ 19500 MB ≈ 19g
```

**容器（65vcore/236GB）**：
```
executor_instances = 12
executor_cores = 65 / 12 ≈ 5
executor_memory_mb = (236000 * 0.95 - 8000) / 12 ≈ 18000 MB ≈ 18g
```

## 输出示例

### Spark 输出格式

输出必须为三列对比表（原值 / skill 推荐值 / 修改后），**不输出 JSON**。格式：

```
| 参数 | 原值 | 推荐值 | 修改后 |
|------|------|--------|--------|
| spark.driver.memory | 1g | 8g | 8g |
| spark.executor.instances | 2 | 12 | 12 |
| spark.executor.cores | 1 | 5 | 5 |
| ...
```

### Flink 输出格式

输出必须包含以下三个部分，**不输出 JSON**：

**1. 公式计算过程**

```
parallelism.default         = cores / 2 = 16 / 2 = 8
taskmanager.numberOfTaskSlots = parallelism.default / TM容器数 / 容器内TM进程数 = 8 / 2 / 4 = 1
taskmanager.memory.process.size = 容器内存 / 容器内TM进程数 = 32 GiB / 4 = 8192m
```

**2. 每个容器的三列对比表**（原值 / skill 推荐值 / 修改后，公式不涉及的参数推荐值标注 `—`）

格式：
```
### flink_JM
| 参数 | 原值 | 推荐值 | 修改后 | formula |
|------|------|--------|--------|---------|
| parallelism.default | 8 | 8 (16/2) | 8 | 是 |
| pipeline.object-reuse | true | true | true | 否 |
...

### flink_TM1
| 参数 | 原值 | 推荐值 | 修改后 | formula |
|------|------|--------|--------|---------|
| taskmanager.numberOfTaskSlots | 1 | 1 (8/2/4) | 1 | 是 |
| taskmanager.memory.process.size | 8192m | 8192m (32G/4) | 8192m | 是 |
| pipeline.object-reuse | true | true | true | 否 |
| table.exec.mini-batch.enabled | true | true | true | 否 |
| table.exec.mini-batch.allow-latency | 缺失 | 2s | 2s | 否 |
| table.exec.mini-batch.size | 缺失 | 50000 | 50000 | 否 |
...
```

**3. 变更汇总**（仅列出有改动的参数）

## 配置应用 (apply_config)

本 skill 支持将推荐配置**自动应用到目标 Spark 环境**，支持自动检测、公式计算、对比、重启验证。

### 输入参数

| 参数 | 类型 | 说明 |
|------|------|------|
| `--target` | string | 目标容器名或主机（如 `server2-spark`） |
| `--apply-all` | flag | **推荐** 自动检测 Spark 容器并批量应用配置 |
| `--spark-home` | string | Spark 安装路径（默认 `/usr/local/spark`） |
| `--config-file` | string | 配置文件名（默认 `spark-defaults.conf`） |
| `--deploy-mode` | string | 部署方式：`docker`（默认）或 `ssh` |
| `--spark-mode` | string | Spark 模式：`yarn`/`standalone`/`auto`（默认 auto，自动检测） |
| `--detect-only` | flag | 仅检测环境，不应用配置 |
| `--dry-run` | flag | 仅输出命令，不执行 |
| `--restart` | flag | 应用后重启 Spark 集群（standalone 模式） |
| `--driver-memory` | string | 手动指定 driver 内存（可选，覆盖自动计算） |
| `--executor-instances` | integer | 手动指定 executor 数量（可选，覆盖自动计算） |
| `--executor-cores` | integer | 手动指定 executor 核数（可选，覆盖自动计算） |
| `--executor-memory` | string | 手动指定 executor 内存（可选，覆盖自动计算） |
| `--no-compare` | flag | 跳过当前配置 vs 推荐配置对比 |
| `--compare-only` | flag | 仅对比当前配置与推荐配置，不应用（隐含 --dry-run） |

### 自动检测流程

```
1. 自动发现 Spark 容器（按名称/端口匹配 master/worker）
2. 检测 Spark 模式：standalone 或 YARN
3. 获取各容器 CPU 核数（NanoCpus 优先）和内存
4. 汇总集群总 vcores 和总内存
5. 计算推荐参数：
   - spark.driver.memory = 8g（固定推荐值）
   - spark.executor.instances = 物理机 24 / 64U容器 12 / 小规格 max(2, vcores/4)
   - spark.executor.cores = 总 vcores / instances
   - spark.executor.memory = (总内存 × 0.95 - driver内存) / instances
6. 生成 spark-defaults.conf 配置
7. 对比当前配置与推荐配置（逐参数差异表）
8. 备份原配置并应用新配置
```

### 使用方式

```bash
# 【推荐】一键批量配置：自动检测 Spark 容器并应用
scripts/apply_spark_config.sh --apply-all --restart

# 预览将要执行的变更（不实际修改）
scripts/apply_spark_config.sh --apply-all --dry-run

# 单容器模式
scripts/apply_spark_config.sh --target server2-spark
scripts/apply_spark_config.sh --target server2-spark --detect-only

# 手动覆盖参数
scripts/apply_spark_config.sh --apply-all --driver-memory 10g --executor-instances 16

# 仅对比不应用
scripts/apply_spark_config.sh --apply-all --compare-only
```

### 应用后的 spark-submit 命令示例

基于自动检测的推荐参数，生成的 spark-submit 命令：

```bash
spark-submit \
  --master yarn \
  --deploy-mode cluster \
  --driver-memory 8g \
  --executor-memory 18g \
  --executor-cores 5 \
  --num-executors 12 \
  --conf spark.sql.autoBroadcastJoinThreshold=100m \
  --conf spark.sql.shuffle.partitions=600 \
  --conf spark.sql.optimizer.runtime.bloomFilter.applicationSideScanSizeThreshold=0 \
  --conf spark.sql.sources.parallelPartitionDiscovery.parallelism=60 \
  --conf spark.executor.extraJavaOptions="-XX:+UseG1GC -XX:ParallelGCThread=4 -XX:MetaspaceSize=256m -XX:+UseBiasedLocking" \
  your_app.jar
```

> 将以上参数写入 `spark-defaults.conf` 后，`spark-submit` 无需重复指定这些参数。使用 `--apply-all` 脚本自动完成写入。}

## Flink 配置应用 (apply_config_flink)

本 skill 支持将推荐配置**自动应用到目标 Flink 环境**，支持**容器和物理机**两种场景。

### 输入参数

| 参数 | 类型 | 说明 |
|------|------|------|
| `--target` | string | 目标容器名或主机（如 `flink_JM`） |
| `--apply-all` | flag | **推荐** 自动检测 JM + 所有 TM 容器并批量应用配置 |
| `--flink-home` | string | Flink 安装路径（默认 `/usr/local/flink`） |
| `--config-file` | string | 配置文件名（默认 `flink-conf.yaml`） |
| `--deploy-mode` | string | 部署方式：`docker`（默认）或 `ssh` |
| `--detect-only` | flag | 仅检测环境，不应用配置 |
| `--dry-run` | flag | 仅输出命令，不执行 |
| `--restart` | flag | 应用后重启 Flink 集群 |
| `--parallelism` | integer | 手动指定 parallelism（可选，自动计算） |
| `--task-slots` | integer | 手动指定 task slots（可选，自动计算） |
| `--tm-per-container` | integer | 手动指定每容器 TM 进程数（覆盖自动检测） |
| `--object-reuse` | string | object-reuse: true/false/auto（默认 auto，根据 state-backend 决定） |
| `--mini-batch` | string | mini-batch: true/false/auto（默认 auto） |
| `--state-backend` | string | 状态后端: memory/rocksdb（默认 auto=memory，影响 object-reuse 默认值） |
| `--no-compare` | flag | 跳过当前配置 vs 推荐配置对比（默认开启对比） |
| `--compare-only` | flag | 仅对比当前配置与推荐配置，不应用（隐含 --dry-run） |

### 自动检测流程

```
1. 检测目标是否为容器（Docker inspect 或 cgroup）
2. 获取 CPU 核心数（容器场景优先用 NanoCpus/1e9，物理机用 nproc；不要用 cpuset 范围）
3. 获取内存大小（容器场景用 docker inspect Memory 字段，物理机用 /proc/meminfo）
4. 获取 TM 容器数及各容器内 TaskManager 进程数（docker exec <tm-container> ps aux | grep -c TaskManagerRunner）
5. 计算推荐参数：
   - parallelism.default = 所有TM容器总核数 / 2（8U小规格≤8核时不除以2）
   - taskmanager.numberOfTaskSlots = parallelism.default / TM容器数 / 容器内TM进程数
   - taskmanager.memory.process.size = 容器内存 / 该容器内TM进程数（容器场景）
6. 生成 flink-conf.yaml 配置
7. **对比当前配置与推荐配置**（默认开启），逐容器展示差异表
8. 备份原配置并应用新配置
```

### 使用方式

```bash
# 【推荐】一键批量配置：自动检测 JM + 所有 TM 容器并应用
scripts/apply_flink_config.sh --apply-all --restart

# 预览将要执行的变更（不实际修改）
scripts/apply_flink_config.sh --apply-all --dry-run

# 单容器模式：仅配置特定容器
scripts/apply_flink_config.sh --target flink_JM
scripts/apply_flink_config.sh --target flink_JM --detect-only
scripts/apply_flink_config.sh --target flink_JM --dry-run

# 手动指定参数覆盖自动计算
scripts/apply_flink_config.sh --apply-all --parallelism 16 --task-slots 8

# 指定每容器 TM 进程数（不自动检测）
scripts/apply_flink_config.sh --apply-all --tm-per-container 2

# SSH 模式（物理机）
scripts/apply_flink_config.sh --target root@flink-server --deploy-mode ssh --restart
```

### 容器场景示例

当前检测到容器配置：
- flink_JM: 8 核 (NanoCpus=8000000000), 内存 32 GiB
- flink_TM1: 8 核 (NanoCpus=8000000000), 内存 32 GiB, 容器内 4 个 TM 进程
- flink_TM2: 8 核 (NanoCpus=8000000000), 内存 32 GiB, 容器内 4 个 TM 进程
- TM 容器数: 2, 总 TM 核数: 16

自动计算推荐参数：
| 参数 | 计算公式 | 推荐值 |
|------|----------|--------|
| parallelism.default | 16 / 2 | **8** |
| taskmanager.numberOfTaskSlots | 8 / 2 / 4 | **1** |
| taskmanager.memory.process.size | 32 GiB / 4 | **8192m** |

### 物理机场景示例

对于物理机（64核/256GB，运行 2 个 TaskManager 进程）：
| 参数 | 计算公式 | 推荐值 |
|------|----------|--------|
| parallelism.default | 64 / 2 | **32** |
| taskmanager.numberOfTaskSlots | 32 / 2 | **16** |
| taskmanager.memory.process.size | 256 GiB / 2 | **128g** |

> 物理机场景：`taskmanager.numberOfTaskSlots = parallelism.default / TM进程总数`，无需容器数和容器内进程数的中间层。`taskmanager.memory.process.size = 机器总内存 / TM进程总数`。

### 生成配置示例

```yaml
# Flink 推荐配置 (TM 容器)
# 自动生成，基于: 8核/32GiB 容器, 4 TM进程

# 并行度设置 (JM 容器配置)
parallelism.default: 8

# TaskManager 设置
taskmanager.numberOfTaskSlots: 1
taskmanager.memory.process.size: 8192m

# 对象复用（内存状态后端推荐开，RocksDB状态后端建议关）
pipeline.object-reuse: true

# Mini-batch 攒批（增加吞吐，劣化时延）
table.exec.mini-batch.enabled: true
table.exec.mini-batch.allow-latency: 2s
table.exec.mini-batch.size: 50000
```

### AI Agent 使用指南

当用户请求 Flink 或 Spark 参数优化时，AI Agent 应按以下步骤操作：

**Flink:**
```
1. 先运行 --apply-all --dry-run 获取环境检测结果和推荐参数
   脚本会自动拉取各容器当前 flink-conf.yaml 的值，与推荐值进行对比
2. 向用户展示完整对比结果（必须逐容器、逐参数展示，禁止仅输出汇总结论）
3. 用户确认后运行 --apply-all --restart 应用并重启
```

**Spark:**
```
1. 先运行 --apply-all --dry-run 获取环境检测结果和推荐参数
   脚本会自动拉取当前 spark-defaults.conf 的值，与推荐值进行对比
2. 向用户展示完整对比结果（9 个参数的当前值 vs 推荐值对比表）
3. 用户确认后运行 --apply-all --restart 应用并重启（YARN 模式无需重启）
```

#### 第 2 步强制输出要求

**必须原样展示脚本输出的以下内容，不得省略或仅给汇总：**

1. **环境检测结果** — 每个容器的 CPU、内存、TM 进程数
2. **公式计算过程** — parallelism、slots、memory 的计算推导
3. **逐容器参数对比表** — 每个容器一张表，列出全部参数及其 `当前值 | 推荐值 | 状态 | formula`：

```
### flink_JM (角色: jobmanager)

  参数                                        | 当前值    | 推荐值              | 是否一致 | formula
  ----------------------------------------------|--------------|------------------------|--------------|--------
  parallelism.default                           | 8            | 8                      | ✓ 一致   | 是
  pipeline.object-reuse                         | true         | true                   | ✓ 一致   | 否
  ...

### flink_TM1 (角色: taskmanager)

  参数                                        | 当前值    | 推荐值              | 是否一致 | formula
  ...
```

**关键约束**：
- 对比表由脚本内置的 `show_comparison_table()` 自动生成，AI Agent 只需**完整转发**，禁止只给"全部一致"之类的结论
- **即使所有参数都是 ✓ 一致，也必须逐容器、逐参数展示完整表格**
- 差异行标注 ✗ 差异，一致行标注 ✓ 一致，缺失的参数显示"缺失"
- 对比表之后可以附加一句话汇总（如"N 容器 × M 参数 = X 项，全部 ✓ 一致"），但不能替代完整展示

> **对比展示是脚本内置能力**：`--apply-all` 默认开启当前配置 vs 推荐配置的对比（可通过 `--no-compare` 跳过）。
> 使用 `--compare-only` 可仅对比不应用，适合审计/检查场景。

**优化建议**：
- 如果容器内 TM 进程数 ≥ 2，建议用户考虑合并 TM 进程（减少进程数，增大每个 TM 的 slots 和内存）以减少 JVM 开销。可通过 `--tm-per-container` 参数指定合并后的进程数
- 8U 小规格（≤8核）容器自动使用 `parallelism.default = cores`（不除以 2），脚本已内置此逻辑
- 合并 TM 后需同步修改 TM 启动脚本（如 `start-tm-cluster.sh` 或 `start-multiple-tm.sh`）

### 配置应用后的验证

```bash
# 查看新配置
docker exec flink_JM cat /usr/local/flink/conf/flink-conf.yaml | grep -E "parallelism|taskmanager|object-reuse|mini-batch"

# 重启集群使配置生效 (--apply-all --restart 已自动包含此步骤)
docker exec flink_JM /usr/local/flink/bin/stop-cluster.sh
docker exec flink_JM /usr/local/flink/bin/start-cluster.sh

# 检查 Flink Web UI
curl http://localhost:8081
```

### 兼容性说明

- **容器场景**：自动检测 cpuset 和 memory limit，确保参数不超过容器限制
- **物理机场景**：自动使用全部资源，根据 CPU 和内存计算推荐值
- **混合部署**：支持 Docker 容器和 SSH 物理机两种部署模式
- **配置备份**：应用前自动备份原配置到 `.bak` 文件
