# 原理与实施：消除重复 DB 读，延后非必需写

## A. 模板解析缓存

同模板 c100 突发会重复查询同一状态。若 N 次查询均读取一个短期稳定结果，singleflight 将同时在途查询合为一次；TTL 缓存继续覆盖查询已结束后的近邻请求。两者作用域不同：singleflight 本身不保证完成后缓存，也不解决缓存一致性。

历史实现位置：

| 文件 | 作用 |
|---|---|
| `CubeMaster/pkg/templatecenter/resolve_cache.go` | TTL map、去重组和失效函数 |
| `templatecenter/store.go` | 单副本/READY 候选元数据缓存，逐请求判定，副本写后失效 |
| `templatecenter/cache.go` | 模板写关联失效 |
| `service/httpservice/cube/cubeboxutil.go` | skip-locality 决策及 NotFound 负缓存 |

值的 TTL 为 1s，从成功回填开始，用于覆盖同模板突发。在途 miss 先保留没有值和 TTL 的代际占位，不能因慢查询超过 1s 就重新使用同一代。不要用 TTL 承诺强一致性，也不要把 TTL map 当硬容量上限；高基数输入需要独立的容量/淘汰策略。

### 三类缓存的职责

| 读取位置 | 参考补丁保存的值 | 每次请求仍要做的事 | 迁移检查点 |
|---|---|---|---|
| `ResolveTemplateReadyReplica` | 模板的 READY 候选元数据列表 | 使用本次 `ctx` 判定候选，再执行 preferred-node 选择 | 不把某个请求的临时健康/策略结果固化到共享列表 |
| `isTemplateReplicaSchedulable` | 模板与节点对应的 `ReplicaStatus` | 使用本次 `ctx` 执行 Now 判定 | 不将判定后的 `bool` 当作节点当前状态缓存 |
| `skipTemplateLocalityReady` | 是否可跳过本地副本检查的短期布尔决策 | 按结果选择原有检查路径 | 独立审计失效；NotFound 缓存 `false`，仍保留完整本地检查；nil、超时和权限错误不填缓存 |

前两类用 templatecenter 内的 singleflight 合并元数据读取，查询完成后由每个调用者分别选择。当前沿用 v0.7.1 的 `templateFetchGroup.Do`：leader 使用自己的查询 ctx，同一 flight 的 follower 共享该查询的错误，follower 取消也要等共享调用返回后才被观察。这里尚未实现独立 waiter 的即时取消隔离；目标契约要求这一能力时需另行实现并验证。第三类位于 HTTP service 包，不应套用前两类的逐请求 Now 判定或写后失效结论。历史模板键之外，目标系统若以租户、权限域或模板版本区分结果，应先补足键语义，再衡量复用率。

### 在途回填与失效协议

需要阻止的交错是：读 R 获取旧 DB 值，写 W 更新并删除缓存，R 随后又把旧值填回。修订后的两类 templatecenter 缓存按如下协议处理：

1. `Lookup` 在同一 mutex 内返回值、命中状态和 generation；miss 也建立占位。generation 单调分配，slot 删除后不复用旧代，避免旧查询误认新 slot。
2. singleflight 的键包含查询类型、完整语义键和 generation。写后 `DeletePrefix` 删除相关 slot，失效后的请求建立新代及新 flight，不加入旧代查询。
3. 查询完成后 `Store(key, generation, value)` 在同一锁内核对当前代，只接受仍有效的本代值；成功时才启动值 TTL。失败时 `Discard` 只释放自己的代，旧失败不能删除新代占位或新值。
4. 旧代结果发现 Store/Discard 过代后，外层先检查请求 ctx，再重试当前代；flight 的 waiter 醒来后还检查 Current，避免在唤醒前发生失效时返回过代结果。ready-list 的无 READY 错误与 DB 错误不缓存；单副本已读出的状态元数据仍按 TTL/写后失效更新，不能由此推导跨 Master 状态立即可见。

这些检查维护本进程中的失效与回填顺序，不给数据库外部写者或其他 Master 提供全局线性一致性。多 Master 仍需共享版本/事件失效，或明确接受并记录 TTL 内陈旧；实际节点健康检查也继续由每个请求的目标判定源执行。

skip-locality 缓存与 templatecenter 缓存处于不同包。参考补丁只有后两类 resolve 缓存接到 invalidateResolveResultCache，不能笼统声称三个决策都在写后立即失效。逐条审计失效路径及节点心跳/可调度性变化。

**元数据与当前判定分离**：修订后的 `readyReplicaResultCache` 保存 READY 候选元数据；填充时只按规范化后的 `Status` 筛选，不把某个请求的健康/权限判定结果过滤进共享列表。`selectReadyReplica(ctx, ...)` 在 miss、singleflight 返回及后续命中时，都调用当前请求的 `isReplicaSchedulableNow(ctx, replica)`，通过后才执行 preferred-node 策略。暂时不健康的 READY 候选仍留在元数据列表中，恢复后可被下一请求重新检查。`replicaSchedulableResultCache` 同样保存 `ReplicaStatus`，不能保存最终 `bool`；singleflight 只合并元数据读取，判定在其外逐请求执行。

