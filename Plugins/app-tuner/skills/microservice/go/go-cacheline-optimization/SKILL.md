---
name: go-cacheline-optimization
description: 当 Go 服务出现 cache miss、跨核缓存行迁移或怀疑伪共享/真共享时使用。按证据采集、差分诊断、字段归属、假设验证和优化点卡片的流程识别缓存行争用优化点，输出证据、置信度、反例、方向级修复思路和移交建议，不展开具体实现或补丁。
---

# Go Cacheline Optimization

本 skill 做缓存行争用的识别、分类、方向级建议和优化点验证；不展开具体实现，不写补丁，不改业务代码。识别结果按争用类型移交：业务结构布局 → code-optimizer；运行时布局补丁 → `go-runtime-patch-build`；sync.Pool 参数 → `go-syncpool-optimization`；GC 参数 → `go-runtime-tuning`。

## 何时触发

- 目标服务是 Go 程序，分配/调度热路径在 CPU profile 中占比高。
- 硬件缓存证据显示 cache miss、跨核缓存行迁移或 true sharing 集中。
- 怀疑相邻字段被不同核分别读写，或共享元数据/统计计数的写路径异常。
- 需要判断“这里到底是不是缓存行争用、值不值得优化”，而不是已经知道具体改法。

不进入本 skill：

完整排除清单见 `references/cacheline-contention-playbook.md` §1。典型情况：无缓存行为证据；容量/带宽/锁/真共享已有更直接解释；不能重编译且不能改布局；数据竞争；已明确修复方案；只有 GC/sync.Pool 颠簸。

## 依赖与分工

- `go-pprof-analysis`：CPU/heap profile 和热点调用链采集（识别阶段按需）。
- `perf` 或硬件缓存事件采集：cache miss、跨核迁移、true/false sharing 证据（识别阶段必需；命令与权限见 playbook §2）。
- `microservice-ab-testing`：仅在需要验证候选变更时使用，不用于识别阶段。
- `go-runtime-patch-build`：仅在优化点需要运行时补丁时使用。
- `code-optimizer`：业务结构布局、字段重排等代码实现。
- 本 skill 只输出识别结果、方向级建议和移交建议，不复制下游 skill 的实现细节。

## 必读 Reference

- `references/cacheline-contention-playbook.md` — 证据体系、差分诊断、字段归属、假设验证、置信度、优化点卡片和常见误判。
- `references/demo-case.md` — 合成示例：从 cache miss 现象到优化点卡片的识别流程。
- `references/prerequisites.md` — 证据采集依赖、授权和缺失降级。

## Input Modes

### 独立运行（standalone）

- **触发**：未提供 `evidence_snapshot_dir` 或 `environment_backup_dir`。
- **行为**：自采集 Go 基线、缓存事件、热点函数和结构信息；采集后询问压测/对照条件。
- **输出**：`findings[]` 和方向级建议；不执行构建、补丁或 A/B。

### 上层 Agent 调用（可选）

- **触发**：上层 Agent 提供 `evidence_snapshot_dir` 和/或 `environment_backup_dir`。
- **行为**：直接读取已有证据，不重复采集。
- **输出**：只输出 `findings[]`、缺失证据和下一跳；不执行构建、补丁或 A/B。

## 输入证据

- 基线与二进制身份：`go version`、`$GOROOT/VERSION`、`go version -m <binary>`、sha256。
- 缓存行为：cache miss、跨核缓存行迁移、true sharing/false sharing 采样，热点地址/函数与共享结构的对应关系。
- 热点函数：分配/调度/同步路径的 CPU 占比和调用链。
- 结构清单：字段、访问频次、核归属、读写比例、序列化/反射/`unsafe` 约束。
- 运行条件：`GOMAXPROCS`、容器 CPU limit、绑核、NUMA、负载分布。
- 反例证据：容量/带宽、锁竞争、NUMA 远端访问、频率和负载不均等其它解释。

## Workflow

### Step 0：目标与基线

- 明确服务、负载、二进制身份和要回答的问题：是否缓存行争用、在哪个结构/字段、是否值得优化。
- 没有可采集的 profile 或缓存事件时，停止在假设阶段，输出 `blocked_evidence`。

### Step 1：证据采集

- 至少采集两类独立证据：硬件缓存事件、热点函数 profile、字段归属、受控实验。
- 缓存事件看 cache miss、跨核迁移、true sharing 的分布，不只看总量。
- 记录采集窗口、负载和绑核方式；空载和漂移负载不作为判据。

### Step 2：差分诊断

- 先排除容量 miss、带宽、锁竞争、NUMA 远端访问、频率和负载不均。
- 再区分伪共享和真共享：不同字段跨核读写 vs 同一字段多核读写。
- 无法区分时输出 `undetermined`，并给出下一步最小实验，不直接下结论。

### Step 3：字段归属映射

- 从热点函数回溯到结构，定位具体字段和缓存行。
- 字段偏移只说明相对位置；结合现场缓存行大小和样本对象地址核验所属缓存行。
- 记录每个字段的读/写、核归属、访问频次和共享范围。
- 标注序列化、反射、`unsafe`、ABI 和 GC 扫描约束，判断布局是否可改。
- 只有结构定义、无法证明实例起点对齐时，保留 `undetermined`。

### Step 4：假设验证

