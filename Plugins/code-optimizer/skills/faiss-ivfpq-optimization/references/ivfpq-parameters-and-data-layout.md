# IVFPQ 参数与布局

## 参数分层

| 层 | 参数 | 需要核对 |
| --- | --- | --- |
| 索引 | nlist、M、nbits、by_residual | 训练/编码配置与保存索引的实际值 |
| 查询 | nprobe、metric、top-k | 每次调用的 SearchParameters 和索引默认值 |
| 查表 | use_precomputed_table | 请求模式、实际模式、表尺寸与每列表 setup |
| 包装器 | k_factor 或适配器 k_f | base 候选数及最终精排规模 |

## 码字与表

ProductQuantizer 把 d 维分成 M 个子空间；常见均分布局要求 d 可被 M 整除。每个子空间有 `2^nbits` 个中心，紧凑码字大小通常为 `ceil(M*nbits/8)`，以实际 code_size 与 decoder 为准。8 位码字可逐字节解码，其他位宽须核对位打包和跨字节访问。
距离表通常包含 M×2^nbits 个表项；残差和预计算模式会改变表的生成、偏置和复用方式。记录实际缓冲布局、浮点类型和尾部路径。

## 性能与正确性

M、nbits、nprobe、候选倍率共同影响工作量与精度，纯实现对照冻结这些值。局部特化应限定 decoder/形状/metric/selector，保留原路径；累加顺序、偏置、heap 比较和精排输入必须符合数值契约。
可选后端的压缩缓存、降低精度或过滤层单独记录，未出现的能力标记为不适用。
