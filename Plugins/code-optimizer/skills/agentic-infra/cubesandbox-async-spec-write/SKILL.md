---
name: cubesandbox-async-spec-write
description: 缩短 CubeSandbox CubeMaster 创建应答前等待 sandbox spec UPSERT 的时间。当跟踪表明 spec 同步写位于创建关键路径，且调用契约允许应答后尽力落库时使用。
---

# CubeSandbox spec 异步落库

在请求内完成规范化和记录编码，将独立记录提交到有界写队列，由后台 worker 执行数据库 UPSERT。入队完成和数据库提交是两个完成点，各自采样与验收。

本补丁与 `scheme2-rsi-cache.patch` 的异步 spec 实现互斥。需要默认同步、按需启用及同进程待写记录可见性时，选择本实现。

1. 确认 `configureSandboxSpecHooks` 的同步 `sandboxspec.Put` 确实在应答前等待。按 sandbox ID 关联 hook、UPSERT 和应答时刻。把带覆盖项立即生成快照、立即更新网络后克隆纳入准入检查，明确数据库可见性要求和崩溃丢失窗口。
2. 核对部署中的所有 spec 写者。参考方案的顺序保障限于同一进程且所有写者使用同一套写接口。应答必须意味着已经持久化，或多个进程可写同一 ID 且无数据库版本保护时，保留同步路径。
3. 用 [参考补丁](references/async-spec-write.patch) 实施有界队列与同 ID 写序。默认同步，具备准入条件后显式启用。`Get` 可以读取同进程待写记录的独立副本，已经在途则按请求预算等写入结束。队列满同步回退，旧创建写无法覆盖更新或复活已删除记录。
4. 按 [实施与复现](references/runbook.md) 运行写序回归和真实数据库验收。做同步基线与异步候选的独立对照，记录 c100/n100 整批 `total_time`、队列积压、后台完成时间和最终记录。

交付应答与持久化契约、各写入口、目标变更和构建版本、正确性与原始基准结果、停写回退步骤。选用该方案时保留入队、同步回退和 worker 失败的可观测性。
