# 性能工程方法论域

跨组件 / 全栈系统（尤其是含 LLM / embedding 等不确定上游、负载不可自然复现的系统）的
**测量驱动优化方法论**。本域的技能不绑定具体产品，回答的是"怎么把『感觉慢』变成『可复现、
可量化、可证伪、可溯源』的优化闭环"：接手 + 锁基线 → 阶段门控采集 → 瓶颈挖掘 →
候选证伪 → 最小实现 → 量化决策 → 溯源复验，七个阶段一一对应一个原子 skill。

## 与既有 pipeline 的边界（避免重复）

本域与 `kpbot-code-optimizer` 的既有 pipeline（`prepare-project` / `analyze-hotspot` /
`decide-optimization` / `apply-optimization` / `verify-optimization` / `adversarial-review`）
**互补不重叠**：

| 维度 | 既有 pipeline | 本域（perf-engineering） |
|---|---|---|
| 优化对象 | C/C++/Rust 单一热点函数（循环、向量化、访存） | 跨组件全栈系统（框架 + 驱动 + 存储，多种运行时） |
| 负载特性 | 确定性微基准 | 含不确定上游（LLM/embedding），需缓存回放冻结 |
| 判定重点 | 编译 + 正确性用例 + 微基加速比 | 请求计数逐位一致（防假提速）+ 语义正确性 + 尾延迟代价 |
| 瓶颈语言 | 反汇编 / 端口压力 | 组件→进程→函数分层 + self/cum + 算力/带宽分叉 |

本域七个阶段补的是既有 pipeline **未显式产品化的那部分**——系统性测量纪律（相位门控、采集/计时分离、
证伪优先、溯源闭表，以及含不确定上游负载的缓存回放冻结）。两者可组合：先用 `opt-onboarding-and-baseline`
锁住全栈基线，再用 `opt-bottleneck-mining` 定位到某个 C++ 内核后，把该内核交给 `kpbot-code-optimizer` 的
`analyze-hotspot` / `apply-vectorization` 做函数级优化。

### 与 agent-memory-db-optimization（顶层枢纽）的关系

本域与该枢纽是**方法论 vs 单案例落地**的关系，互补且互指，不重叠：

- `agent-memory-db-optimization` = **记忆/图+向量后端**单案例的诊断方法（冻结确定性负载 → 三段归因 →
  瓶颈建模 → 分层杠杆，标定 Graphiti+Neo4j 与 mem0+Milvus）。
- 本域 = **测量纪律的跨产品泛化**：把"缓存回放冻结模型调用、请求计数防假提速、负结果记账、相位门控采集"
  这些枢纽案例里出现的纪律，抽成不绑定具体后端的原子阶段/横切 skill。

同一条纪律（如"冻结含不确定上游的负载"）在枢纽里是案例步骤，在本域是独立可复用的 `opt-onboarding-and-baseline`。
导航时：诊断某个具体记忆后端 → 走枢纽；把测量/证伪/溯源纪律套到任意全栈系统 → 走本域。

### 与 pipeline 第一步 prepare-project 的差异

`opt-onboarding-and-baseline`（本域 0→1）与 pipeline `prepare-project`（「验证项目状态、定位优化目标函数、
建立性能基线」）职责相近但侧重不同：本域面向**无交接文档的全栈系统**，交付环境预检 + 负载理解 + 全栈规格表 +
规格临界判定（含 NUMA/磁盘/外部容器约束）；`prepare-project` 面向**代码/函数级优化**，先定位到目标函数再建
函数级基线。前者先锁全栈、后者先锁函数。

## 成员

> **占位 · 当前 0 成员**。本域成员随各自独立 PR 逐个迁入，迁入后再改为可点击链接并补职责说明。

| Skill（阶段） | 职责 |
|---|---|
| _（待迁入）_ | — |

### 本域后续迁入（逐个独立 PR，均只动各自的 `skills/perf-engineering/**`）

主链七阶段：

- `opt-onboarding-and-baseline`（0→1）—— 接手无交接文档的系统：环境预检、负载理解、锁定可复现基线四件套、规格临界判定
- `opt-measurement-infra`（1）—— 阶段门控采集基建：结构化阶段标记契约、按相位起停采集、采集/计时分离、优雅降级/停止
- `opt-bottleneck-mining`（2）—— 组件→进程→函数分层下钻，self/cum 判据，多源交叉验证，热点换算为算力/带宽/分配类型
- `opt-candidate-falsification`（3）—— 候选六字段 + 证伪优先 + 便宜证据阶梯，先排除不成立的再投入重成本验证
- `opt-minimal-implementation`（4）—— 最小变量面落地：语义等价逐条论证、两侧数据接口对齐、补丁式落库、只换二进制复用基座
- `opt-quantify-and-decide`（5）—— N 轮中位对比 + 防假提速两层铁证 + 尾延迟代价量化 + 保留/放弃决策固化
- `opt-traceability-and-reverify`（6）—— 溯源闭表、归档分层、负结果记账、等价重构实跑复验、单一事实源与契约化
- `opt-bottleneck-migration`（横切）—— 热点迁移认知：优化只搬走瓶颈，加速后必须重 profile，"仍可见"≠"还能挖"，路径过时收益为零

横切专题（从阶段中抽出、可独立复用）：

- `opt-anti-fake-speedup` —— 防"假提速"两层铁证：请求计数逐位一致 + 语义正确性核验，单一硬指标当锚，等价重构实跑复验
- `opt-clean-attribution` —— 干净归因：识别混杂因素（二进制体积/.text 段、单轮方差、构建路径差异），无法归因就诚实降级

推荐串联：`onboarding → measurement-infra → bottleneck-mining → candidate-falsification →
minimal-implementation → quantify-and-decide → traceability-and-reverify`。

## 成员登记纪律

- 新增成员只在本文件登记，不改 `skills/kpbot-code-optimizer/SKILL.md` 的全局路由表、不改插件 `README.md` 总表。
- 目录约定：`skills/perf-engineering/<skill>/SKILL.md`；本域目录恒带 `DOMAIN.md`。
- 标定案例来源：mem0 + Milvus 联调优化项目（mem0 2.0.14 + Milvus 2.4.5，鲲鹏 aarch64），
  数字是**收益分布形态**，不是对其它系统的收益承诺。

## Review

- owners: []（待指派，填写 GitCode 用户名）
- 评审范围：`Plugins/code-optimizer/skills/perf-engineering/**`