对照 [v0.7.1 的 store.go](https://github.com/TencentCloud/CubeSandbox/blob/v0.7.1/CubeMaster/pkg/templatecenter/store.go)，`isReplicaSchedulable` 确实存在，但它直接比较 `Status`；`isReplicaSchedulableNow` 使用 `strings.TrimSpace(Status)` 后比较 READY，目前也不查询实时节点健康。恢复 Now 调用保留的是目标判定接口与其规范化语义，不能因此声称已实现节点健康检查。目标版本若增加心跳、维护模式或请求策略检查，这些检查必须读取当前状态并留在缓存之外；跨 Master 的副本元数据变化仍受 TTL/失效协议约束。

对 AppSnapshot 的 NotFound，只有类型/标识语义证明必定不属于 snapshot 时才允许直接跳过查询；否则只能做满足陈旧预算的短 TTL 负缓存。NotFound、nil、超时和权限错误不能混为一谈。

## B. spec 异步持久化

创建总时间包含响应前同步 UPSERT 时，移到后台可减少关键路径等待，但 DB 工作量没有消失。必须观察完整 drain 时间和最终行数，不能通过留下后台欠账声称吞吐提高。

位置为 `sandboxspec/async.go`、`sandboxspec/store.go` 与 `configureSandboxSpecHooks`。修订实现保留 512 队列、2 worker 和最多 3 次尝试。worker 取出队列项、开始处理时创建共享 10s context，预算包含等待同 ID gate、UPSERT 与退避，不包含此前的队列等待；不是每次尝试各有 10s。排队、gate 等待和实际执行分别记录，不能仅看 UPSERT 时长。原附件的短期 tombstone 标记已由按 ID 的顺序状态替代，不依赖 5min 到期假设来判断旧任务是否仍可写入。

需要分别记录两个完成点：请求内完成 canonical record 的构造、取得 gate 和入队，worker 随后尝试 UPSERT；同 ID 旧写仍在途时，PutAsync 接受新操作也会等待 gate，等待服从请求 ctx。入队成功只说明进程已接收任务，最终写入应在 drain 后逐 ID 核对。队列满时回退同步写，store 未就绪则返回显式错误；这些分支的响应时延与失败结果都应保留在样本中。

| 观测点 | 回答的问题 |
|---|---|
| 应答时间与 `total_time` | 创建请求的等待是否减少 |
| 队列深度、最老任务年龄与 drain 时间 | 是否只把等待转移成持续积压 |
| 最终 spec、更新版本与删除结果 | 是否写全，是否被旧写覆盖或删除后复活 |
| worker 错误、重试及同步回退次数 | 改善是否依赖丢写，资源压力是否转移到后台 |

这些是需要采集的逻辑观测项，按目标版本已有日志或指标实现映射，不假定参考补丁已提供同名指标。

需要明确的状态顺序：

```text
create(v1) -> queued -> writing -> persisted
update(v2)             必须保证 v1 不会晚于 v2 覆盖
delete(v3)             必须保证 v1/v2 都不能复活该行
```

参考补丁用以下协议维护同一进程中的顺序：

1. 同步 Put、PutAsync 的接受/队列满回退、Delete 和 worker 实际 UPSERT 共用同一个 sandbox ID gate；调用者等待 gate 时服从请求取消。
2. 新 Put、PutAsync 或 Delete 在接受操作时推进该 ID 的 generation。队列项携带状态引用与接受时的 generation，worker 取得 gate 后先检查版本，过代任务直接跳过。generation 在 DB 尝试前推进，新操作随后失败也不会重新放行旧队列版本；调用方处理错误并重试新意图，不自动退回旧值。
3. worker 的 UPSERT 和有限重试在持有 gate 时执行；新 Put/Delete 等待在途旧写完成后再落地，因此旧写不能在新操作之后覆盖或复活记录。不同 ID 使用不同 gate，可由 worker 并行处理。
4. 等待者、排队项和在途操作都持有状态引用，最后一个引用释放后才回收状态。已入队任务与调用者 ctx 分离，仍在 worker 自己的预算内执行；请求结束不等同于撤销已接受写入。

这只覆盖同一进程、同一 ID 且所有写者都经过上述接口的顺序。多个 Master 或外部写者共享数据库时，需要数据库内版本条件写、持久删除版本或分布式 fence；未建立这些机制时不启用异步分支。进程内队列也不提供崩溃后的重放能力；允许崩溃丢失待写 spec 时，在契约与结果中明确丢失窗口，并验证消费方能够接受。

## 验证矩阵

| 分支 | 测试 | 验收依据 |
|---|---|---|
| 查询合并 | 同键/异键突发、leader 出错、follower 取消 | 相同键 DB 请求数下降、异键不串行；按原 Do 共享错误，独立 waiter 取消隔离另行验收 |
| 缓存 | 写与回填交错、节点下线、多 Master、权限隔离 | 不错误选择副本；陈旧窗口满足契约 |
| 负缓存 | NotFound 后新增、DB timeout、无 READY→READY | 新状态按预算可见，不缓存暂态错误 |
| 异步 | 队列满、worker 超时、关闭、崩溃恢复 | 无静默丢行；已声明丢失窗口可观测 |
| 顺序 | 阻塞旧 UPSERT，再触发 Put/Delete 后释放旧写 | 新值不被覆盖；删除后保持不存在 |
| 性能 | cache-only、async-only、combined | total_time、SELECT、队列深度、最终写入数相互印证 |

用可控 DB stub/同步屏障确定性制造顺序，不依赖 sleep 碰运气；再在目标机做真实 DB 集成和 c100 测量。历史附件记录解析 p50 12.02→2.61ms、诊断窗口 SELECT +312→+14、INSERT +103；这些是机制参考，不是当前版本的固定验收数字。

包内完整校验会编译执行实际补丁派生代码。其子进程采用最小环境白名单、独立临时 HOME/配置/Go 缓存，不继承操作者令牌或默认工具配置；这不构成文件系统或网络沙箱。静态模式只做补丁格式与 Git 解析，完整回归应在已审查代码、无凭据的隔离测试环境执行，范围见本地 [runbook.md](runbook.md)。
