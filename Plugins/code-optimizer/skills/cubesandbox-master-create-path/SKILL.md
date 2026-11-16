---
name: cubesandbox-master-create-path
description: 优化 CubeSandbox CubeMaster 创建关键路径中的模板解析 TTL 缓存、同键查询合并及 best-effort spec 异步落库。适用于同模板并发 SELECT 和创建响应前 UPSERT 等待；要求明确缓存一致性与持久化契约。
---

# CubeMaster 创建路径：解析缓存与异步 spec

将突发内重复只读解析合并，将允许延后的 spec 写移出创建响应关键路径。两个机制独立开关、独立验收；缓存与异步化均不能默认为语义等价。

## 输入与准入

获取源码版本、Master 副本数、MySQL 与 spec 的使用方、模板/副本更新路径、代表性创建负载和 `run_dir`。确认当前 spec 写本来就是 best-effort；若成功应答必须意味着 spec 持久化完成，禁用异步分支，或另行设计满足该契约的持久化机制。参考补丁的 spec 顺序保障限于同一 Master 进程；多 Master 或存在绕过这些写接口的写者时，先实现数据库版本条件或分布式 fence，否则不启用异步分支。

本目录独立包含 [机制与一致性](references/implementation.md)、[逐步操作手册](references/runbook.md)、[复现协议](references/reproduction.md)、参考补丁和分析工具。先检查目标源码已有缓存、查询合并与异步队列，避免重复实现；无需加载其他技能。

先按观测结果选择分支：重复 SELECT 占关键路径时实施缓存；同步 UPSERT 占关键路径且消费方接受延迟时实施异步落库；两者同时存在时先分别验收，再测组合。这里的分支是实验划分，开关或独立构建由目标版本提供，不代表参考补丁已暴露对应的运行时配置。

## 工作流程

先建立两条独立的因果链，不把异步化贡献全部计入缓存：

| 分支 | 原路径 | 优化路径 | 必需机制证据 |
|---|---|---|---|
| 模板解析 | 每请求重复 SELECT | 完整键 TTL + 在途同键查询合并 | DB 往返数、命中与合并数、解析阶段时间 |
| spec 落库 | 创建应答前同步 UPSERT | 行构造后入有界队列、后台写 | 队列深度、最终记录、应答时间与 drain 时间 |

1. **基线**：定位 locality、ListReplicas/bind、恢复源查询及 spec UPSERT；记录阶段延迟、同键查询次数、DB QPS 和创建总时间。共享 DB 的全局计数仅作辅助，优先用请求关联计数或独立窗口。
2. **缓存候选**：以完整查询语义构造键，保留租户/权限、模板、节点与版本等实际影响结果的维度。templatecenter 的 ready-list 和单副本缓存只保存副本元数据，不保存最终选择或可调度布尔值；命中后仍使用当前请求的 `ctx` 调用目标版本的 `isReplicaSchedulableNow`，ready-list 再做 preferred-node 选择。skip-locality 是另一类短期布尔决策缓存，单独复核其键、失效和 NotFound 语义，见实施说明。0.7.1 的 Now 判定仅检查 READY，目标契约需要节点实时健康检查时应接入真实状态源。
3. **去重与失效**：templatecenter 的 miss 按键与 generation 合并，Lookup、条件回填和失效使用同一锁协议；失效后新请求进入新代，过代查询的成功值和错误都重试当前代，不能污染新缓存。1s TTL 从成功回填开始，失败只释放本代占位；暂态 DB 错误和 ready-list 的无 READY 错误不负缓存。skip-locality 的 NotFound 短期决策单独复核，多 Master 仍不能依靠进程内失效提供跨进程立即一致。
4. **异步候选**：调用者构建不可变记录，有界队列、固定 worker、总 deadline 与有限重试；同 ID 在途写入会让接受新操作等待 gate，等待服从本次请求 ctx，不能声称 PutAsync 永不等待。队列满在 gate 内回退同步写；store 未就绪显式报错/计数。worker 生命周期与请求取消分离，服务关闭时明确 drain 或丢失窗口。
5. **乱序防护**：同一进程内让同步 Put、异步入队/队列满回退、Delete 与 worker UPSERT 共用按 sandbox ID 的可取消 gate；新操作推进 generation，worker 取得 gate 后丢弃过代队列项，实际 UPSERT 与有限重试持有 gate，阻止旧写晚于新操作落地。状态保留到排队、等待和在途引用全部释放再回收；跨进程顺序仍需数据库版本条件或分布式 fence，不能由进程内 gate 保证。
6. **正确性与性能**：分别测 cache-only、async-only、二者组合；验证副本变化、删除、队列饱和、DB 超时、进程退出与恢复。按共同协议比较总创建时间；记录 SELECT 下降与 UPSERT 最终完成，证明收益来源而非写丢失。

## 交付与历史参考

具体源码定位、六文件改动顺序、DB 只读观测、缓存键表、故障屏障测试、构建/心跳就绪与回退见 [操作手册](references/runbook.md)。历史参数（1s TTL、512 队列、2 worker、共享 10s 总尝试 deadline、3 次尝试）均说明适用边界，不要求在所有环境写死。

本包独立带采集计划、ABBA 分析器和测试；在本目录执行 `python3 scripts/validate_package.py` 运行完整包内校验，依赖 Git、Python 3 和 Go 1.20+。入口检查参考补丁的原始 LF 字节与 hunk 计数，再执行 Git 解析、环境继承回归、分析器测试及缓存选择/spec 写入顺序的 Go 用例；缺依赖、缺用例或任一步失败均返回非零。`--static-only` 只检查随包补丁格式与 Git 解析，不执行用例或补丁派生代码，输出 STATIC PASS 不能当作完整测试通过。各独立 runner 同样只使用本包补丁，向子进程提供最小白名单环境及专用临时 HOME/缓存，不转发父进程令牌或配置；环境清理不提供文件系统/网络隔离，未审核的仓库代码仍需在无凭据隔离 runner 运行。Go 用例归为 tier B，CI 接入和覆盖范围见 [操作手册](references/runbook.md)。按 [本地复现协议](references/reproduction.md) 比较本方案 c100/n100 结果；包内测试不能代替目标 DB、节点健康集成与完整生命周期验收。

输出候选补丁、缓存键和失效路径表、异步一致性协议、测试结果、原始 bench、配对结果及 `accepted / inconclusive / rejected / not_applicable / already_present / awaiting_target_evidence`。

[scheme2-rsi-cache.patch](references/scheme2-rsi-cache.patch) 以 0.7.1 已有实测经验对应的实现为基础，已补充逐请求判定、缓存代际回填保护与单进程 spec 写入顺序保护。目标版本迁移时复核实际健康判定、缓存一致性范围、所有写接口是否经过 gate，以及多 Master 与崩溃恢复的持久化协议。修订后的性能需重新测量，与原案例的实测收益分别记录，历史参数与数据见 [实施说明](references/implementation.md)。
