# 算子层优化（L2）：算子库接入、路由、预打包、JIT、稀疏

> **何时读**：要把 TF 的热点算子（MatMul/BatchMatMul/FusedMatMul/Softmax/逐元素/稀疏乘…）路由到高性能库（oneDNN/KDNN/KML/OpenBLAS 类）或自写内核；设计路由门控与回退；做权重预打包（packing cache）；评估 JIT vs 静态内核；稀疏×稠密乘法优化；给依赖库（Eigen 等）打小补丁。

算子层的核心契约：**框架算子入口处插一个"路由判断"，满足条件就调高性能实现，否则走原生路径**。全部工程问题都围绕这个契约展开——怎么判（门控）、怎么调（adapter）、怎么更快（prepack/JIT）、怎么不出错（回退与守卫）。

---

## 1. 路由三层门控（最重要的模式）

```
#if defined(ENABLE_MYLIB)                 // 第 1 层：编译期
  if (IsMyLibEnabled() &&                 // 第 2 层：运行期总开关（进程 flag，默认可关）
      dtype == float &&                   // 第 3 层：算子内逐项守卫
      rank >= 2 && rank <= 5 &&
      !trans_x &&                        // 转置支持与否要显式判断
      meets_threshold) {
    if (TryMyLibGemm(...)) return;        // 成功才结束；可回退失败须保持输入/状态可重试
  }
#endif
  // 无条件 fall through 到原生实现       // 回退契约
```

- **第 1 层（编译宏）**：由构建系统的平台 select 注入（如 bazel `select` 只在目标平台加 `-DENABLE_MYLIB`）。非目标平台完全不编入相关代码，零成本。
- **第 2 层（运行总开关）**：进程级 flag（gflag/环境变量），默认值 = 你推荐的稳妥值。仿照当前分支已有开关机制，明确是启动期读取还是允许动态切换；首次求值缓存的开关需重启才生效。记录解析后的值、平台拒绝原因与实际路由命中。
- **第 3 层（算子内守卫）**：dtype、rank 范围、transpose/adjoint、广播形态、维度上限（防 int32 溢出）逐项判断。**每一条守卫都对应库的一个真实限制**——写守卫前先穷举库的约束清单，漏一条就是一个线上正确性 bug。

**守卫之外的规模门槛（防"小输入变慢"）**：路由到重量级实现前判元素数/维数下限（如逐元素算子 >1000 元素才走库；GEMM 的 M ≥ 某阈值才值得 pack）。库的准备工作（描述符构造、线程切分）在小输入上摊不平。

**典型路由点清单**（在 TF 里找这些位置）：`kernels/matmul_op_fused.cc` 的 `LaunchFusedMatMulOp`、`kernels/matmul_op_impl.h` 的 `BaseBatchMatMulOp::Compute`（batch==1 与 batch>1 分开处理）、`cwise_ops_common.h` 的 Binary/UnaryOp（`if constexpr` 特化具体 functor）、`softmax_op.cc`、稀疏乘的 functor 分发宏处。新版 TF 位置会变，找法：从 op 的注册名 → kernel 注册 → Compute/Launcher 函数。

## 2. adapter 设计（框架张量 ↔ 库描述符）

不要在算子 Compute 里直接堆库调用。写一个**独立 adapter 头文件层**（`mylib_adapter.h`），提供每类算子一个入口函数，内部完成：

1. **描述符构造**：把框架 Tensor 的 shape/dtype/布局翻译成库的描述对象（如 `TensorInfo{dims, TypeT, Layout}`）。批量矩阵乘的技巧：任意 2–5D 张量**右对齐填入固定长度向量**（低维补 1），转置用布局枚举表达（AB↔BA / ABCDE↔ABCED），单次库调用覆盖整个 batch；先验证每个 batch 维相等或至少一侧为 1，再按广播规则求输出维（含 0 与 1 的边界），不能无条件取 max。
2. **类型/布局映射表**：`template<TypeAdapter<T>>` 把框架类型映射到库类型枚举（float/half/bf16/int8/…），不支持的映射成 UNDEFINED 并在路由期拒绝——**让"不支持"显式失败，而不是静默错算**。
3. **epilogue 融合**：库支持 post-ops 的话，把 bias + 激活（ReLU/ sigmoid）挂上（一次 GEMM 完成），否则在 adapter 里手写收尾。epilogue 是库接入里性价比最高的部分。
4. **线程池激活**（见 runtime_threading.md §1）：从 `ctx->device()->tensorflow_cpu_worker_threads()->workers` 取框架 intra-op 池 → 包成库要求的 threadpool 接口 → activate → 计算 → deactivate。RAII 恢复进入前的激活态，覆盖早退/异常与嵌套调用；仅 deactivate 会丢掉外层池。

adapter 层独立成头文件的另一个好处：**多个算子入口共享同一套翻译逻辑**，修一处全生效。

## 3. 权重预打包（prepack / packing cache）

