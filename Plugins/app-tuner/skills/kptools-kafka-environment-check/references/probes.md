# 探针、判定与证据

所有命令都有独立有限超时，POSIX 下在独占进程组运行，超时或 KeyboardInterrupt
都 kill 自己启动的进程组并用第二个有限预算 drain；interrupt 随后重新抛出。
输出目录使用 `mkdir(parents=True, exist_ok=False)` 独占创建。

| 探针 | 强制 | ready 证据 | 主要失败类别 |
| --- | --- | --- | --- |
| `secure_client_config` | 是 | 文件属主/权限安全，键在白名单，值不进入报告 | `absent` / `insecure_config` / `invalid_config` |
| `official_cli_tools` | 是 | 六个实际使用的官方脚本均存在且可执行 | `absent` |
| `cli_version` | 是 | `kafka-topics.sh --version` 返回 4.4.x | `wrong_version` / `timeout` / `error` |
| `java_runtime` | 是 | 按官方脚本优先取 `JAVA_HOME/bin/java`，否则用 PATH 中的 `java`；版本可解析且 major >= 17 | `absent` / `wrong_version` / `error` |
| `authenticated_metadata` | 是 | 真实 `kafka-topics.sh --list` 成功；stdout 仅按列表解析并保留条目数，不收集 topic 名，也不把名称中的错误词当诊断 | `auth` / `timeout` / `error` |
| `topic_plan` | 创建模式是 | 唯一命名/分区/副本计划有界；明确未发 create/delete | 计划参数由 CLI 拒绝 |
| `topic_metadata` | 既有 topic 是 | describe/get-offsets 使用字面量转义并只选精确同名结果；describe 头接受官方 TopicId（同时保留无 TopicId fixture 兼容性）；offset 完整覆盖 describe 分区集合且总和为零 | `auth` / `timeout` / `topic_not_empty` / `error` |
| `operation_authorization` | 是 | 用户显式声明允许计划中的写操作 | 未声明时为 `unknown` |
| `workload_budget` | 是 | 轮数/条数/速率/超时有限，总 perf 身份不超过 1000000，计划载荷不超过 100 GiB，限速估计不超进程预算 | `resource` / `invalid_config` |
| `output_storage` | 是 | 本地结果盘覆盖累计全量身份验证日志，且至少保留 16 MiB 余量 | `resource` / `error` |
| `host_resource_evidence` | 否 | 记录逻辑 CPU 与 MemAvailable（能读取时） | 不可知只标 `unknown` |

任一强制探针 `blocked` 则总体 `blocked`；无 blocked 但有强制 `unknown`
则总体 `unknown`；只有全部强制探针 `ready` 才总体 `ready`。可选探针不会把
总体降级。

## 版本边界

版本门只声明脚本实际调用的参数面：Kafka CLI **4.4.x** 和 Java 17+。
其他版本即使可能兼容也 fail closed，报告 `wrong_version`。真实运行必须保存
`cli_version.evidence.version` 与 `java_runtime.evidence.version` 的精确值，不能
把“4.x”写成已验证版本。Java 17 要求与 Kafka 4.x 官方运行要求一致；安装与服务
部署不在本 skill 范围。4.0.x/4.1.x 官方已归档并有未修复 CVE 公告（如
CVE-2026-35554、2026-10-28 批次），属于已证实的拒绝理由而非兼容性猜测。

## 认证与授权的区别

metadata 查询成功可证明客户端确实完成了协议交互，而非仅 socket-open。它不能
证明 Produce、Consume、CreateTopics 或 DeleteTopics ACL。只读检查不会用写操作
试探 ACL，因此 `--operations-authorized` 只是资源所有者的授权声明，报告同时
保留 `broker_acl_verified=false`。缺声明时总体是 `unknown`，不猜测生产权限。

## 来源

- [Kafka 4.4 配置](https://kafka.apache.org/44/configuration/)
- [Kafka 4.4 安全](https://kafka.apache.org/44/security/)
- [Kafka CVE List](https://kafka.apache.org/community/cve-list/) 与 [Downloads](https://kafka.apache.org/community/downloads/)：4.1.x/4.0.x 已归档且存在未修复公告
- Java properties 解析参考[已有 Kafka 部署检查](https://gitcode.com/openeuler/KPBot/merge_requests/206)，解析实现包含在本技能目录中。
- JVM 选择依据[官方启动脚本](https://github.com/apache/kafka/blob/4.4.0/bin/kafka-run-class.sh)，报告保留 `selected_from` 以区分 `JAVA_HOME` 和 PATH。
