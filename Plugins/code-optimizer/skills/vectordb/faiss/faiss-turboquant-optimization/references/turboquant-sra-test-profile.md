# TurboQuant 测试配置映射

## 注册与入口

sra_test 的常见注册名为 `ivfturboquant`，对应目标常写为 `ivfturboquant_test`。先核对当前工程的注册表、构建目标、`--help` 和适配器；没有注册的版本需要另行集成，不能改用相似算法名代替。

| 字段线索 | 需要映射到的行为 |
| --- | --- |
| nb_bits / NBits | 目标量化器的数据精度 |
| qb / int_qjl / qjl_type | 实际 query 量化和校正模式 |
| nlist / nprobe | IVF 配置 |
| k_f / k_factor | 可选精排倍率 |

## 运行前检查

- 确认所有字段来自配置、命令行、索引默认值还是 SearchParameters 覆盖值。
- 记录包装索引的真实类型；保存与加载同一索引，避免对比中重复训练。
- 实际 metric、归一化、top-k、线程/查询批量、预热与循环次数写入 manifest。
- 只有适配器确实支持时才设置额外编译开关；核对头文件、库和字段解析的一致性。
- 查询工作量、QPS 计时范围和 Recall 公式从当前测试源码确认，不预填某一副本的公式。

具体数据集矩阵和参数由任务确定，示例配置不作为正式冻结值。公共操作见 `faiss-sra-test`，冻结字段见 [benchmark-contract.json](benchmark-contract.json)。