GEMM 类内核为 SIMD 连续加载，通常要求权重是 **blocked 布局**（按 N 方向分组、组内 K 连续、lane 交错——让一条向量加载指令恰好填满寄存器）。原生权重是行主序，于是"每次推理都要 pack 一遍"——在线 serving 权重不变，这是纯冗余。

**模式**：首次执行把权重一次性 reorder 成 blocked 布局并**持久缓存**，稳态 GEMM 直接读常驻 buffer，框架跳过 pack 的分配/barrier/释放全流程。

设计要点（每条都是实战换来的）：

- **缓存位置**：单算子独占权重时优先放 OpKernel 成员，确认实例实际生命周期；跨节点共享权重时再评估模型级资源，避免重复打包。比较查找成本、共享收益和总内存，不把某一种位置规定为唯一方案。
- **原子发布的只读快路径**：`shared_ptr<const Entry>` + acquire/release 的原子读写发布完整 Entry；miss 加锁双检后打包。命中可免应用层 mutex，但 atomic shared_ptr 不保证底层 lock-free，应测引用计数与同步开销。替换/逐出后旧 buffer 仍由在飞请求持有，不能提前释放。
- **缓存键与所有权**：覆盖源权重身份/版本、dtype、shape、transpose、切分及打包格式/后端身份。权重不可变不保证地址稳定；用指针键时持有源 Tensor 所有权或证明同等生命周期，防止释放后地址复用。动态权重必须版本化失效，否则禁用缓存；模型重载隔离新旧版本。上游若每次重建拼接张量，应先验证拼接缓存的身份与所有权，避免 prepack 持续 miss 或误命中。
- **门槛**：按实际 shape、复用次数、后端和线程配置测收益。`M >= 16`、`K*N >= ~1k`、非转置是历史实现的候选条件，不是通用限制；不满足已验证条件时保留 plain 路径。
- **字节预算**：同时约束单权重与模型/进程总量，计入并发构建、旧版本在飞引用和临时缓冲。历史实现采用 64MB/权重、超限不缓存；这是案例策略，不保证总内存有界。容量及 0 值语义以当前实现为准，超限日志限流；若采用逐出，必须保护在飞引用。
- **收益/代价要分开报告**：冷启动（首次 reorder）与稳态（跳过 pack）单独测；要求以真实模型 A/B 验证冷启动时间、稳态 p50/p90/p99、吞吐、RSS 五项。
- **A 侧 vs B 侧**：只 pack 权重侧（不变的一侧）；激活侧若行主序天然匹配内核读取顺序，就不 pack——收益全部来自权重侧。

## 4. JIT vs 静态内核、运行时特性检测

- **静态编译内核**：每个 (数据类型 × ISA × 形状档) 预编译一份。启动快，但形状覆盖有限、二进制膨胀。
- **运行时 JIT 生成**：按 shape/芯片现场生成微内核（需要一套 aarch64/x86 代码生成基础设施）。覆盖任意形状、能按 shape 特化（循环全展开、寄存器分配最优），代价是首次调用的编译开销（配小形状专用的静态快路径内核兜底：GEMV 变体、小矩阵 kernel 先查表，未命中再走 JIT 方案查找）。
- **演进路径**（实战验证）：初期按芯片分派静态内核（NEON 一套/SVE 一套）→ 统一到"都走 JIT 方案查找 + 小 kernel 静态快路径"——删掉静态分支后代码量和特判显著下降。
- **运行时特性检测**：ARM 先用 OS 暴露的 HWCAP/HWCAP2 判断指令集可用性，再用可获得的芯片标识选择调优变体；芯片型号识别不等价于 ISA 能力检测。型号白名单是历史实现条件，不能代替能力守卫。显式配置可选择已支持路径或禁用加速，不得越过硬件能力检查强开 ISA。
- **线程切分策略按芯片选**：不同微架构的缓存容量不同，GEMM 的 M/N 切分求解器可以按芯片型号选简单/advanced 版本（同上，用正式检测而非环境变量）。

## 5. 稀疏 × 稠密（SparseTensorDenseMatMul 类）

推荐模型 NN 层常见"稀疏激活 × 稠密权重"。原生实现按 nnz 逐条 chipping 累加，单线程且访存差。优化路线：

