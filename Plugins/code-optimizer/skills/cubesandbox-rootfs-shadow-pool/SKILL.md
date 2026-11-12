---
name: cubesandbox-rootfs-shadow-pool
description: 优化 CubeSandbox Cubelet/cubecow 的模板 rootfs 派生，通过后台预派生、rename 领养和目录批量 fsync 降低并发创建存储开销。适用于 reflink 派生及共享目录持久化争用；需明确持久化应答契约与补池资源预算。
---

# CubeSandbox rootfs 影子预派生池

将 FICLONE 派生前移到后台，前台领养已准备快照。核心是省掉前台派生，而不是轮换多个来源再次执行 FICLONE。该池不是 malloc 对象池。

## 输入与准入

获取 Cubelet/cubecow 版本、存储引擎和文件系统、模板是否不可变、创建与删除语义、持久化要求、突发规模和 `run_dir`。先定位 derive、引擎锁和 origin 目录 fsync 的真实成本；非 reflink 后端必须重新确认能力，不能套用本补丁。

本目录独立包含 [原理与实现边界](references/implementation.md)、[逐步操作手册](references/runbook.md)、[复现协议](references/reproduction.md)、原始补丁与分析工具。所需 Go/Rust/cgo 构建顺序、开关、清理与恢复要求均在包内，不依赖其他技能。

先确定本次要回答的问题：满池实验衡量已准备成员被领养时的创建时间；冷池/耗尽实验衡量 miss 回退；持续负载衡量补池能否跟上消耗。三类实验使用各自的采样记录，按操作手册记录每次调用前水位、命中与补池成本，再解释 `total_time` 的变化。

## 工作流程

实施必须覆盖完整调用链，而非仅新增池 map：

```text
CreateSandboxRootfsFromTemplate
  → pool lookup/reserve → Go RenameSnapshot
  → cgo/C ABI → Rust ReflinkEngine 文件 rename 与索引更新
  → 扩容/路径检查 → 领养完成
  ↘ flush pending → origin 目录 fsync
  ↘ 单 worker 补池
miss/disabled/unsupported → 原派生路径
```

池成员的 available/reserved/adopted 状态与池的 closed 状态共同约束所有权；具体检查点见实施说明。历史补丁中的列表/map/closed 标志是迁移起点，显式预留协议、在途任务等待和严格持久化屏障应按目标契约核验。FICLONE 在后台完成不等于开销消失；rename 原子也不等于掉电后已持久化。

1. **归因**：记录前台 FICLONE 次数、derive 分布、fsync 次数/耗时和锁等待。若存储不在关键路径，返回 `not_applicable`。
2. **设计池生命周期**：按模板身份与版本隔离成员，后台有界错峰构建，维护 available/reserved/adopted/closed 状态；只回收可证明属于该不可变模板且未被 Guest 使用的孤儿。
3. **实现领养**：原子预留成员，执行带冲突检查的 rename 并同步引擎索引；完成尺寸/路径检查后返回。失败路径区分未发生与已发生 rename，避免泄漏、重复领养或错误回退覆盖。
4. **设计持久化**：声明创建应答前是否必须完成 rename 的 fsync。需要持久成功语义时等待 group-commit 屏障；允许延迟时记录崩溃窗口。20ms 定时器不构成绝对持久化上界，排队和 fsync 失败均需处理。
5. **回退与清理**：池空、禁用或不支持时走原派生；模板删除先关闭并等待构建/领养任务，再清池。服务停止对待 flush 领养做已声明的 drain。设置可观察的池容量、命中/回退、补池及 flush 失败指标。
6. **验证**：覆盖并发领养、同目标重试、rename/resize/fsync 失败、模板更新/删除、重启与掉电恢复。先正确性，再分别测满池突发、冷池、池耗尽和持续负载，记录补池 CPU/I/O 成本。
7. **归因验收**：同一候选二进制比较 pool-enabled 与 pool-disabled，并与原基线比较；以总创建时间下降为主指标，命中率、FICLONE/fsync 次数为机制证据。

## 交付与历史参考

文件级修改、FFI 调用链、480/25ms/20ms 参数依据、混合构建、首次准备调用、池就绪、故障矩阵及开关消融见 [操作手册](references/runbook.md)。先按顺序构建 Rust 静态库和 C 头文件，再构建 cgo Cubelet，不能链接旧库测新方案。

本包自带采集计划、配对分析器及测试；满池/冷池/持续负载分别采集，不能混到同一 manifest。工具命令和报告字段见 [本地复现协议](references/reproduction.md)。

交付状态、源码/静态库/头文件与运行产物哈希、池状态及持久化协议、正确性报告、bench 原始数据和配对结果。目标机不可用时记 `awaiting_target_evidence`。

[scheme3-pool.patch](references/scheme3-pool.patch) 为 0.7.1 历史实现：默认 480 成员、25ms 补池间隔、20ms flush 窗口。它不保证后台与前台完全无争用，模板删除也未等待所有在途构建结束；这些边界需迁移时处理。参数依据见 [实施说明](references/implementation.md)。
