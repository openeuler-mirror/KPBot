# 测试算法路由

| 索引 | 常见注册名 | 常见构建目标 |
| --- | --- | --- |
| HNSW | hnsw | hnsw_test |
| IVFPQ | ivfpq | ivfpq_test |
| IVFPQFastScan | ivfpqfs | ivfpqfs_test |
| IVFRaBitQ | ivfrabitq | ivfrabitq_test |
| TurboQuant | ivfturboquant | ivfturboquant_test |
这些名称是定位线索。使用前检查当前注册工厂、适配器和构建目标。缺少目标时报告支持情况，不能通过另一个名称运行不同算法并标作原目标。
跟随适配器的实际索引构造、包装器、字段解析与宏条件，确认配置最终到达的 API。索引默认参数、配置值和运行时 SearchParameters 分别记录。
