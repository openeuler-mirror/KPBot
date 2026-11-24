# RaBitQ 算法与源码导航

## 能力确认

先在目标检出中定位 RaBitQ 索引、量化器和测试注册。并非所有 Faiss 版本都提供相同类、位宽或 FastScan 变体。

## 定位入口

- 索引：`IndexRaBitQ*`、`IndexIVFRaBitQ*` 及实际存在的 FastScan 类。
- 量化器与辅助函数：`faiss/impl/RaBitQuantizer*`、`RaBitQUtils*`。
- 码字打包：实际 CodePacker/encoder/decoder。
- 架构内核：搜索 bitwise dot product、popcount、距离估计函数及其 dispatch。

## 流程

```text
训练：旋转/中心等模型参数 → 编码所需统计量
添加：向量变换 → 符号与可选额外位 → 校正因子 → 索引存储
查询：可选 IVF 粗量化 → query 预处理/量化
   → 位运算或点积距离估计 → 校正 → 候选选择 → 可选精排
```
根据真实调用链确认每项变换发生在每个 query、每个 list 还是每条码字。L2/IP、中心化、查询量化和多位码字可能使用不同公式，不能仅凭共用函数名合并分析。

## 证据重点

记录参数从调用方到 SearchParameters/DistanceComputer 的映射、码字长度与因子位置、位平面与尾部处理，以及实际选中的内核。源码事实绑定 commit/文件/符号；距离成本和收益由当前采样验证。