- 用不改最终方案的最小实验验证假设：单核 vs 多核、访问扰动、合成微基准、对照结构。
- 隔离共享写/独立副本会同时消除真共享；要归因伪共享，保持访问者不变，只改变两个字段是否同处一条缓存行。
- 每个实验写清预期、实际、反例和置信度。
- 实验结果不能复现或与其它证据冲突时，降级置信度，不把假设当结论。

### Step 5：优化点卡片与移交

- 输出结构化优化点卡片：结构/字段、争用类型、证据、置信度、反例、影响面、验证计划、下一跳 skill。
- 只描述“哪里有问题、证据是什么、下一步该谁处理”，不写具体修复方案。
- 同一结构存在多个争用点时分别建卡片，不合并归因。

### Step 6：终止与交回

- `confirmed`：仅 `high`；证据能落到具体结构/字段，且反例已排除。
- `inconclusive`：`medium` / `low`，证据冲突、字段归属不完整或实验不可复现；交回缺失证据和最小实验。
- `rejected`：更直接解释成立，交回排除理由。
- 没有缓存事件、只能看热点函数：`status=blocked_evidence`，只保留假设并给最小补采命令。
- 缓存事件可用但只有单一来源：`status=degraded`，降置信度并给出最小验证实验。
- 压测客户端/固定负载缺失：`status=degraded`，只给假设和最小验证计划。
- 缺少依赖、授权或 Go 基线：分别输出 `blocked_dependency` / `blocked_authorization` / `blocked_go_baseline`，不承诺收益。

## 识别原则

- 证据三角：硬件事件、profile、结构归属、受控实验至少两类一致才升级置信度。
- 先排除再确认：容量、带宽、锁、NUMA、频率和负载不均优先排除。
- 不把 cache miss 高等同于伪共享；不把 race detector 当伪共享检测器。
- 不在没有字段归属的情况下判断为伪共享。
- 识别阶段只给方向级建议，不展开具体实现，避免把方案偏好带入证据判断。

## 修复方向（仅思路）

识别确认后只给方向级建议；具体实现、参数、字段偏移和补丁由下游 skill 设计。

| 争用类型 | 大致方向 | 主要代价/风险 | 不展开 |
|---|---|---|---|
| 伪共享 | 缓存行隔离、字段重排、热冷分离、写路径 owner 化 | 结构变大、内存占用、序列化/反射约束 | 字段偏移、隔离粒度、补丁 |
| 真共享 | 分片、批量合并、延迟合并、只读快照 | 汇总开销、一致性语义、尾延迟 | 具体分片数、同步实现 |
| 容量 miss | 减少工作集、分块、调整遍历顺序、压缩 | 算法复杂度、精度/格式约束 | 具体块大小、重写代码 |
| 锁竞争 | 缩小临界区、分片、无锁/批量 | 正确性、复杂度 | 锁实现代码 |
| NUMA/远端 | 绑核、NUMA 亲和、实例布局 | 部署约束、资源隔离 | 具体绑核命令 |
| 频率/负载不均 | 环境修复、负载均衡、频率策略 | 运维约束 | 具体参数 |

## Finding Contract

每个 `findings[]` 至少包含：

- `finding_id`
- `structure` / `fields`
- `contention_type`：`false_sharing` / `true_sharing` / `capacity_miss` / `undetermined`
- `evidence[]`：事件、profile、实验和反例
- `confidence`：`high` / `medium` / `low`
- `counterevidence[]`
- `impact_scope`
- `validation_plan`：最小验证实验
- `remediation_direction`：方向级建议（如隔离/重排/分片/快照/绑核），不写字段偏移、参数或补丁
- `recommended_next_skill`
- `not_a_finding_reason`：`rejected` 时必填

## Candidate Action Contract

本 skill 的输出是 `findings[]`，不是执行动作。若上游编排要求 `candidate_actions`，按 finding 生成 `analysis_only` 动作：

- `action_id`：对应 `finding_id`
- `change_mode`：`analysis_only`
- `implementation_plan`：只写方向级建议和下一跳 skill，不写实现、参数或补丁
- `validation_plan`：最小验证实验
- `rollback`：不适用

## 输出字段

- `status`：`confirmed` / `inconclusive` / `rejected` / `degraded` / `blocked_evidence` / `blocked_dependency` / `blocked_authorization` / `blocked_go_baseline`
- `cacheline_evidence`
- `contention_classification`
- `hot_structure_inventory`
- `findings[]`（每个 finding 含 `remediation_direction`）
- `confidence_summary`
- `missing_evidence`
- `recommended_next_skill`

证据不足时输出对应的 `status` 和最小补采命令，不得仅凭经验承诺收益。

## 拒绝条件

完整拒绝条件见 `references/cacheline-contention-playbook.md` §10。关键三条：无缓存证据、无法区分伪共享/真共享、无法做字段归属或对照实验。

## 边界

- 只给方向级修复思路，不展开具体实现、参数、字段偏移或补丁；不写补丁、不改业务代码。
- 不定义构建/重编译/回退命令；需要时移交 `go-runtime-patch-build`。
- 不定义 A/B 协议；需要验证时移交 `microservice-ab-testing`。
- 不调整 sync.Pool 参数和 GC 参数；分别移交 `go-syncpool-optimization` 和 `go-runtime-tuning`。
- 不写机型信息、历史数据或其它环境的结论。
- 结论必须绑定本轮证据；证据不足时输出 `undetermined`。
