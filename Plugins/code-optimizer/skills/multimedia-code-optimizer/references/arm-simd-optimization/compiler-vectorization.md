# ARM 编译与分派手段

循环变换见[共用方法](../media-optimization-knowledge/core.md#改写循环以形成向量数据流)。

## 按扩展选择内核

通用入口以 baseline ISA 编译，NEON 可选扩展、SVE2、SVE2.1、SME 内核分别编译，再通过函数表分派。

Linux 使用 `getauxval(AT_HWCAP/AT_HWCAP2)`：SVE2 对应 `HWCAP2_SVE2`，SVE2.1 对应 `HWCAP2_SVE2P1`，SME/SME2 对应 `HWCAP2_SME` / `HWCAP2_SME2`。SVE I8MM 用 `HWCAP2_SVEI8MM`，NEON I8MM 用 `HWCAP2_I8MM`；LSE 用 `AT_HWCAP` 中的 `HWCAP_ATOMICS`。

## 目标与代码生成

| 目标 | 具体改法 |
|---|---|
| 针对确定 CPU 生成指令 | `-mcpu=<cpu>` 同时选择扩展和调优目标；只在构建机运行时可用 native |
| 保持基线 ISA，改变调度目标 | `-march=<baseline> -mtune=<cpu>`；mtune 不单独开放新指令 |
| 独立 SVE2 / SVE2.1 内核 | 例如 `-march=armv9-a+sve2` / `-march=armv9-a+sve2p1`，高 ISA 选项只用于对应实现 |
| 独立 SME / SME2 内核 | 例如 `-march=armv9-a+sme` / `-march=armv9-a+sme2`，同时声明 streaming/ZA 属性 |
| VLA 可伸缩循环 | 用 `svcnt*` 推进；Clang 自动向量化可用 `#pragma clang loop vectorize_width(scalable)` 表达偏好 |
| 固定线程 VL 的专用实现 | `-msve-vector-bits=<bits>` 允许固定长度优化；执行线程必须满足对应 VL，普通分派仅检查 ISA 不够 |
| 核验是否得到预期向量循环 | Clang 用 `-Rpass=loop-vectorize -Rpass-missed=loop-vectorize -Rpass-analysis=loop-vectorize`；GCC 用 `-fopt-info-vec-optimized -fopt-info-vec-missed`；再检查目标指令和 spill |
