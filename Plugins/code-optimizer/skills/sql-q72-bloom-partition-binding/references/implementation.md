# Bloom 绑定实现与消费者契约

## 源码线索与来源

原 SQL72 包的源码线索为目标工程 `src/gluten/cpp/velox/compute/WholeStageResultIterator.cc` 中的 `bloomProbeField`、`findBloom`、`findCompiledBloom` 及 `HiveTableHandle::remainingFilter`。此路径含工程外层目录，函数可能属于私有补丁；源包未给可确认的仓库提交。复用时记录目标仓库、提交、补丁和真实相对路径，不把这些名字当作已在官方某版本存在的保证。

已读取 KPBench 相对路径 `velox/velox/src/gluten/tools/gluten-it/common/src/main/resources/tpcds-queries/q72.sql`，SHA-256 为 `186d7f086c498743f59c26f6fef950f7ecc06f3b4dfc962e11be915a751766bd`。模板提供销售日期与日期维表关系，不能证明 Bloom 已进入 NativeFilters；原生表达式与分区绑定仍须从实际最终计划取证，不依赖本机模板路径运行。

## 生产者输出

描述符包含：scan_id、分区列句柄/名称、source_bits、hash_bits、常量 seed、原 Bloom 常量、编译后的原探针、execution_id 与拥有方。INTEGER→BIGINT 时 source_bits=32、hash_bits=64；这两者必须分别保留。用目标转换器按 source_bits 解析分区文本，再交原探针求值。

只沿顶层连续 AND 提取独立必要条件。AND 内有 OR 时不能进入 OR 找一个 Bloom；其他合法 AND 兄弟仍可独立作为候选。UDF、动态种子、非确定性或不支持转换不产出描述符。对同名列核对扫描、别名与原列句柄，拒绝把数据列当分区列。

## 与提交前剪枝衔接

消费者核对 scan_id、execution_id、格式与 split 分区元数据，再复用描述符持有的原探针。只允许成功求值且返回非 NULL false 时拒绝；描述符缺失、失效或解析失败时走原提交。提取和编译不应每个 split 重复进行，原表达式与常量须覆盖任务生命周期。

本地用例的 dict AST 是类型/必要条件模型，函数身份通过限定 fixture 节点表示，不解析 Substrait、不调用真实编译器或 Bloom。目标端需要保存编译前后表达式、列句柄、常量和绑定失败日志；不能以本地模型替代真实原生绑定证据。
