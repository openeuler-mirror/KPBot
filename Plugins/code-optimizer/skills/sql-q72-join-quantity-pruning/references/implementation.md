# 库存数量上界预筛的源码定位

来源为用户 SQL72 包中的实施摘要；本次本地整理未复跑业务引擎。下列 `src/velox/` 是目标工程的包装路径，函数可能来自业务补丁。原包未提供可完整复现的仓库提交、补丁和原始日志，迁移时先补齐这些信息；历史数字不作为本轮验收成绩。

## 查询来源

已读取 KPBench 根目录下 `velox/velox/src/gluten/tools/gluten-it/common/src/main/resources/tpcds-queries/q72.sql`，SHA-256 为 `186d7f086c498743f59c26f6fef950f7ecc06f3b4dfc962e11be915a751766bd`。模板包含商品等值连接与 `inv_quantity_on_hand < cs_quantity`；它不能证明目标计划的 build/probe 方向或候选已命中。路径是来源线索，不是运行 skill 的本机路径依赖，实际 SQL 与最终计划优先。

## 调用链与适用条件

SQL72 最终计划的 `ShuffledHashJoinExecTransformer` 按 `cs_item_sk = inv_item_sk` 执行内连接，`BuildLeft` 选中销售侧。原生 `SubstraitToVeloxPlan` 生成 `HashJoinNode`，`LocalPlanner` 选用 `HashBuild` 与 `HashProbe`。验证目标版本的左右方向后再设置预筛。

| 源码入口 | 需要核对 |
| --- | --- |
| `src/velox/velox/functions/sparksql/Comparisons.cpp` | `ComparisonFunction::getCanonicalName` 仅为目标 `INTEGER` 小于比较提供 `kLt` 身份 |
| `src/velox/velox/exec/HashProbe.cpp` | `HashProbe::initializeFilter` 构造 `HashProbeFilter`，检查函数身份、默认 NULL 语义及直接列绑定 |
| `HashProbe::asyncWaitForHashTable` | 就绪后调用 `prepareBound`，跨全部行容器取最大值，检查待恢复 spill 和重复建表键 |
| `HashProbe::addInput` | `prune` 与当前 `activeRows_` 相交，位于常规 `prepareForJoinProbe` 前 |
| `HashProbe::evalFilter` | 保留配对后的原条件，保证键、剩余谓词、输出重数不变 |

第 7 轮引入数量上界，随后第 8 轮加入正式的函数身份校验等收窄条件。迁移时按最终版本的准入实现。原实现中，上界是当前完整哈希表所有商品的最大有效销售量。遇到 NULL 建表值时忽略，该值无法使严格 `<` 为 true。所有建表值均为 NULL 时可以不启用预筛；SQL72 保留 `INT32_MIN` 初值，对无 NULL 的 `int32` 输入可安全排除，因为任意值都不小于它。尚未覆盖的 spill 分区不能按全 NULL 处理。

## 反例与成本口径

普通 `=` 不匹配两个 NULL 商品键，数量预筛不能改变这一点。本地参考连接显式排除 NULL 键，再用独立 SQLite JOIN 对照；SQLite 不证明原生类型绑定、SIMD 或 spill。连续给同一模型传入上界 20 和上界 5 的完整表，后一次只能使用新上界；原生实现另须通过哈希表替换、状态清理及 spill 恢复测试。

销售建表数据为 `(商品 A, 10)`、`(商品 A, 10)`、`(商品 B, 3)`。`(A, 10)` 可预筛，`(A, 9)` 匹配两行，`(B, 9)` 虽低于全表上界仍不通过逐对过滤。局部用例核对键缺失、重复行与等号边界。生产路径按 32 值抽样，在预期淘汰较多时才执行。上界扫描可能每个探测算子实例都进行，按实际次数计成本。

SQL72 第 6 轮候选中位耗时 93.488 秒，第 8 轮收窄准入后的候选耗时 41.103 秒。两轮都是各自与冻结基线交错，不能直接当成第 1 项的独立配对节省。第 16 轮最优构建 `5e7a20c` 的 `99.496 → 36.450` 秒和 100 行完全一致是整套补丁的证据。
