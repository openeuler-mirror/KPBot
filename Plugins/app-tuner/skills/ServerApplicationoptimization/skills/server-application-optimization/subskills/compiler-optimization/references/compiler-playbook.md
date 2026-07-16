# Compiler Optimization Playbook

## Scope

本文件承接 `compiler-optimization/SKILL.md` 的通用细节。进入架构参数、PGO/AutoFDO、LTO/ThinLTO、BOLT、编译器选型、向量化或源码配套分析时读取。

## Knowledge Base Anchors

| 技术 | 适用信号 | 案例/收益口径 | 验证 |
|---|---|---|---|
| Architecture flags | 通用二进制、CRC/atomic/vector 热点 | Redis/Intel flag benchmark；NVIDIA Grace LSE 原子路径可带来数量级局部收益 | CPU flags、`readelf -A`、`objdump` |
| PGO / AutoFDO | branch miss、I-cache、间接调用、热路径稳定 | Arm MySQL PGO：写 +11.8% 到 +16.7%，读 +16.4% 到 +25.9%；Intel TencentDB MySQL oneAPI LTO+PGO 最高 +85% | profile 代表性、QPS/P99、branch/I-cache |
| LTO / ThinLTO | 跨模块调用、内联受限、大工程 | Intel TencentDB MySQL ICX-LTO 最高 +51%；Redis `-O3 -flto` SPEC 约 +5% | 链接内存、二进制大小、功能回归 |
| Function layout / BOLT | Frontend Bound、I-cache/ITLB、代码 footprint 大 | Meta BOLT CPU 执行时间降低约 2%-15%，论文真实应用最高约 +20.41% | I-cache、ITLB、branch miss、profile 覆盖 |
| Loop/vectorization | 数组扫描、编码、校验、张量前后处理 | Redis GEO 算法/计算路径最高约 4x；Intel vectorization 指南 | vector report、SIMD 指令、cycles/element |
| BiSheng / platform compiler | 鲲鹏/aarch64，循环、字符串、数学、原子热点 | BiSheng 公开资料显示 SPEC2017 对 GCC 平均 15%+，需 workload 验证 | 三编译器 A/B、ABI/功能回归 |

知识库来源：`L4-Compiler-Optimization/*.md`、`Foundations/PMU.md`。

## Profile Quality Gate

PGO、AutoFDO、BOLT 和布局优化都依赖 profile 代表性：

- 采集负载必须覆盖核心请求类型、数据集、并发、预热后稳态和异常路径。
- profile 采集命令、时长、数据集、压测参数必须进入报告。
- 源码变化超过约 10% 或热点路径明显变化时重新采集。
- 只覆盖单一场景时，报告边缘场景回归风险。

路径选择：

| 场景 | 推荐路径 |
|---|---|
| 中小型目标、插桩开销可接受 | Instrumented PGO：`-fprofile-generate` -> workload -> `-fprofile-use` |
| 大型应用、不希望插桩失真 | AutoFDO：`perf record -b` -> `create_gcov` -> `-fauto-profile=` |
| LLVM/Clang/BiSheng、多场景合并 | IR PGO：`-fprofile-instr-generate` -> `llvm-profdata merge` -> `-fprofile-instr-use` |
| 前端瓶颈、函数布局问题 | BOLT/Propeller 或 PGO + LTO 布局 |

## LTO Gate

LTO 前检查：

- 所有目标文件和静态库是否由兼容编译器生成。
- `AR/RANLIB` 是否换成 `gcc-ar/gcc-ranlib` 或 `llvm-ar/llvm-ranlib`。
- 是否链接非 LTO 预编译库、插件、汇编对象。
- 链接内存是否可承受，是否需要 ThinLTO 或 GCC partition。
- 符号可见性、导出 ABI、调试符号和崩溃诊断是否满足生产要求。

选择：

- 大项目优先 ThinLTO 或 GCC `-flto=auto -flto-partition=balanced/1to1`。
- 小中项目可试 Full LTO。
- GCC < 12 的大型 ARM 工程谨慎启用 LTO，优先小轮次验证。

## Architecture Flag Diagnostics

必须做三步，不可只看命令行：

1. 硬件能力：`lscpu`、`/proc/cpuinfo`、厂商资料。
2. 编译展开：`gcc -mcpu=native -Q --help=target` 或 Clang 对应 dry run。
3. 二进制验证：`readelf -A`、`objdump`、`perf annotate`。

aarch64 常见断层：

| 能力 | CPU flag | 推荐动作 |
|---|---|---|
| CRC32 | `crc32` | `-march=armv8.x-a+crc`，并验证 CRC 指令或快路径 |
| LSE atomics | `atomics` | `-march=...+lse` 或 `-moutline-atomics` |
| Crypto | `aes sha1 sha2` | `-march=...+crypto` |
| NEON/SVE | `asimd` / `sve` | 明确 `-march`、vectorization report 和指令 |

当热点函数含 `*_sw`、`*_soft`、`*_generic`、`*_byte_by_byte`、`*_fallback`，且占比 >1%，必须执行上述断层诊断。

## Source-Aware Rules

编译选项无收益或负收益时，不要继续叠加 flags，转入代码生成和源码配套：

- runtime dispatch 是否命中硬件快路径。
- 构建宏是否启用平台实现。
- 循环是否因别名、对齐、分支、volatile/atomic 语义无法向量化。
- 锁/原子热点是否需要分片、批量、每线程缓存或减少共享状态。
- `-O3`/LTO 是否造成代码膨胀、I-cache 压力或错误内联。

源码候选按风险分层：

- `build_macro_fix`
- `runtime_dispatch_fix`
- `hot_loop_codegen_fix`
- `algorithmic_hotpath_fix`
- `profile_layout_fix`

## Candidate Action Template

```json
{
  "action_id": "compiler-001",
  "action_type": "arch_flags|pgo|lto|compiler_upgrade|source_patch",
  "precondition": ["current_run_id evidence is current"],
  "build_plan": ["commands or build-system edits"],
  "codegen_validation": ["readelf/objdump/perf annotate commands"],
  "functional_validation": ["smoke tests"],
  "performance_validation": ["A/B benchmark plan"],
  "rollback": ["restore baseline binary/config"],
  "reject_condition": ["gain <= noise", "P99 regression", "functional failure"],
  "risk": ["ABI", "portability", "profile staleness"]
}
```
