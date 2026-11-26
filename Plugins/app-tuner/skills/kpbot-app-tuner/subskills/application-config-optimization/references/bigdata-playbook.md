# Big Data Framework Optimization

> **本参考文档提供 Spark、Flink 等大数据框架的参数推荐和配置应用指引。**
>
> **脚本安全约束**：`scripts/` 下的脚本已改造为推荐器模式，只分析环境并输出候选命令 JSON（stdout），不直接执行任何修改操作。主框架读取 JSON 后通过安全门控执行 `commands_execute` 中的命令。Agent 调用脚本后，将输出的 JSON 作为 `candidate_actions` 提交主框架审核执行。

当检测到工作负载为 Spark 或 Flink 等大数据框架时，使用本参考文档提供的参数推荐。

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

## 配置推荐脚本

脚本已改造为推荐器模式：只分析环境并输出候选命令 JSON（stdout），不直接执行任何修改操作。主框架读取 JSON 后通过安全门控执行 `commands_execute` 中的命令。

### apply_spark_config.sh

| 参数 | 类型 | 说明 |
|------|------|------|
| `--target` | string | 目标容器名或主机 |
| `--apply-all` | flag | 自动检测 Spark 容器并批量分析 |
| `--spark-home` | string | Spark 安装路径（默认 `/usr/local/spark`） |
| `--config-file` | string | 配置文件名（默认 `spark-defaults.conf`） |
| `--deploy-mode` | string | 部署方式：`docker`（默认）或 `ssh` |
| `--spark-mode` | string | Spark 模式：`yarn`/`standalone`/`auto`（默认 auto） |
| `--driver-memory` | string | 手动指定 driver 内存（覆盖自动计算） |
| `--executor-instances` | integer | 手动指定 executor 数量（覆盖自动计算） |
| `--executor-cores` | integer | 手动指定 executor 核数（覆盖自动计算） |
| `--executor-memory` | string | 手动指定 executor 内存（覆盖自动计算） |

使用方式：

```bash
# 自动检测 Spark 容器并输出推荐命令 JSON
scripts/apply_spark_config.sh --apply-all

# 单容器模式
scripts/apply_spark_config.sh --target server2-spark

# 手动覆盖参数
scripts/apply_spark_config.sh --apply-all --driver-memory 10g --executor-instances 16
```

脚本输出 JSON 包含：环境检测结果、推荐参数、当前参数对比、配置内容、`commands_execute`（备份+写入）、`restart_commands`、`rollback`。Agent 将 JSON 作为 `candidate_actions` 提交主框架审核执行。

### apply_flink_config.sh

| 参数 | 类型 | 说明 |
|------|------|------|
| `--target` | string | 目标容器名 |
| `--apply-all` | flag | 自动检测 JM + 所有 TM 容器并批量分析 |
| `--flink-home` | string | Flink 安装路径（默认 `/usr/local/flink`） |
| `--config-file` | string | 配置文件名（默认 `flink-conf.yaml`） |
| `--parallelism` | integer | 手动指定 parallelism（覆盖自动计算） |
| `--task-slots` | integer | 手动指定 task slots（覆盖自动计算） |
| `--tm-per-container` | integer | 每容器 TM 进程数（覆盖自动检测） |
| `--object-reuse` | string | true/false/auto（默认 auto） |
| `--mini-batch` | string | true/false/auto（默认 auto） |
| `--state-backend` | string | memory/rocksdb（默认 auto=memory） |
| `--role` | string | jobmanager/taskmanager/auto（默认 auto） |

使用方式：

```bash
# 自动检测 JM + TM 容器并输出推荐命令 JSON
scripts/apply_flink_config.sh --apply-all

# 单容器模式
scripts/apply_flink_config.sh --target flink_JM

# 手动覆盖参数
scripts/apply_flink_config.sh --apply-all --parallelism 16 --task-slots 8
```

脚本输出 JSON 包含：环境检测结果（JM/TM 容器、CPU、内存、TM 进程数）、推荐参数（parallelism、slots、memory、object-reuse、mini-batch）、当前参数对比、每个容器的 `config_content` + `commands_execute` + `restart_commands` + `rollback`。

### 容器场景示例

检测到容器配置：
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

### 其他脚本

| 脚本 | 用途 | 输出 |
|------|------|------|
| `scripts/cleanup_benchmark_env.sh` | 清理 benchmark 残留进程和端口 | 候选命令 JSON（pkill/stop-cluster/start-cluster） |
| `scripts/run_tpcds_benchmark.sh` | 执行 TPC-DS SQL 并统计性能 | 候选命令 JSON（spark-sql 执行命令） |
| `scripts/start_tm.sh` | 启动指定数量的 TM 进程 | 候选命令 JSON（pkill + flink-daemon.sh start） |

> `start_tm.sh` 合并了原 `start-multiple-tm.sh` 和 `start-tm-cluster.sh`。

### AI Agent 使用指南

当用户请求 Flink 或 Spark 参数优化时，AI Agent 应按以下步骤操作：

1. 执行脚本获取推荐命令 JSON（脚本不执行任何修改）
2. 解析 JSON 中的 `environment`、`recommended_params`、`current_params` 展示对比
3. 将 `actions` 作为 `candidate_actions` 提交主框架安全门控
4. 门控通过后，主框架执行 `commands_execute` 中的命令
5. 执行后按 `validation` 验证，失败则执行 `rollback`

### 兼容性说明

- **容器场景**：自动检测 cpuset 和 memory limit，确保参数不超过容器限制
- **物理机场景**：自动使用全部资源，根据 CPU 和内存计算推荐值
- **混合部署**：Spark 支持 Docker 容器和 SSH 物理机两种部署模式；Flink 仅支持容器模式
- **配置备份**：`commands_execute` 中包含备份命令，`rollback` 中包含恢复命令
