# 算法构建能力检查

构建目标和测试注册从实际源码/适配器确认，不按版本号或算法名预设扩展后端。
| 算法 | 源码能力检查 | sra_test 常见目标 |
| --- | --- | --- |
| IVFPQ | IndexIVFPQ、ProductQuantizer 及实际 decoder | ivfpq_test |
| FastScan | IndexIVFPQFastScan、packed block 和 SIMD 内核 | ivfpqfs_test |
| RaBitQ | 实际 RaBitQ 索引、quantizer 与位宽支持 | ivfrabitq_test |
| TurboQuant | 实际量化器枚举、MSE/QJL 或对应实现 | ivfturboquant_test |
| HNSW | IndexHNSW、图结构和 storage | hnsw_test |

测试程序的宏、Faiss 的 CMake 选项和库的实际编译定义是不同层，需分别核对。附加后端若不存在，不作为构建前置条件；若存在，记录启用方式、ABI 及相关数值模式。
缺失字段从 CMakeLists、缓存、编译数据库、头文件和注册表取证；无法确认时说明缺什么，不能借用另一个工程的配置补齐。
