---
name: milvus-sve-hnsw-kernel
description: >
  Milvus/Kunpeng 的 SVE HNSW 距离内核：确认 `fvec_L2sqr_sve_pf` 走 256-bit SVE、预取距离、构建开关与
  符号验证，并用"算力利用率 vs 峰值"判据理解"带宽受限"结论为何否决并行化/双累加器。
  适用：要确认 SVE 内核是否生效（whilelo 符号）、要重编 knowhere、要确认 SIMD 分发链（NEON→SVE）、
  或想给 L2 距离加并行/展开/预取但需要先判断是否值得（带宽受限）。
  不适用反例：瓶颈在 Go 分配或框架侧过滤（那是同域其它专项/相邻域的职责，本 skill 只覆盖 C++ 距离内核）；
  或纯 NEON/标量内核不涉及 SVE 分发。
---

# Milvus SVE HNSW 距离内核（标定案例）

## 何时使用

- 要确认 Milvus 的 L2 距离真的走了 SVE 而不是 NEON/标量。
- 要在 Kunpeng 上重编 knowhere 并验证 SIMD 产物。
- 想给 `fvec_L2sqr` 加并行/SIMD 宽度/预取，需要先判"值不值得"。

## 核心原则

> **来源边界**：`fvec_L2sqr_sve_pf`、`hook.cc` 的 `__ARM_FEATURE_SVE` 分发与 CMake 的
> `COMPILER_SUPPORTS_SVE` 均来自私有补丁 `0001-hnsw-scann-sve-pf`；上游 knowhere v2.3.5（Milvus
> 2.4.5 固定版）的 ARM 分支只有 `#if defined(__ARM_NEON)`、`distances_neon.cc` 无一行 SVE 代码。
> 因此下面的"验证三件套"与"确认 SVE 生效"只对**打了该补丁的构建**有意义；对 stock Milvus 2.4.5
> 执行会得到 whilelo 零命中 + CMakeCache 无该变量 + nm 无该符号——应判"未打补丁"，而非"构建损坏"，
> 也不要据此误以为 SVE 是 Milvus 上游自带能力。

1. **内核现状**：`fvec_L2sqr_sve_pf`（`distances_neon.cc`）是 **SVE 256-bit**
   （`svcntw()=8` lanes，`/proc/sys/abi/sve_default_vector_length=32`），预取距离 `0x200=512B`
   （`PLDL1KEEP_OFF`），**单累加器**。
2. **分发链（静态证据 → 结论只到"构建产物含 SVE 实现"）**：`fvec_L2sqr_neon` → `fvec_L2sqr_sve_pf`
   （`hook.cc`，`__ARM_FEATURE_SVE` 下）；编译 `-O3 -march=armv8-a+sve`；hnswlib 自带 AVX/SSE 路径已被
   `#if 0` 禁用。**这些是静态源码证据**：库同时含 NEON 和 SVE 实现时，即便实际查询走 NEON，静态检查也照样
   通过。要证"实际查询命中 SVE"，需对目标 milvus 进程做**一次 L2 查询**采样调用栈 / 记录分发目标。
3. **"带宽受限"是待验证假设，不是已确认结论**：~2.7 GFLOP/s/核 ≈ 理论峰值 ~36 GFLOP/s 的 ~8%（口径：
   维度 1024（seed/replay 集合 `..._1024d_...`）× squared L2 每元素 sub+mul+add=3 FLOP，每查询 ~31 MFLOP），低利用率
   同样可能由访存延迟 / 单累加器依赖链造成，且"每查询 ~41MB DRAM 流量"是按向量大小**推算**、非实测。
   要坐实带宽受限，必须先测**同一窗口实测 DRAM 带宽**并对照**平台可持续带宽**（STREAM/规格值），实测贴近
   可持续带宽才下"带宽受限"结论、才据此否决并行/双累加器；缺实测则保留**待验证假设**。
4. **查询级并行已存在**：每个 query 的 `searchKnn` 进全局 folly ThreadPool；线程池大小=2（2 核 pin +
   GOMAXPROCS=2），已被占满。**"=2" 的前提是 factor=1**：v2.4.5 `knowhereThreadPoolNumRatio` 默认 4，
   仅 `factor<=0` 或 `!EnableDisk` 强制 factor=1；本部署实证用 enableDisk=false 才有池=2，磁盘索引启用时
   池=2×4=8，需先核 `KnowhereThreadPoolSize` 生效值再迁移"已占满"结论。

