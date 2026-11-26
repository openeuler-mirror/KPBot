# 提交前剪枝实现与证据

## 源码线索

原 SQL72 包指向目标工程 `src/gluten/cpp/velox/compute/WholeStageResultIterator.cc` 中的 `SplitBloomPruner::rejects` 和 `ConnectorSplit` 创建边界。路径可能包含工程包装层，函数可能来自未提供提交的补丁；实施前记录实际仓库、相对路径、提交与补丁。源码编译或 libvelox.so 中存在符号，只证明构建包含候选，不证明运行命中。

已读取 KPBench 相对路径 `velox/velox/src/gluten/tools/gluten-it/common/src/main/resources/tpcds-queries/q72.sql`，SHA-256 为 `186d7f086c498743f59c26f6fef950f7ecc06f3b4dfc962e11be915a751766bd`。模板不能证明真实存储分区、Hive Parquet 格式或 split 已在提交前被拒绝，这些均以目标环境取证为准；运行本 skill 不要求该本机路径存在。

## 输入绑定与失效

接收原生必要条件绑定：scan_id、列句柄/名称、source_bits、hash_bits、常量种子、Bloom 常量、原编译表达式、execution_id/拥有方。绑定可由 `sql-q72-bloom-partition-binding` 的工作流或目标工程已有等价验证生成；本 skill 可独立消费已有有效绑定，不要求调用另一个脚本。

原列为 INTEGER 而探针扩宽到 BIGINT 时，按 32 位解析分区值，之后才执行原探针内部扩宽。缺值不是 0。新任务、扫描替换、常量/上下文销毁后应使绑定失效，避免跨任务使用过期探针。

## 保守执行与任务协议

对成功 false 跳过，其余沿原提交流程；回退仍计入实际提交清单。保留完整 split 对象或等价的所有元数据，不能丢失文件范围、分区键或安全属性。全部 split 被剪掉时也遵守任务结束/无更多输入信号，防止空任务挂起。

按目标接口列出可恢复的表达式异常类型，再回退到原提交；任务取消和内存不足等错误保持原协议传播。本地模型把 ValueError、RuntimeError 和 OverflowError 作为可恢复 fixture，MemoryError 应向外传播，这不是对 C++ 异常分类的通用定义。空提交清单只验证模型结果，不能代替引擎终止信号和资源释放的目标端用例。

本地模型的 `submit` 返回原 split 对象以核对元数据，注入三值探针并检查 int32/int64 范围、异常和绑定失效；它不会打开 Parquet，也不会运行真实 Bloom。原 skill 的 OR/函数/转换资格检查已迁到生产者；两者联调用例需证明有效描述符可消费、无效提取导致原提交。

## 性能证据

原包 SQL72 阶段性数字包含多项变化，缺少关闭本项的配对及实读统计，拆分技能不继承单项加速比。目标端应记录尝试/跳过/提交/打开 split、实际字节、初始化与求值耗时、完整结果及库指纹，无命中场景也提交成本数据。
