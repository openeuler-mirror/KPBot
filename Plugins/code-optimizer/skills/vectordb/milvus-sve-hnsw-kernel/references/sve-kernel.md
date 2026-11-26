# SVE HNSW 距离内核全量（标定案例）

> **来源边界**：本文件描述的 `fvec_L2sqr_sve_pf` 内核、`hook.cc` 的 `__ARM_FEATURE_SVE` 分发、CMake 的
> `COMPILER_SUPPORTS_SVE` 均为私有补丁 `0001-hnsw-scann-sve-pf` 的产物；上游 knowhere v2.3.5（Milvus
> 2.4.5 固定版）只有 NEON 实现、`distances_neon.cc` 无 SVE 代码。验证三件套只对打了该补丁的构建有意义；
> stock 构建执行会 whilelo 零命中 / CMakeCache 无该变量 / nm 无该符号，应判"未打补丁"而非"构建损坏"。

## 1. 内核实况

- `fvec_L2sqr_sve_pf`：`distances_neon.cc`，SVE **256-bit**（`svcntw()`=8 lanes，
  `/proc/sys/abi/sve_default_vector_length=32`），预取 `PLDL1KEEP_OFF(x+i, 0x200)`（512B），
  单累加器 `svmla_f32_m`。
- 分发：`fvec_L2sqr_neon` → `fvec_L2sqr_sve_pf`（`hook.cc`，`__ARM_FEATURE_SVE`）；编译
  `-O3 -march=armv8-a+sve`；hnswlib AVX/SSE 被 `#if 0` 禁用。
  > **以上是静态源码证据，结论只到"构建产物含 SVE 实现"**：库同时含 NEON 与 SVE 实现时，即使实际查询走
  > NEON，这些静态检查也全过。要证"实际查询命中 SVE"，需对目标 milvus 进程做**一次 L2 查询**采样调用栈 /
  > 记录 `fvec_L2sqr` 分发分支（走 `_sve` 还是 `_neon`）。
- 调用链：segcore → `HnswIndexNode::Search` → `HierarchicalNSW::searchKnn` → `space_l2.h L2Sqr` →
  `faiss::fvec_L2sqr`。

## 2. 并行度现状

- 查询级并行：`hnsw.cc` 每 query 的 `searchKnn` 进全局 folly ThreadPool；`thread_pool.h` 用
  `hardware_concurrency()`。
- 线程池大小=2 的推导链（`querynodev2/server.go:186-187` → `component_param.go` `GetCPUNum()×factor` →
  `hardware_info.go` `GetCPUNum()=GOMAXPROCS`）里，**"×factor 得 2" 的前提是 factor=1**：v2.4.5
  `pkg/util/paramtable/component_param.go:2163-2180` 的 `KnowhereThreadPoolSize`
  （`queryNode.segcore.knowhereThreadPoolNumRatio`）DefaultValue="4"，Formatter 仅在 `factor<=0` 或
  `!EnableDisk` 时强制 factor=1。**本部署实证用 enableDisk=false（无磁盘索引）才有池=2**
  （2 核 pin + automaxprocs → GOMAXPROCS=2）；磁盘索引启用（生产常见）时池=2×4=8，"2 核已被占满"
  的复现前提随之变化——**他部署需先核 `KnowhereThreadPoolSize` 生效值再迁移本结论**。

## 3. 带宽测算（低利用率 ≠ 已证带宽受限）

> **维度与 per-element FLOP 口径（先写死）**：标定集合是 1024 维（seed/replay 均为 `MILVUS_SEED_DIMENSIONS=1024`、
> 集合名 `mem0_locomo_replay_1024d_hnsw_l2`），squared L2 每元素 = sub + mul + add = **3 FLOP**（NEON/SVE 路径同为 3）。
> 因此每查询 = 候选数 × 维度 × 3 = 1 万 × 1024 × 3 ≈ 31 MFLOP；旧稿"每查询 77 MFLOP"是 5 FLOP/元素 × 1536 维
> （10000×1536×5=76.8M）两个错误口径叠加，已按"1024 维 × 3 FLOP/元素"统一重算整条数字链。

| 量 | 值 |
|---|---|
| 候选集 | ~1 万 |
| 维度 | 1024 |
| 每查询浮点（3 FLOP/元素） | ~31 MFLOP |
| 76.8 req/s → 浮点 | ~2.4 GFLOP/s |
| 核当量 | ~0.86（self 52% × CPU 164.7%） |
| 单核 | ~2.7 GFLOP/s/核 |
| Kunpeng 950 SVE-256 峰值 | ~36 GFLOP/s |
| 利用率 | **~8%** |
| 每查询 DRAM（**推算**，非实测） | ~41 MB |

→ **低利用率只能"不能排除"带宽受限，不等于"就是"带宽受限**：~8% 同样可能由访存延迟 / 单累加器依赖链造成；
  41 MB/query 是按向量大小推算式估算（1 万 × 1024 × 4B ≈ 41 MB），不是实测。要下"带宽受限"结论并据此否决
  并行/双累加器，必须先补：① **实测同一窗口 DRAM 带宽**（`perf stat` 带宽 PMU 计数器 ÷ 墙钟）；② **对照平台
  可持续带宽**（STREAM / 规格值）。实测贴近可持续带宽才判带宽受限、才动降精度/降召回；否则标**待验证假设**，
  转向依赖链/延迟方向。

## 4. SVE 验证三件套（静态证据，结论限于"构建产物含 SVE 实现"）

```bash
objdump -d <build>/distances_neon.cc.o | grep -w whilelo          # 命中（5 处 whilelo）
grep COMPILER_SUPPORTS_SVE <build>/milvus/cmake_build/CMakeCache.txt  # =1
nm -D <build>/libknowhere.so | grep fvec_L2sqr_sve_pf
```

`build.sh` 输出 `SVE 验证通过：distances_neon.cc.o 含 SVE 指令（whilelo）`。

> 三项都是**静态产物证据**：库同时含 NEON 与 SVE 实现时，即使实际查询走 NEON 也全过。结论只到"构建产物
> 含 SVE 实现"；要证"实际查询命中 SVE"，需对目标 milvus 进程做**一次 L2 查询**采样调用栈 / 记录分发分支。

## 5. 构建开关

- `build.sh` 检测 `sve-patch/patches` 目录存在即启用 SVE；若被改名 `sve-patch.bak` 则回退 NEON。
- `make -j16 knowhere`，`CC/CXX=gcc-10/g++-10`，`-O3 -march=armv8-a+sve` + `USE_DYNAMIC_SIMD`。

## 6. TOP1(c) 并行化证伪

低利用率 ~8%（**待验证假设**：缺实测 DRAM 带宽 vs 可持续带宽，低利用率同样可能由依赖链 / 访存延迟引起）
+ 2 核已被查询级并行占满（已实证，且 **前提是 enableDisk=false → factor=1 → 池=2**，见 §2）→ 查询内再切线程
只挤线程、增开销，吞吐不涨 → 放弃。

## 7. TOP1(b) 展开/预取尝试

| 项 | 原 | top1-b |
|---|---|---|
| 累加器 | 单 | 双 sum0/sum1 |
| 步长 | `svcntw()` | `2*svcntw()` |
| 预取 | 0x200 | 0x400 |
| 尾部 | 无 | `svwhilelt_b32` |

结果：1 轮无改善（p50 表观 -19.8% 是方差，top2 run#1 120.93 ≈ top1b 120.21）；
`libknowhere.so` 206MB vs 314MB、`.text` +45% confound → 无法干净归因 → 放弃。