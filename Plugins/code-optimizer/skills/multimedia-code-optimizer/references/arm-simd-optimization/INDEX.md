# ARM 指令与实现优化

算法与通用数据流查[媒体方法](../media-optimization-knowledge/INDEX.md)，循环、分块与融合查[共用方法](../media-optimization-knowledge/core.md)。

## 指令选择与数据排列

| 计算模式 / 瓶颈 | 具体手段 |
|---|---|
| 窄输入、宽累计、定点缩放 | [SVE2：偶奇扩宽、乘高位、成对累计](arm-sve2-methods.md) |
| 分段重排、双向量窄化、区间裁剪 | [SVE2.1：EXTQ/TBLQ、融合窄化、CLAMP](arm-sve2-methods.md#sve21-重排窄化与循环) |
| 多输出乘加、累加器 spill | [SME/SME2：ZA 外积与多向量组](arm-sme-methods.md) |
| 固定宽度运算、交错访存 | [NEON：加载、重排、宽累计与打包](arm-neon-methods.md) |
| 平缓整数表、SGR 系数查表 | [NEON：TBL、USHL 与 CNT 压缩查表](arm-neon-methods.md#差分位图压缩查表) |
| 字节预测差、向零截断除二 | [比较修正与 UHSUB 窄半差](arm-neon-methods.md#向零截断的字节半差) |
| 已有四位权重的点积 | [高半字节 SDOT 与末尾 SCVTF 缩放](arm-neon-methods.md#四位权重高位点积) |
| 任意长度、条件统计、间接索引 | [SVE：谓词、累计、gather/scatter](arm-sve-methods.md) |
| 标量乘加、条件选择、位操作 | [MADD、CSEL、CCMP、CSSC](scalar-and-codegen.md) |

## 向量化与编译

| 计算模式 / 瓶颈 | 具体手段 |
|---|---|
| ISA 分派、VL、目标参数、向量化诊断 | [ARM 编译与分派](compiler-vectorization.md) |

## 硬件资源与访存

| 计算模式 / 瓶颈 | 具体手段 |
|---|---|
| 多输出累计、寄存器压力、streaming 状态切换 | [SME/SME2：ZA 复用与执行状态](arm-sme-methods.md) |
| 寻址、交错访存、预取、非临时访问与尾部 | [ARM 访存指令](memory-and-cache.md) |
| 原子更新与同步 | [LSE、排他访问、acquire/release](multicore-and-atomics.md) |
