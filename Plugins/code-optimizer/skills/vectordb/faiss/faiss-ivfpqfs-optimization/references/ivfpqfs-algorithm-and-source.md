# IVFPQ FastScan 算法与源码导航

## 核心区别

普通 IVFPQ 按码字读取 PQ 索引并累加距离；FastScan 将低位宽码字按块打包，以整数 LUT 和 SIMD 查表提高批量扫描吞吐。具体支持的位宽、块大小和 implementation 以目标源码为准。

## 定位入口

- `faiss/IndexIVFPQFastScan*`：PQ 训练、转换索引和 LUT 计算。
- `faiss/IndexIVFFastScan*`：块索引、查询组织与 implementation 分派。
- `faiss/impl/pq4_fast_scan*`：PQ4 打包、LUT 重排和块累加。
- `faiss/impl/simd_result_handlers.h`：heap/reservoir 或其他结果处理器。
- `faiss/utils/simdlib*`：架构对应的查表、累加与仿真实现。

## 查询链检查

```text
可选精排包装器 → IVF 粗量化 → implementation 分派
  → float LUT → 量化/偏置 → 按 query block 打包 LUT
  → packed code 块累加 → result handler → 可选精排
```
搜索 `compute_LUT`、`compute_LUT_uint8`、`pq4_pack_LUT`、`kernel_accumulate_block` 等实际符号。不同 implementation 的并行与缓冲策略可能不同，自动选择结果须单独记录。

## 架构和扩展

查看实际 SIMD 封装与编译目标，确认执行 NEON、AVX 或其他路径。额外重打包、过滤和精排后端是可选能力，不能由类名、处理器品牌或版本推断。验证用仿真路径是否存在及其适用范围也应从源码确认。
