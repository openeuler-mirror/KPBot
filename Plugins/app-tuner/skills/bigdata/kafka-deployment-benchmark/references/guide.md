# Kafka 部署与消息基准

## 部署与版本

先确认 broker/client 版本、JDK、CPU 架构、节点、端口和磁盘。已有集群直接验证；新建隔离实例按 [官方 Quick Start](https://kafka.apache.org/41/getting-started/quickstart/) 选择目标版本。在 KRaft 模式按该版本文档生成 cluster ID 并初始化新的日志目录，随后启动 broker。存量数据目录不重复 format。Kafka 4.0 起已移除 ZooKeeper，本文 4.1.x 新建实例只使用 KRaft，参见[官方 4.0 升级说明](https://kafka.apache.org/40/getting-started/upgrade/)。存量 3.x ZooKeeper 集群须按其对应版本文档验证，不使用 4.x 发行包启动 ZooKeeper。

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

创建专用测试 topic，名称每轮唯一。分区按实际 broker 数量设置；副本默认继承 broker 的 `default.replication.factor`（省略 `--replication-factor`），仅单节点实验显式指定 `--replication-factor 1`。集群要求 `min.insync.replicas=2` 时，测试 topic 副本数不得小于该值，否则后续 `acks=all` 写入会失败：

```bash
# 多节点：省略副本数，继承 broker 默认
bin/kafka-topics.sh --bootstrap-server localhost:9092 --create --topic kpbot-smoke-001 --partitions 1
# 单节点实验：与上条二选一，显式指定 1
# bin/kafka-topics.sh --bootstrap-server localhost:9092 --create --topic kpbot-smoke-001 --partitions 1 --replication-factor 1
bin/kafka-topics.sh --bootstrap-server localhost:9092 --describe --topic kpbot-smoke-001
printf 'kpbot-1\nkpbot-2\nkpbot-3\n' | bin/kafka-console-producer.sh --bootstrap-server localhost:9092 --topic kpbot-smoke-001
bin/kafka-console-consumer.sh --bootstrap-server localhost:9092 --topic kpbot-smoke-001 --from-beginning --max-messages 3 --timeout-ms 10000
```

检查 Leader 与 ISR，并确认消费内容恰为三条消息。自动入口 `scripts/check_service.py` 会创建唯一 topic、写入三条消息并验证消费内容；测试结束保留 topic 名与原始证据，用户需要时再清理该 topic，不删除共享资源。脚本在发出创建请求前就把 topic 名和 `cleanup=topic creation result unknown; verify topic before cleanup` 写入 `result.json`；创建命令超时或非零退出时该状态保持不变，不能据此认定 topic 未创建，应先按记录的 topic 名用 `--describe` 核实，再决定清理。只有创建请求发出前的配置校验失败才报告 `no topic created`。

## 有限生产压测

预先创建独立压测 topic 并记录分区、副本和 ISR。topic 名每轮唯一；副本数同样默认继承 broker 默认，单节点实验显式 `--replication-factor 1`，多节点不得低于集群 `min.insync.replicas`：

```bash
bin/kafka-topics.sh --bootstrap-server localhost:9092 --create --topic kpbot-bench-001 --partitions 1
bin/kafka-topics.sh --bootstrap-server localhost:9092 --describe --topic kpbot-bench-001
```

topic 名需随实验更换；不要将副本数设置得超过可用 broker 数量。使用发行包自带的 [producer performance 工具](https://github.com/apache/kafka/blob/4.1.0/tools/src/main/java/org/apache/kafka/tools/ProducerPerformance.java)，在同版本工具帮助中核对参数。每次实验使用新的结果目录；目录已存在时 `mkdir` 直接失败，不覆盖也不追加旧日志。预热与每个测量轮次分别写入独立文件：

```bash
mkdir -p results
EXP=results/bench-001   # 每次实验换新编号
mkdir "$EXP" || { echo "结果目录 $EXP 已存在；换新编号后重试" >&2; exit 1; }

# 预热与测量共用同一执行函数；逐轮保存退出码，失败即停
run_round() {
    local name="$1"
    local records="$2"
    local rc=0
    bin/kafka-producer-perf-test.sh --topic kpbot-bench-001 --num-records "$records" --record-size 1024 --throughput 10000 --producer-props bootstrap.servers=localhost:9092 acks=all compression.type=none batch.size=16384 linger.ms=0 > "$EXP/$name.log" 2>&1 || rc=$?
    if ! printf '%s exit_code=%s\n' "$name" "$rc" >> "$EXP/exit-codes.log"; then
        printf '%s 的退出码写入 %s/exit-codes.log 失败，停止后续轮次\n' "$name" "$EXP" >&2
        return 1
    fi
    if [ "$rc" -ne 0 ]; then
        printf '%s 失败（退出码 %s），停止后续轮次；日志保留在 %s\n' "$name" "$rc" "$EXP" >&2
        return "$rc"
    fi
}

# 预热：少量消息，单独文件
run_round warmup-1 10000 || exit $?

# 测量轮次：每轮独立文件，不覆盖
run_round round-1 100000 || exit $?
run_round round-2 100000 || exit $?
run_round round-3 100000 || exit $?
```

先完成少量消息 smoke，再预热并进行三轮，每轮独立输出文件。预热或任一测量轮次非零退出时，将该轮退出码追加到 `exit-codes.log` 并立即停止，不执行后续轮次，也不删除已写入日志；成功的轮次同样记录退出码。保留工具报告的实际发送成功数、错误、records/s、MB/s 与延迟，不能仅凭退出码判断投递完整。消费端另行记录测试前后各分区 offset 增量并核对消息数量。固定消息大小、分区/副本、acks、压缩、批量、生产者数量和限速；不同投递语义不作直接收益比较。

需要吞吐上限测试时按实验计划调整限速；先估算总数据量并检查磁盘。topic 清理只作用于本次创建的测试资源。

若自动检查脚本位于 skill 目录而 Kafka 解压在别处，`--kafka-dir` 传相对当前调用目录的发行包位置，例如 `../experiment/kafka`。脚本直接使用该目录的 bin，不要求安装到系统 PATH。Java、Bash 与网络需在执行检查脚本的同一环境可用。存在认证时用 `--client-config` 提供真实的连接配置文件，文件由集群连接信息生成，不是示例附件。该文件只允许连接与认证项：脚本会按 Java `.properties` 规则（含转义与续行）解析，发现 `group.id` 时在创建任何 topic 之前拒绝执行，避免验证消费者加入业务消费组；脚本自身为每轮分配唯一消费组。

自动检查的 `--timeout` 是每个客户端进程的总等待上限（含 JVM 启动）；消费者的 `--timeout-ms` 同步换算为毫秒，最低为 1 毫秒。较慢的 broker 或 JVM 启动应增加该参数；任一步超时均保留日志并报告失败。上面的手工消费示例使用独立的 10 秒空闲等待。`--replication-factor` 默认省略以继承 broker 默认，单节点实验传 `1`；集群要求 `min.insync.replicas=2` 时不要传 `1`。
