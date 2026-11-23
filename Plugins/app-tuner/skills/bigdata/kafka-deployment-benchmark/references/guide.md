# Kafka 部署与消息基准

## 部署与版本

先确认 broker/client 版本、JDK、CPU 架构、节点、端口和磁盘。已有集群直接验证；新建隔离实例按 [官方 Quick Start](https://kafka.apache.org/41/getting-started/quickstart/) 选择目标版本。在 KRaft 模式按该版本文档生成 cluster ID 并初始化新的日志目录，随后启动 broker。存量数据目录不重复 format。ZooKeeper 模式按其版本文档先启动 ZooKeeper，再启动 broker，不混用两种配置。

listeners 是监听地址，advertised.listeners 必须能从实际客户端访问。保存 broker 配置、集群模式与节点 ID。下面命令在 Kafka 发行包根目录执行，bootstrap-server 替换成目标地址；认证配置通过对应工具的配置文件参数传入。

示例使用 Linux/Bash 与 Kafka 4.1.x CLI；该系列需要 Java 17+。Kafka 的 `_2.13` 表示 Scala 版本，不是 CPU 架构；JDK 需选择目标架构。可从 [Apache 版本归档](https://archive.apache.org/dist/kafka/4.1.0/) 获取固定例子及 .sha512：

```bash
curl -fL -o kafka.tgz https://archive.apache.org/dist/kafka/4.1.0/kafka_2.13-4.1.0.tgz
tar -tzf kafka.tgz
tar -xzf kafka.tgz
mv kafka_2.13-4.1.0 kafka
```

以上在新的实验父目录执行，目标 kafka 目录应尚不存在；解压后 kafka/bin 下才有下面的脚本。先从归档页下载同名 .sha512 文件并与本地 SHA-512 比对，通过后再运行。服务端选择其他版本时使用其发行包和同版本说明，不用源码压缩包代替二进制发行包。

## 完整消息验证

创建专用测试 topic，名称每轮唯一。分区与副本按实际 broker 数量设置，以下单分区单副本仅用于隔离 smoke test：

```bash
bin/kafka-topics.sh --bootstrap-server localhost:9092 --create --topic kpbot-smoke-001 --partitions 1 --replication-factor 1
bin/kafka-topics.sh --bootstrap-server localhost:9092 --describe --topic kpbot-smoke-001
printf 'kpbot-1\nkpbot-2\nkpbot-3\n' | bin/kafka-console-producer.sh --bootstrap-server localhost:9092 --topic kpbot-smoke-001
bin/kafka-console-consumer.sh --bootstrap-server localhost:9092 --topic kpbot-smoke-001 --from-beginning --max-messages 3 --timeout-ms 10000
```

检查 Leader 与 ISR，并确认消费内容恰为三条消息。自动入口 `scripts/check_service.py` 会创建唯一 topic、写入三条消息并验证消费内容；测试结束保留 topic 名与原始证据，用户需要时再清理该 topic，不删除共享资源。

## 有限生产压测

预先创建独立压测 topic 并记录分区、副本和 ISR，例如在该次实验中执行：

```bash
bin/kafka-topics.sh --bootstrap-server localhost:9092 --create --topic kpbot-bench-001 --partitions 1 --replication-factor 1
bin/kafka-topics.sh --bootstrap-server localhost:9092 --describe --topic kpbot-bench-001
```

topic 名需随实验更换；不要将副本数设置得超过可用 broker 数量。使用发行包自带的 [producer performance 工具](https://github.com/apache/kafka/blob/4.1.0/tools/src/main/java/org/apache/kafka/tools/ProducerPerformance.java)，在同版本工具帮助中核对参数：

```bash
mkdir -p results
bin/kafka-producer-perf-test.sh --topic kpbot-bench-001 --num-records 100000 --record-size 1024 --throughput 10000 --producer-props bootstrap.servers=localhost:9092 acks=all compression.type=none batch.size=16384 linger.ms=0 > results/round-1.log 2>&1
```

先完成少量消息 smoke，再预热并进行三轮，每轮独立输出文件。保留工具报告的实际发送成功数、错误、records/s、MB/s 与延迟，不能仅凭退出码判断投递完整。消费端另行记录测试前后各分区 offset 增量并核对消息数量。固定消息大小、分区/副本、acks、压缩、批量、生产者数量和限速；不同投递语义不作直接收益比较。

需要吞吐上限测试时按实验计划调整限速；先估算总数据量并检查磁盘。topic 清理只作用于本次创建的测试资源。

若自动检查脚本位于 skill 目录而 Kafka 解压在别处，`--kafka-dir` 传相对当前调用目录的发行包位置，例如 `../experiment/kafka`。脚本直接使用该目录的 bin，不要求安装到系统 PATH。Java、Bash 与网络需在执行检查脚本的同一环境可用。存在认证时用 `--client-config` 提供真实的连接配置文件，文件由集群连接信息生成，不是示例附件。

自动检查的 `--timeout` 是每个客户端进程的总等待上限（含 JVM 启动）；消费者的 `--timeout-ms` 同步换算为毫秒，最低为 1 毫秒。较慢的 broker 或 JVM 启动应增加该参数；任一步超时均保留日志并报告失败。上面的手工消费示例使用独立的 10 秒空闲等待。
