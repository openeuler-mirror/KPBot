---
name: kptools-kafka-environment-check
description: 'Kafka 环境检查、压测前置检查、已有 Kafka 服务只读探针、客户端版本/JDK 兼容性核查、SASL/SSL 连接与认证排查、非默认 bootstrap 端点核查时使用。 对指定现有安装执行有超时的只读检查，交付逐探针 JSON 证据及 ready/blocked/unknown 结论；不部署、不启停服务、不写消息。

  '
license: MulanPSL-2.0
metadata:
  compatibility: Linux/Bash，Python 3.10+；仅 Apache Kafka CLI 4.0.x/4.1.x 已实现参数面，Java 17+
  skill_type: tool_assisted
  category: environment_check
  domain: kafka
  side_effect_free: 'true'
  version: 1.0.0
---

# Kafka 压测环境只读检查

## 输入与边界

需要现有 Kafka 发行包目录、可访问的 bootstrap 端点、输出路径，以及
`--topic` 与 `--allow-create-topic` 二选一的计划目标。认证集群另提供当前用户
所有、权限为 `0600` 或 `0400` 的 client properties 文件。支持逗号分隔的非默认
端点，端点不得携带 userinfo。

本 skill 只运行本地版本命令及 Kafka metadata list/describe/get-offsets。
不安装依赖，不创建/删除 topic 或 group，不生产/消费消息，不改 broker、OS、
数据库或服务状态。`ready` 仅表示本页实现的强制探针通过，不是生产健康、容量、
全部 ACL 或后续压测必然成功的证明。

## 执行

将示例中的 `../kafka` 替换为实际已安装的 Kafka 发行包目录，端点替换为获授权的现有 broker。以下新建唯一 topic 模式不需要预建数据或配置文件。认证集群另加 `--client-config` 指向管理员提供的现有安全配置；无认证实例省略该参数。

在本 skill 目录运行；所有示例路径均按当前目录解析，输出目录必须不存在。

```bash
# 最小示例：现有 loopback Kafka；只评估创建计划，本检查不会创建 topic
python3 scripts/check_environment.py \
  --kafka-dir ../kafka \
  --bootstrap 127.0.0.1:19092 \
  --allow-create-topic \
  --replication-factor 1 \
  --operations-authorized \
  --output results/env-001

# 可选：仅当管理员已提供空专用 topic 和真实安全配置时使用
python3 scripts/check_environment.py \
  --kafka-dir ../kafka \
  --bootstrap localhost:9092 \
  --topic kpbot-empty-dedicated \
  --operations-authorized \
  --output results/env-002
```

默认计划为每轮 100000 条十进制 monotonic 身份消息、3 个计时轮，另有 10000
条预热，限速 10000 records/s，每个子进程硬超时 120 秒。`--rounds` 只接受
3–10；全部 perf 身份总量不超过 1000000，其他负载参数和超时也都有有限上下界。
详见 `--help`。

`--client-config` 只接受 transport/auth/连接超时白名单属性，拒绝 `group.id`、
业务 producer/consumer 配置及 `bootstrap.servers`，避免加入既有消费组或覆盖
目标。凭据只从该安全文件读取，不能放入命令行；报告保存文件 SHA-256、权限和
属性键，属性值及工具输出中的秘密均脱敏。

## 结果与退出码

- `result.json`：目标、精确 CLI/JDK 版本、计划负载、逐项 `mandatory`、
  `status`、`category`、`evidence` 和限制说明。
- `0 ready`：全部强制探针通过；`1 blocked`：缺工具、版本不符、认证/超时/
  运行错误、非空 topic、资源或配置不满足。
- `2`：参数、输出路径或独占建目录失败；旧输出不会覆盖。
- `3 unknown`：强制信息不足，例如未传 `--operations-authorized`。这不等于
  `ready`，也不能据此启动写入压测。

探针类别明确区分 `absent`、`wrong_version`、`timeout`、`auth`、`error`；
socket 可达或 Python 包可导入均不属于通过证据。逐探针语义见
[probes](references/probes.md)，负载与指标边界见 [metrics](references/metrics.md)。

## 回归

```bash
bash cases/contract/run.sh
```

该 tier-A 用例以状态化假 CLI 检查成功、版本不符、缺工具、认证、超时、运行
失败、非空 topic、输出冲突与秘密脱敏；它不是实际 Kafka 服务 E2E。

## 官方依据

- [Apache Kafka 4.1 Quick Start](https://kafka.apache.org/41/getting-started/quickstart/)
- [Apache Kafka 4.1 Security](https://kafka.apache.org/41/security/)
- [Apache Kafka 4.1 Configuration](https://kafka.apache.org/41/configuration/)