1. **在线格式转换**：框架的稀疏是 COO（indices/values），库要 CSR——adapter 里单趟计数建行指针数组（pntrb/pntre）+ 列索引，就地转换，不要引入离线预处理步骤。
2. **密度自适应分派**（库内 driver）：按 `ratio = nnz/(M·K)` 与 N 的范围选路径——足够稠密时先把稠密 B **按列块转置打包**进对齐缓冲（把稀疏侧随机访问变成对连续块的流式读），再调块化内核（1 行 × 16/8/4/1 列分档的 NEON 汇编微内核）；超稀疏走免打包路径。
3. **全满回退**：在索引合法、无重复且覆盖所有坐标时，`nnz == M*K` 才表示全满；单凭 nnz 相等无法排除重复和缺项。满足原算子语义的 ToDense 转换后走**稠密 GEMM** 路径（能吃到 JIT/prepack 全套）。100% 稠密走 CSR 反而显著更慢——按实际密度分流。
4. **多线程**：按输出**列维度**分片（列分片天然无写冲突、CSR 行结构全线程共享只读）、每线程独立缓冲（无锁+消除伪共享）、`malloc_align` 对齐分配（SIMD 效率）。
5. **正确性坑**：框架稀疏索引**不保证有序**——CSR 行指针构造若假设行分组连续，乱序输入直接错；要么计数排序重排，要么确认库内有兜底重排。越界校验也不能省（原生路径逐 nnz FastBoundsCheck，你的快路径同样要有）。

## 6. 第三方库集成工程

- **头文件适配**：库头文件若用 C++ 异常做校验，而框架构建是 `-fno-exceptions`，直接 include 编译失败。优先使用库的无异常 API，或通过允许异常的独立适配编译单元捕获并转换成状态码。必须修改头文件时保留类型/边界校验，把 throw 改为显式错误返回，并逐调用点传播到 TF Status 或安全回退；不能删除越界检查或吞掉错误继续计算。
- **构建集成**：构建系统里给库做平台 select（目标平台编入、其余 `target_compatible_with = incompatible`）；库源码目录组织成 `include/`（公共 API 头）+ `src/`（实现）+ 顶层 `BUILD`/`build_defs.bzl`（select 宏 + 实例化宏——Bazel BUILD 不能写循环，用 Starlark 宏批量生成模板实例化目标，每种 dtype 组合一个）。
- **闭源库（.so）接入**：`new_local_repository` 指向安装前缀，`cc_import` 挂动态库；运行时依赖（如 BLAS）用 deps 串起来。注意闭源路线的长期维护成本——实战中一整条闭源算子路线后来被自研开源路线整体替换。
- **给依赖库打小补丁（比 fork 便宜）**：框架会带一些"够用但慢"的依赖（如 Eigen 的 Tensor 模块）。定位到热路径后，打 30~150 行的小补丁注入构建（workspace 的 patch_file 列表）：
  - `TensorBlock` 线性拷贝（连续、stride=1 时）从手工 packet 循环改成 `std::memcpy`（平台 `#if` 守卫）——glibc 的 NEON/SVE memcpy 更快；
  - `TensorChipping` 的 `packet()/srcCoeff()` 热路径从**运行时** isInner/OuterChipping 分支改成 `EIGEN_IF_CONSTEXPR` **编译期**分派 + 快路径独立内联函数——消除逐元素分支与整数除法。
  - 补丁尽量跟踪上游 MR（能升级就删本地补丁），patch_file 列表里注明来源。

## 7. 反模式

- **守卫遗漏**：库不支持转置 B，路由却只判了 dtype——transpose 输入进了库，结果错。穷举库约束 → 逐条守卫。
- **激活态泄漏**：手动 activate/deactivate 配对，异常路径漏 deactivate → 后续算子用错池。RAII。
- **prepack 缓存键缺项**：见 §3；以及"开关判断散在 4 个调用点重复"（抽 helper，改一处全生效）。
- **把"不支持"静默映射**：类型映射表默认值应该是"显式不支持"，路由期拒绝，而不是映射成某个恰好能编过的类型。
- **只测快路径**：回退路径（超限/异常/不支持 shape）也要有测试——它恰恰是线上出事时走的路。
- **负结果过度泛化**：库在模型 A 上负收益后直接推广「这类模型都不适合」——shape/线程配置/并发剖面任一不同，结论就可能反转（实测：某 GEMM 库在大 K 模型上 -15%，据此跳过了同属"大 MatMul"的另一模型；后者配上线程上限后实际 +13%）。**负结果必须标注适用范围（shape/配置/负载），跨模型/跨配置复测后才可泛化。**
- **依赖库版本漂移**：补丁打在 workspace 锁定的 commit 上，升级依赖时补丁可能 silently 失败或错位——构建脚本里对 patch 应用失败要硬报错。

## 实战案例摘要

此层支撑过：一套 oneDNN 风格算子库的全量接入（MatMul/BatchMatMul/FusedMatMul+ReLU/Softmax/Sigmoid/FloorMod/稀疏乘/Concat/Einsum，各带三层门控与逐算子守卫）、权重 blocked 布局预打包（原子发布 shared_ptr 缓存 + 案例中的 64MB 上限 gflag 与 M≥16 门槛）、静态内核到 JIT 的统一迁移、MIDR 芯片检测分派 NEON/SVE、CSR 稀疏 GEMM（密度自适应 + 手写 NEON 汇编微内核 + 全满回退）、Eigen 两个热路径小补丁、闭源算子库路线的整体替换。prepack 的 15 项 review 发现（非 RAII 激活、门控不一致、标量 reorder 冷启动尖峰等）是本节多数条目的出处。
