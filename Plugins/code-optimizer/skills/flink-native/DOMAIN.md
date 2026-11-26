# Flink Native 向量化执行域（flink-native）

Flink SQL 流作业在 native 向量化执行栈（Flink planner 影子翻译 + velox C++ 执行引擎 + 跨语言
数据桥）上的**算子与执行层**性能优化技能。覆盖：有状态算子（Top-N/去重/聚合/Join）的状态形态
优化、跨语言数据桥传输开销消除、原生语义对照、以及改码收益的行为冻结核验。

技术栈说明：Flink 是 Apache 开源项目；velox 与 velox4j 是开源项目；gluten（含 gluten-flink
模块）是 Apache 孵化开源项目。本域技能面向**修改这些仓库源码**的优化工作。

## 成员

| Skill | 职责 |
|---|---|
| [`flink-native-stateful-operator-tuning`](flink-native-stateful-operator-tuning/SKILL.md) | 有状态流算子（Top-N/去重等 keyed 算子）在向量化引擎上的端到端性能优化：状态值形态与驻留审计（批式向量引用结构导致内存爆炸的机制与修复）、跨语言传输开销归因、三层拆账（算子/传输/内存）、分阶段实施与 changelog 行为冻结核验；references 承载架构、计划节点契约、语义对照与网络边序列化机制 |
| [`flink-native-jni-transport-tax`](flink-native-jni-transport-tax/SKILL.md) | JVM↔native 数据桥的通用传输税诊断与消除：引用结构未实体化（gather 欠账）、编码税（Base64 类）、字符串拷贝链、小批固定开销四类反模式的账单法定位与逐税消除；适用于任何跨语言混合引擎，附 Flink×velox 案例实录 |

## 与其它域的边界

- **框架层配置**（TaskManager/JobManager 内存配比、并行度、table.exec.\* 参数、集群资源配置）→
  `app-tuner` 侧 `skills/bigdata/` 域；本域只管改源码。
- **基准口径**（nexmark 类查询集双端对比协议、同日锚定纪律）→ app-tuner 侧
  `skills/bigdata/flink-native-bench-protocol`；本域 skill 只引用该协议做改码验证。
- **JVM JIT / 纯 Java 热点**（不涉及 native 边界的 Java 代码优化）→ 域外既有 skill
  （`kptools-jvmjit` 采样 + 通用优化技能）。
- **纯 native 内核优化**（不涉及流语义与跨语言桥的 velox 批式内核）→ 域外通用优化技能
  （向量化/循环展开等）；本域只管"流式语义 + 跨语言边界"叠加的问题。

## 成员登记纪律

- 新增成员只在本文件登记，不要改 `skills/kpbot-code-optimizer/SKILL.md` 的全局路由表——
  共享行会让并行 PR 必然冲突。
- 目录约定：`skills/flink-native/<skill>/SKILL.md`；域目录必须带 `DOMAIN.md`
  （安装器据此识别为分组）。
- 引用纪律：锚点一律用**类名/方法名/机制描述**（可 grep 定位），不写行号、不写 commit
  hash——上游 rebase 与分支演进会让行号和 hash 立即失效；量化收益一律标注测量条件与
  口径（自测/厂商/独立验证）。

## Review

- owners: []（待指派，填写 GitCode 用户名）
- 评审范围：`Plugins/code-optimizer/skills/flink-native/**`
- 判定口径：路径即归属——改动落在本域目录内由本域 reviewer 评审；跨域改动另行指定。
