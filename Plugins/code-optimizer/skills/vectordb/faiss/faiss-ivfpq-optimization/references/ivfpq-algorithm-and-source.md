# IVFPQ 算法与源码导航

以下是定位线索。文件、符号、模板和分派应在目标检出中确认；行号及可选后端不作为跨版本事实。

## 算法与索引

IVFPQ 通过粗量化把向量分到倒排列表，在每个子空间训练 PQ 码本。添加向量时编码原向量或残差；查询先选择列表，再查表估算距离并维护候选结果。
`IndexRefineFlat` 等包装器可对 base 结果精排，是否存在包装器由调用方决定。

| 层 | 常见源码入口 | 需要确认 |
| --- | --- | --- |
| 索引与粗量化 | `faiss/IndexIVFPQ.{h,cpp}`、`IndexIVF.{h,cpp}` | train/add/search、残差模式与实际 nprobe |
| 量化与解码 | `faiss/impl/ProductQuantizer*` | M、nbits、code_size、decoder |
| 查表与扫描 | `IndexIVFPQ.cpp` 的 QueryTables/scanner | 每查询/每列表 setup、selector、四码或其他扫描组织 |
| 内核分派 | `faiss/impl/code_distance/*` | generic 与架构实现、实际编译和运行选择 |
| 候选选择 | `KnnSearchResults`、`faiss/utils/Heap.h` | 比较方向、tie、更新次数和输出排序 |
| 可选精排 | `faiss/IndexRefine*`、`IndexFlat*` | 候选倍率、原向量距离与最终 top-k |

## 查询链检查

```text
调用方 / 可选包装索引
  → IndexIVF::search → 粗量化器选择列表
  → search_preassigned → scanner 的 query/list setup
  → 距离表准备 → 码字扫描与距离累加 → 结果容器
  → 可选精排 → 最终结果
```
核对实际实现中 coarse search 与 scan 的顺序、并行边界，以及显式 SearchParameters 是否覆盖索引保存值。
普通 PQ 通常使用浮点表和解码器；FastScan 的整数表与 packed block 另见对应技能。

## 扩展能力

自定义距离后端、额外 SIMD 距离文件、连续扫描特化、预筛或省略排序都可能是目标仓库扩展。只有确认目录、构建入口、调用点及数值语义后才纳入调用链；它们不是某个 Faiss 版本号所保证的能力。
