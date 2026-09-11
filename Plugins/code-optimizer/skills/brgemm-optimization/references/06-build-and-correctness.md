# 阶段 6：编译与正确性门槛

本阶段是性能测试前的强制门槛。使用独立构建目录完成增量构建，再执行项目规定的格式、静态检查和完整测试；通过 verbose 或链接信息确认测试加载的是新库，避免静态链接旧产物。

## 数值矩阵

- plain/direct B、plain/PackB、blocked N32、blocked N64/compute N32。
- M/N/K tail、batch broadcast、batch×M 合并、nStart=32/64 跨物理块 offset 与 K tail。
- alpha/beta，尤其 `beta=0` 不读 dst；bias、SUM、eltwise、scalar/per-N/broadcast binary、PReLU。
- per-N product scale、common destination scale、非连续 C gather/scatter。
- parallel-K partial/reduction、PackB 单槽覆盖、跨 work 复用。
- scratch 重复执行、同一对象并发 lease、外层并行退化。

结果对标量 reference 逐元素比较，按项目数值范围设定约 `1e-4` 相对容差。测试 dst 以 NaN 初始化，专门证明 `beta=0` 不计算或读取旧值。

## 调度与框架断言

为每个 family 固化 `n_blk/k_blk/m_blk/nthr_k`。框架侧覆盖全部目标 shape × `{plain,prepack}` × 目标线程数，并要求 failed=0；运行完整 gtest，不能只跑新增用例。

## 失败处理

构建、静态检查或正确性任一失败都返回对应实现阶段修复，不得进入性能阶段。记录编译器、构建选项、ISA、依赖版本、测试命令和结果摘要；对预期跳过项写明理由，不能把失败改成 skip 来通过门槛。
