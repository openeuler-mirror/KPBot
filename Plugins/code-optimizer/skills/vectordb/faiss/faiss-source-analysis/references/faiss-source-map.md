# Faiss 源码导航

## 分层

| 层 | 常见位置 | 检查目标 |
| --- | --- | --- |
| 索引 API | faiss/Index*.h、*.cpp | train/add/search 与包装关系 |
| 编码与布局 | faiss/impl/ | quantizer、packer、DistanceComputer |
| 距离与 SIMD | faiss/utils/、faiss/impl/ | 标量/架构实现与分派 |
| 倒排列表 | faiss/invlists/及 IndexIVF* | code/ID 存储、列表和 scanner |
| 测试/基准 | tests/、benchs/或外部适配器 | 实际实例化类型、配置和计时边界 |

## 算法入口

IVFPQ 从 IndexIVFPQ 和 ProductQuantizer 定位；FastScan 从 IndexIVFFastScan、IndexIVFPQFastScan 和 pq4 内核定位；HNSW 从 IndexHNSW、impl/HNSW 及 storage 定位。RaBitQ 和 TurboQuant 先搜索目标版本支持的类型与枚举，再追踪量化器和距离计算。

## 调查方法

从测试/应用实际创建的索引开始，展开包装器和参数覆盖，再进入目标 API、模板分派和内核。记录文件、符号、行号及 commit，使用实际检出的声明核对字段和默认值。
某个版本号不保证额外后端、特化内核或优化宏存在。跨版本复用导航思路，不复用行号、固定宏集或性能结论。
