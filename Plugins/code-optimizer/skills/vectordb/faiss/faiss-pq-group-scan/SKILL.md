---
name: faiss-pq-group-scan
description: 把 Faiss 8-bit PQ 逐码距离改写为"码即 lane"的分组扫描核：codes 转置、gather 取表、多组独立累加、组宽与启用阈值、尾部回退。适用于逐码水平归约或查表随机访存成为瓶颈的扫描路径；4-bit 打包累加见 faiss-pq-accumulate-kernel。
---

# Faiss 分组扫描核

## 输入

M/ksub、code_size、sim_table 布局与足迹（字节/元素、是否超 L1）、列表长度分布、当前每码成本（归约、寻址、查表占比）、结果处理方式与过滤要求。

## 执行步骤

1. 量化现状：确认每码路径的成本构成——一次跨 lane 水平归约、逐子量化器的表访问、解码与写回；用 `faiss-feature-capture` 的分段计时分离 setup/主循环/尾部（尚未并入子域时按 `faiss-perf-profile` 的采样与归因口径获取同类字段）。
2. 设计布局：每组 R×L 个码（L = f32 lane 数），每码占一条 lane，M 维迭代中一次 gather 取 L 个码的表项；见 [布局与转置](references/group-scan-layout.md)。
3. 构造转置：把连续码按（子量化器, lane）重排进 scratch；整 tile 用向量 zip 网络，残段用标量回退。
4. 组宽与阈值：按 [阈值与尾部](references/threshold-and-tail.md) 实测"收益–列表长度"曲线取拐点；低于阈值回退原逐码路径。
5. 等价性：先核对基线路径的实际浮点归约顺序（如 AVX2 8-bit PQ 为水平归约，与按 m 升序逐项累加的舍入不同）；要求逐位一致时分组核保留基线的归约顺序，声明容差时给出容差值并单独验证近并列输入下 Top-K 的 ID 与距离。写回保持 j 升序。
6. 写回与统计：逐码写回保持 j 升序；扫描计数等统计语义不变。

## 输出

布局与 scratch 足迹、转置构造、组宽/阈值表、等价性说明、回退路径与验证矩阵。

## 约束

- gather 索引由码字节加载并零扩展；索引取值域由 ksub 与码宽保证，不得出现越界。
- scratch = R × tile × L 字节，与 LUT 竞争缓存时必须一起测量。
- 阈值以下与尾段的回退路径须与原实现逐结果一致。
- 组宽展开以寄存器与代码体积为上限；收益曲线平顶即停止加组。

## 参考资料

- [布局与转置](references/group-scan-layout.md)
- [阈值与尾部](references/threshold-and-tail.md)

候选排序与止损用 `faiss-optimization-selection`；执行对照用 `faiss-benchmark-run`（尚未并入子域时按 `faiss-benchmark-baseline` 的对照口径执行并记录来源）。
