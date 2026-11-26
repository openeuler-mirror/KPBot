# Agentic Infra 域（code-optimizer 侧）

Agent 运行时基础设施的性能优化：microVM 沙箱（CubeSandbox）与 AgentENV 沙箱的创建/恢复路径。
本插件侧负责**创建路径与内核机制**；部署、基准协议、生命周期压测、时序分析属于 app-tuner 侧的同名域（另一个 PR 迁入）。

## 成员

| Skill | 职责 |
|---|---|
| [`cubesandbox-guest-rpc-overlap`](cubesandbox-guest-rpc-overlap/SKILL.md) | Evaluate concurrent guest clock setup and RNG reseeding during snapshot restore, including the create barrier and failure semantics. |
| [`cubesandbox-create-optimize`](cubesandbox-create-optimize/SKILL.md) | CubeSandbox 完整创建路径与多机制组合调优：核对 Guest 预连接、Master 解析缓存与异步 spec、rootfs 影子预派生池之间的交互，做组合消融与端到端归因 |
| [`cubesandbox-replica-metadata-ttl`](cubesandbox-replica-metadata-ttl/SKILL.md) | Opt-in, process-local reuse of READY replica metadata by template for one second, with per-request placement checks. The reference patch is mutually exclusive with `scheme2-rsi-cache.patch`. |
| [`cubesandbox-template-cache-encoding`](cubesandbox-template-cache-encoding/SKILL.md) | 模板定义缓存命中后仍反复编码时，在填充缓存时编码一次，每个创建请求独立解码。 |
| [`cubesandbox-spec-final-encoding`](cubesandbox-spec-final-encoding/SKILL.md) | 显式指定存储后端导致 spec 重复编码时，先完成后端规范化，再生成一次最终的 `RequestJSON`。 |
| [`cubesandbox-restore-source-negative-cache`](cubesandbox-restore-source-negative-cache/SKILL.md) | 同一恢复源在 locality 探测中反复返回未找到时，短时复用该结果，每个请求仍检查本地就绪状态。 |
| [`cubesandbox-snapshot-binding-read`](cubesandbox-snapshot-binding-read/SKILL.md) | 快照绑定连续读取同一记录，且就绪与放置共享固定视图时，从同一条记录校验就绪状态并取得放置所需字段。 |
| [`cubesandbox-origin-fsync-group-commit`](cubesandbox-origin-fsync-group-commit/SKILL.md) | 同一 origin 下并发 rename 需要持久化应答时，合并目录 fsync，各请求等到覆盖自身 rename 的刷盘成功再应答。 |
| [`cubesandbox-rootfs-refill-budget`](cubesandbox-rootfs-refill-budget/SKILL.md) | 多个模板世代在同一存储节点并发补池时，统一限制池库存与后台派生并发数。 |
| [`cubesandbox-rootfs-origin-lookup`](cubesandbox-rootfs-origin-lookup/SKILL.md) | 创建重试遇到 `AlreadyExists` 且枚举同源快照开销高时，按名查找并核对对象类型、来源与身份。 |

## 域内路由

跨 Guest、Master 和 rootfs 联调或需要整批创建时间归因时，选择 [`cubesandbox-create-optimize`](cubesandbox-create-optimize/SKILL.md)，用其单项实验臂做组合消融。跟踪已定位到一个调用点时，按上表的触发条件选择专项入口。

- **CubeMaster**：同键并发 SELECT 或应答前 UPSERT 等待走 [`cubesandbox-master-create-path`](../cubesandbox-master-create-path/SKILL.md)。若热点是 JSON 编码，按发生位置选模板缓存命中或 spec 最终编码。对恢复源查询，先定位到 locality 探测还是快照绑定。负缓存只用于允许延迟看到新来源的探测；进入绑定后重新读取，要求看到最新提交时保留第二次读取。已有 Master 缓存时，先检查该调用点是否已缓存相同结果，避免叠加两层 TTL。
- **Cubelet**：需要把前台 rootfs 派生移到后台并实现领养时，走 [`cubesandbox-rootfs-shadow-pool`](../cubesandbox-rootfs-shadow-pool/SKILL.md)。已有池出现跨模板争用时看补池预算，同源 rename 等待目录持久化时看 fsync 合并。两者并用时，库存名额在所有权转移后释放，成功应答仍要等到该 rename 持久化。重试扫描同源快照时看按名查询；该入口只处理已有对象的核验。
- **Guest**：恢复后串行建连走 [`cubesandbox-agent-preconnect`](../cubesandbox-agent-preconnect/SKILL.md)；连接已就绪，`SetGuestDateTime` 与 `ReseedRandomDev` 串行占用关键路径，且失败实例可隔离时，走 [`cubesandbox-guest-rpc-overlap`](cubesandbox-guest-rpc-overlap/SKILL.md)。

## 后续迁入

- code-optimizer 侧：上述三个域外目录 `cubesandbox-master-create-path`、`cubesandbox-rootfs-shadow-pool`、`cubesandbox-agent-preconnect` 后续逐个迁入本域，保留各自的阶段级入口。
- app-tuner 侧（`Plugins/app-tuner/skills/agentic-infra/`）：`cubesandbox-deployment`、`cubesandbox-bench-protocol`、
  `cubesandbox-lifecycle-bench`、`sandbox-timeline-profile`、`kptools-agentenv-tuning`（含 3 个专项子技能包）、`kptools-openeuler-agentenv`

## Review

- owners: []（待指派，填写 GitCode 用户名）
- 评审范围：`Plugins/code-optimizer/skills/agentic-infra/**`
