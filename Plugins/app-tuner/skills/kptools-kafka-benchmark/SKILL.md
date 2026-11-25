---
name: kptools-kafka-benchmark
description: 'Kafka 压测、Kafka 性能基准、producer 吞吐与延迟测量、消息正确性核对、 配置变更前后可复现多轮对比、已有 Kafka 服务合成负载测试时使用。 实时复查前置条件后运行独立预热和至少三轮有界官方 producer perf， 交付原生指标、offset/计数/全量消息身份校验、逐轮日志与 JSON；不写任意业务 topic。

  '
license: MulanPSL-2.0
metadata:
  compatibility: Linux/Bash，Python 3.10+；仅 Apache Kafka CLI 4.0.x/4.1.x 已实现参数面，Java 17+
  skill_type: tool_assisted
  category: benchmark
  domain: kafka
  side_effect_free: 'false'
  version: 1.0.0
---

# Kafka 有界 Producer 基准

## 范围与必需输入

需要现有 Kafka 发行包、现有可访问服务和新的输出目录。脚本内嵌全部实时
前置检查，不读取旧检查报告，也不依赖其他 skill。只使用 Python 标准库和发行包
官方 CLI，不安装客户端包、不部署或启停 broker。

二选一指定写入目标：

1. `--allow-create-topic`：明确授权创建一个带随机 UUID 的唯一 topic；默认
   成功后只删除这个已确认由本 run 创建的精确 topic。
2. `--topic <空专用 topic>`：使用已有、latest offset 总和为零的专用 topic；
   绝不删除它，正确性与压测消息会留在其中。

两种模式都用随机 consumer group 做三条消息精确收发；预热和每个计时轮
再各用一个新随机 group 核对全部历史消息身份，只清理这些已发起的精确 group。
不提供 `--allow-create-topic` 时绝不发 create。此工具会产生真实消息负载，
调用者必须已取得目标和清理授权。

## 执行

将示例中的 `../kafka` 替换为实际已安装的 Kafka 发行包目录，端点替换为获授权的现有 broker。以下新建唯一 topic 模式不需要预建数据或配置文件。认证集群另加 `--client-config` 指向管理员提供的现有安全配置；无认证实例省略该参数。

```bash
# 最小示例：现有 loopback 单节点 Kafka，显式授权创建唯一 topic
python3 scripts/run_benchmark.py \
  --kafka-dir ../kafka \
  --bootstrap 127.0.0.1:19092 \
  --allow-create-topic \
  --replication-factor 1 \
  --output results/bench-001

# 可选：仅当管理员已提供空专用 topic 时使用；不删除该 topic
python3 scripts/run_benchmark.py \
  --kafka-dir ../kafka \
  --bootstrap localhost:9092 \
  --topic kpbot-empty-dedicated \
  --records 100000 \
  --warmup-records 10000 \
  --rounds 3 \
  --output results/bench-002

# 可选：仅当管理员已提供真实安全配置时追加到相应命令
#   --client-config secure/client.properties
```

安全配置要求同当前用户所有的 `0600`/`0400` properties 文件，且只接受
transport/auth/连接超时白名单键。密码不能放 argv；文件内容不复制进结果，原生
stdout/stderr 也会脱敏。非默认端点直接用 `--bootstrap host:port[,host:port]`。

## 测量协议

1. 重新检查六个官方工具、精确 CLI/JDK 版本、真实 metadata 查询、有限负载和
   本地结果空间；失败则 `blocked`，尚未发写操作。
2. 既有 topic 必须为空；新建 topic 的 create 请求前先持久化随机名称与
   `rpc_outcome_unknown`。create 失败/超时记为 `ambiguous`，只 describe 核实，
   不盲目 delete。
3. console producer/consumer 验证三条不同消息的数量与完整内容；任何差异都
   fail closed，不启动预热。
4. 预热与计时分开。默认预热 10000 条，然后 3 个独立计时轮，每轮 100000 条
   `--payload-monotonic` 十进制身份、`acks=all`、单 producer、10000 records/s
   正限速；各阶段身份都从 0 开始，跨阶段重复身份按出现次数精确核对。
5. 每阶段保存原生最终汇总、进程 wall 时间和前后各分区 latest offset。
   原生 sent 数及 offset 增量都必须精确等于请求数。随后用新随机 group 从头
   消费当前全部消息，并与三条正确性消息及已完成各阶段的所有身份做多重集
   比较；缺消息、额外消息、错误身份或重复次数不符都失败，不能只看计数。
6. 默认 3 轮，允许 3–10；每子进程默认硬超时 120 秒。速率、条数、轮数均有
   有限边界，全部 perf 身份总量不超过 1000000，没有无限迭代、无限时间或
   `-1` 无限速入口。累计验证会重读此前阶段，结果空间按该上限检查；失败不自动重试。

指标定义、单位与聚合口径见 [metrics](references/metrics.md)，前置检查与
资源所有权见 [probes](references/probes.md)。

## 产物与退出码

- `result.json`：精确版本、完整计划、preflight、命令证据、正确性、预热、
  各计时轮退出码/原生指标/wall ms/offset/身份比较、仅计时轮聚合，以及独立的
  `workload_status`、`cleanup.status`、逐 group 清理结果和限制。
- `<step>.stdout.log` / `<step>.stderr.log`：每命令独立、脱敏的原生输出；
  失败结果和日志保留，输出目录已存在时退出 2 且不覆盖。
- `0`：全部轮次、正确性和清理通过；`1`：运行、超时、校验或清理失败；
  `2`：参数/前置检查/输出冲突导致未执行或停止。

`--keep-topic` 可保留已成功创建且明确拥有的 topic；失败/RPC 模糊状态会保留
证据并保守处理。清理失败会把总体 `status` 改为失败，但已完成的
`workload_status=passed` 与 `cleanup.status=failed` 分别保留，不互相覆盖。

## 回归

```bash
bash cases/contract/run.sh
```

tier-A 状态化假 CLI 覆盖成功、unsupported version、missing tool、auth、
timeout、运行失败、输出冲突、预热排除、精确内容/计数/offset、RPC 模糊创建和
清理失败。它是契约证据，不冒称真实 Kafka E2E。

## 官方依据

- [Apache Kafka 4.1 Quick Start](https://kafka.apache.org/41/getting-started/quickstart/)
- [ProducerPerformance 4.1.0 源码与单位实现](https://github.com/apache/kafka/blob/4.1.0/tools/src/main/java/org/apache/kafka/tools/ProducerPerformance.java)
- [Apache Kafka 4.1 Configuration](https://kafka.apache.org/41/configuration/)


## 镜像维护

`scripts/kafka_support.py` 在两个 Kafka skill 中有意保持逐字节镜像；修改任一副本时
必须同步另一副本，并由双侧契约在源码仓存在相邻 skill 时断言 SHA-256 一致。每个已安装
skill 仍自包含其副本，运行时不导入、不读取也不要求相邻 skill 存在。


CLI 子进程仅透传 Java、PATH、HOME、locale、临时目录及明确列出的 Kerberos 配置/缓存位置；不会继承其他凭据或 `KAFKA_OPTS` 等任意 JVM 注入设置。认证参数使用本技能校验的客户端配置文件。
