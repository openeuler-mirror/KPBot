# 实时前置检查与资源所有权

## fail-closed 顺序

脚本不消费任何旧报告。每次调用都以当前进程实际能执行的官方 CLI 为准：

1. 校验输出目录独占创建和安全配置；配置无效时不发网络请求。
2. 检查 `kafka-topics.sh`、`kafka-get-offsets.sh`、console producer/consumer、
   consumer-groups 和 producer-perf 六个脚本及 Java 17+。
3. 只接受 Kafka CLI 4.4.x 已实现参数面，保存实际三项版本与 JDK 完整
   版本；4.0.x/4.1.x 官方已归档并有未修复 CVE 公告，其他版本也不是“兼容”，
   一律 `wrong_version`。
   JVM 与官方启动脚本一致，优先使用 `JAVA_HOME/bin/java`，未设置时再用 PATH。
4. 真实执行 metadata `--list`，分别识别 `absent`、`auth`、`timeout`、`error`。
   不用 socket-open、包导入或历史成功替代它。
5. 校验计划载荷不超过 100 GiB、全部 perf 身份不超过 1000000，限速下最长
   阶段估计不超过单进程硬超时，本地结果空间覆盖累计身份验证日志。broker
   副本/磁盘/retention 容量仍需资源所有者批准。
6. 既有 topic 先 describe，再读取各分区 latest offset；总和必须为零，避免
   正确性消息与既有数据混合。该步骤不会删除或改写旧记录。

以上强制项未全部通过则退出 2、`status=blocked`，不会进入 create/produce。
CreateTopics/DeleteTopics/Produce ACL 只能在实际授权操作中最终确定；失败保留
原生错误，不声称只读探针能证明全部 ACL。

## topic 状态与清理

创建模式在发 RPC 前将随机精确名称写入 `result.json`：

- 返回成功：`topic_create_state=created`、`topic_created_by_this_run=true`；
  finally 中默认仅对这个精确名称发一次 delete，`--keep-topic` 则保留。
- 返回非零或超时：`topic_create_state=ambiguous`，随后仅 describe 该名称并保存
  `ambiguous_topic_present`。因为所有权不能从模糊 RPC 得到确定证明，本实现
  不自动 delete，标记 `retained_rpc_ambiguous_not_owned_for_certain`。
- 既有 topic：`topic_create_state=not_applicable`，任何路径都不发 create/delete。

三条正确性验证、预热和每个计时轮各使用一个随机 UUID group。只有实际发起过的
group 才登记到 `consumer_groups_started` 并在 finally 中逐个按精确名称删除。
官方 ConsoleConsumer 不允许 `--group` 与 `--partition` 同用，因此全量身份核对
使用 group + `--from-beginning`，不依赖 partition 分配顺序。group 已不存在记为
`already_absent`，其他错误写入 `cleanup.consumer_groups` 和 `cleanup.issues`。

`workload_status` 只描述实际负载及正确性，`cleanup.status` 独立描述资源清理；
清理失败把总体 `status` 改为 failed，但不会覆盖已经通过的 workload_status。
没有通配符删除、没有全局服务停止动作、没有既有 topic 删除。

## 进程与秘密边界

所有进程 `shell=False`、参数分别传递、POSIX 独占进程组；每个 communicate 都有
有限 timeout。超时或 KeyboardInterrupt 都 kill 自己启动的进程组并用第二个
有限预算 drain，绝不执行全局进程清理；interrupt 在保存该命令日志后重新抛出，
随后进入 owned-resource finally 清理。console 输入通过 stdin，不做 shell 插值。
密码只能在当前用户所有且无 group/other 权限的 properties 文件中，通过对应官方
`--command-config` / `--producer.config` / `--consumer.config` 文件参数传递。

配置按 Java properties 转义/续行解析，只允许 transport/auth/连接超时键；拒绝
`group.id` 和负载覆盖项。报告只存 SHA-256、权限、键名，命令和原生日志对配置值
及常见 password/secret/token 赋值脱敏。

实现参考：[Kafka 4.4 Security](https://kafka.apache.org/44/security/) 与
[Kafka 4.4 Operations](https://kafka.apache.org/44/operations/)。Java properties
解析适配自本次请求随附的用户参考辅助代码，已完整内嵌，无外部源码依赖。