## 步骤

### 1. 确认编译产物真的含 SVE（静态证据，结论限于"构建产物含 SVE 实现"）

```bash
# 构建输出应含：SVE 验证通过：distances_neon.cc.o 含 SVE 指令（whilelo）
objdump -d <build>/.../distances_neon.cc.o | grep -w whilelo   # 命中即"产物含 SVE"
grep COMPILER_SUPPORTS_SVE <build>/milvus/cmake_build/CMakeCache.txt  # =1
nm -D <build>/.../libknowhere.so | grep fvec_L2sqr_sve_pf     # 符号存在（仍只是静态证据）
```

> **这三项静态产物证据只证"构建产物含 SVE 实现"**：库同时含 NEON 与 SVE 实现时，即使实际查询走 NEON，三
> 项也全过。要证"实际查询命中 SVE"，需对**目标 milvus 进程**做一次 L2 查询定位真实分发目标：采样调用栈，
> 或记录 `fvec_L2sqr` 分发分支（hook 层打点 / 断点看走 `_sve` 还是 `_neon`）。

### 2. 重编 knowhere（若确需改内核）

```bash
# 前置：确保 SVE 补丁启用（build.sh 检测 sve-patch/patches 目录存在即启用）
cd <milvus-build-tree> && mv sve-patch.bak sve-patch   # 若曾被禁用
make -j16 knowhere   # CC/CXX=gcc-10/g++-10，-O3 -march=armv8-a+sve
```

- 只做"最小改动"：改 `fvec_L2sqr_sve_pf` 时同步写语义等价性与预期（带宽受限 → 吞吐≈0）。

### 3. 验证"值不值得"再动手

- 算 `GFLOP/s ÷ 核当量 ÷ 峰值`：远低于峰值**≠ 已证带宽受限**——低利用率同样可能由依赖链 / 访存延迟引起。
  要判"并行/展开/双累加器预期吞吐≈0"，先补**实测 DRAM 带宽 vs 平台可持续带宽**对照：实测贴近可持续带宽
  才下"带宽受限"结论、才预期吞吐≈0；缺实测只标**待验证假设**、只跑最小验证轮，不铺开完整对比。
- 若真要降访存流量（量化/降精度/降召回），那属于正确性取舍，须先过语义正确性核验。

## 门禁

- [ ] `whilelo` 符号 / `COMPILER_SUPPORTS_SVE=1` / `fvec_L2sqr_sve_pf` 三处静态证据齐全，结论限定"构建产物含 SVE 实现"
- [ ] 已用一次 L2 查询确认实际命中函数（采样调用栈 / 记录分发目标），证实"实际查询走 SVE"
- [ ] 重编前确认 SVE 补丁启用、编译 flag 含 `+sve`
- [ ] 改内核前先做带宽/算力分叉判断（带宽受限需实测 DRAM 带宽 vs 可持续带宽，否则标待验证假设）
- [ ] 若改内核，附语义等价性与最小验证轮

## 结果（标定案例）

- `fvec_L2sqr_sve_pf` 确认 256-bit、预取 0x200、单累加器（静态证据：构建产物含 SVE 实现；实际命中依赖一次
  L2 查询采样调用栈确认）。
- TOP1(c) 并行化证伪：低利用率 ~8%（1024 维 × 3 FLOP/元素口径）标"带宽受限待验证"（缺实测 DRAM 带宽 vs 可持续带宽）
  + 2 核已被查询级并行占满（enableDisk=false → factor=1 → 池=2）→ 放弃（核占满已实证，带宽受限为待验证假设）。
- TOP1(b) 双累加器+预取 0x400+2×步长：1 轮无改善，且 `libknowhere.so` 206MB vs 314MB（`.text` +45%）
  confound → 无法干净归因，放弃。

## 参考

- `references/sve-kernel.md`：源码行号、构建命令与开关、SVE 验证三件套、带宽测算全过程、
  TOP1(b)/(c) 证伪记录。