---
name: compiler-optimization
description: 根据热点函数、topdown/PMU、构建日志、二进制反汇编和源码可变更边界，分析编译器版本、架构参数、LTO、PGO/AutoFDO、代码布局、向量化、原子/CRC 指令和源码配套优化，作为 kpbot-app-tuner 的子 skill 使用。也覆盖 A+K 场景（昇腾 NPU + 鲲鹏 CPU）的编译优化，包括毕昇编译器选型、Python/PyTorch/torch_npu 的 LTO/PGO 编译优化。
---

# Compiler Optimization

当证据显示性能仍可能受编译器、编译选项、代码生成或源码形态影响时使用本子 skill。分析阶段只生成候选动作；重编译、替换二进制、打补丁、切换运行库都必须回到主流程串行执行验证阶段。

## 何时触发

满足任一条件即进入本 skill：

- topdown 显示 Frontend Bound、Bad Speculation、I-cache/ITLB、branch miss 或代码布局问题。
- 热点函数落在 CRC/checksum、atomic/lock、memcpy/memset、字符串、加密、数学、循环、解析或第三方库代码生成路径。
- 当前二进制使用通用架构参数，或 `-mcpu/-march/-mtune` 与 CPU flags、热点路径不匹配。
- 项目允许重编译，且可能通过 PGO、AutoFDO、LTO、ThinLTO、BOLT、目标平台编译器释放收益。
- ARM/aarch64、鲲鹏、Graviton、Grace 等平台需要验证 LSE、CRC、NEON/SVE、crypto 或平台编译器。
- 用户询问 Python/PyTorch/torch_npu 编译优化、毕昇编译器选型、AI 训练推理框架编译参数。
- 用户要求编译优化 A+K 场景（昇腾 NPU + 鲲鹏 CPU）下的 Python/PyTorch/torch_npu。

## 必读 Reference

按需加载：

- 通用编译优化策略、知识库技术与案例、输出契约：`references/compiler-playbook.md`
- MySQL ARM64/LSE/CRC 专项、补丁和二进制等价门控：`references/mysql-arm64-playbook.md`
- MySQL LSE outline atomics patch：见 `references/mysql-arm64-playbook.md` 中的补丁内容与应用方法
- A+K 场景毕昇编译器自动化编译脚本（BiSheng Python/PyTorch/torch_npu LTO+PGO 编译流程）：`scripts/ak_compile_optimize.sh`
- 公共依赖、perf/PMU 权限和降级：`../../references/prerequisites.md`

> **⚠️ 涉及 A+K 场景（昇腾 NPU + 鲲鹏 CPU）的 Python/PyTorch/torch_npu 编译优化时，必须加载 `references/ak-compiler-playbook.md`。**
> 毕昇编译器选型、编译参数、LTO/PGO 配置、编译顺序依赖均以经验库为准。

## Input Modes

本 skill 支持两种入口：

### 独立运行（standalone）

- **触发**: 未提供 `evidence_snapshot_dir` 和 `environment_backup_dir`
- **行为**: 自采集编译环境信息（编译器版本、当前二进制编译方式、Python/PyTorch/torch_npu 版本等）
- **压测用例**: 采集后主动询问。有压测用例则走 precise 模式（编译前基线 → 编译 → 复测 → 收益对比 → 保留/回退）;无则走 quick 模式（只给推荐，标注 verified=false）
- **执行**: 逐项询问用户编译范围和优化手段

### 主SKILL调用（subagent）

- **触发**: 主SKILL 提供了 `evidence_snapshot_dir` 和/或 `environment_backup_dir`
- **行为**: 直接读取主SKILL 已采集的证据
- **执行**: 不执行,输出 candidate_actions 供主SKLL 处理

## 输入证据

优先使用本轮 `current_run_id` 的当前证据：

- `perf report`、火焰图、topdown、`perf stat`、PMU 事件可用性诊断。
- 构建系统、编译器版本、构建命令、`CFLAGS/CXXFLAGS/LDFLAGS`、CMake cache、链接器。
- CPU 架构、flags/features、虚拟化/容器限制。
- `readelf -A`、`objdump -d`、`perf annotate`、`nm`、编译器 vectorization/optimization report。
- 源码是否允许修改、热点函数对应源码文件、三方库源码或构建方式。
- 基线二进制身份：版本、hash、`/proc/<pid>/exe`、启动参数、链接库和配置。

## 分析流程

1. **确认收益空间**
   - 若 perf/PMU 不可用，明确降级范围；不能假设编译优化有效。
   - 若 Retiring 已高且热点已是平台快路径，优先把收益空间转给应用算法或配置。

2. **A+K 场景识别（两步判断）**

   **第 1 步: 判断是否为 AI 训练推理场景**

   Agent 从用户提示词中匹配以下关键词:
   - 框架/工具: PyTorch、torch_npu、MindSpore、MindSpeed、CANN、Transformers、vLLM、DeepSpeed
   - 场景: 模型训练、推理、大模型、大语言模型、fine-tune、预训练、微调、多模态、serving、推理服务
   - 硬件: Atlas、昇腾、Ascend、NPU
   - 编译: 毕昇编译器、BiSheng、Python 编译、PyTorch 编译、torch_npu 编译、LTO、PGO

   命中任一关键词 → 判定为 AI 训练推理场景，进入第 2 步。
   未命中 → 询问用户应用场景类型，用户回答 AI 训练推理 → 进入第 2 步；否则按通用编译优化流程处理。

   **第 2 步: 检测硬件是否为 Ascend NPU + 鲲鹏 CPU**

   Agent 用 Bash 工具执行以下命令检测昇腾 NPU 设备:

   ```
   bash -c 'lspci 2>/dev/null | grep -i "processing accelerators\|d100\|d500\|d801" | head -1'
   ```

   CPU 是否为鲲鹏通过 `lscpu` 确认。

   - NPU 检测到 + CPU 为鲲鹏 → 判定为 A+K 场景，加载 `references/ak-compiler-playbook.md`，进入步骤 2a
   - 不匹配 → 按通用编译优化流程处理

   **2a. 编译范围判断与顺序管理（A+K 场景专用）**

   加载 A+K 编译经验库后，Agent 判断编译范围:

   - 用户明确指定组件（如"帮我优化 PyTorch 编译"）→ 只编译指定组件
     - 检查前置依赖：编译 PyTorch 需 Python 为毕昇版；编译 torch_npu 需 PyTorch 为毕昇版
     - 前置依赖满足 → 直接编译指定组件
     - 前置依赖不满足 → 提示"当前 Python/PyTorch 非毕昇编译，建议先编译前置组件"，用户可选择继续或跳过
   - 用户未指定组件（如"优化 A+K 场景的编译"）→ 推荐完整组合编译（Python → PyTorch → torch_npu）
     - 向用户展示组合方案，用户可选择全编或只编部分

   编译顺序规则:
   - Python → PyTorch → torch_npu（严格顺序）
   - 允许从中间开始（前置依赖已满足时）
   - 前一个未完成不能开始下一个（组合编译时）

3. **先证实当前二进制**
   - 记录编译器、选项、架构属性、目标实例身份和运行二进制路径。
   - 确认建议参数是否真的进入目标二进制，而不是只出现在命令行或历史日志。
   - A+K 场景下：确认当前 Python/PyTorch/torch_npu 的编译方式（是否毕昇编译、是否已开 LTO/PGO）

4. **三层诊断**
   - 编译选项层：`-O2/-O3`、`-mcpu/-march/-mtune`、LTO、PGO、宏、链接器。
   - 代码生成层：指令、内联、向量化、LSE/CRC/crypto、函数布局。
   - 软件实现层：runtime dispatch、数据布局、循环/分支/锁/原子/批处理粒度。

5. **生成候选动作**
   - 每个动作必须有适用前提、构建命令、验证命令、功能 smoke test、性能 A/B、回退路径和不采纳条件。
   - 若需要源码修改且 `source_change_allowed=false`，只能进入 `blocked_source_candidates`。
   - A+K 场景下：根据经验库的优化手段决策表排序候选动作（LTO 优先于 LTO+PGO，按编译顺序排列）。

## 候选优先级

1. 低风险构建事实修正：确认 `-O3` 未被构建系统覆盖、目标架构 flag 真正生效、链接器/库一致。
2. 热点匹配的架构能力：ARM LSE/CRC/NEON/SVE/crypto，x86 SSE/AVX/AVX2/AVX-512，按机器池可移植性评估。
3. Profile-guided 路径：PGO、AutoFDO、ThinLTO/LTO、BOLT 或函数布局，仅在 profile 代表性可验证时进入。
4. 编译器选型：GCC 版本升级、LLVM/Clang、BiSheng/GCC for openEuler，必须做 ABI/功能/性能回归。
5. 源码配套：runtime dispatch、构建宏、热点循环、数据布局、锁/原子模型，按风险分层。

## 二进制等价门控

任何重编译、补丁或替换二进制候选，在正式收益归因前必须记录：

- 源码来源、branch/tag/commit、补丁路径、构建日志。
- 基线与候选二进制版本、hash、构建选项、链接库、启动参数、配置差异。
- 候选启动后的 `/proc/<pid>/exe`、`cmdline`、`maps`、端口和健康检查。
- 代码生成证据和功能 smoke test。

若候选同时改变了源码、配置、运行库、数据目录、cpuset、NUMA 绑定或启动参数，本轮只能标记为 `confounded_binary_test`，不得把收益直接归因给编译动作。

## 输出字段

至少输出：

- `compiler_profile_mode`
- `recommended_compiler`
- `recommended_arch_flags`
- `arch_flag_gap`
- `codegen_verification`
- `profile_guided_candidates`
- `source_change_candidates`
- `blocked_source_candidates`
- `binary_equivalence_check`
- `confounded_binary_test`
- `candidate_actions`
- `further_compiler_optimization_potential`
- `next_round_candidate`

若证据不足，输出 `status=degraded|blocked` 和最小补采命令。不要仅凭知识库案例承诺固定收益；收益必须由当前 workload A/B 验证。

## Candidate Action Contract

每个 `candidate_actions[]` 必须包含 `action_id`、`title`、`category`、`priority`、`change_mode`、`requires_root`、`risk`、`implementation_plan`、`validation_plan`、`rollback`、`expected_effect`、`expected_gain_metric`、`rejection_criteria` 和 `evidence_refs`。重编译、补丁、PGO/LTO/BOLT、编译器切换和二进制替换必须在 rollback 中记录恢复基线二进制、配置、运行库和目标实例身份复核步骤。

## NPU 推理 PGO Profile 采集

NPU 推理场景的 PGO profile 采集与 CPU 通用场景有本质区别，必须用真实推理负载在 instrumented build 上运行采集，不能用传统 `perf record` / `perf cpi` 栈采样替代。

### 为什么不能用传统 perf 采样

- NPU 算子在 device 侧（NPU 上）执行，host 侧 `perf` 只能采到 CPU 侧的 launch/wait/拷贝路径，采不到算子内部热点。
- PGO 需要的是"哪些函数被调用、调用频率、分支走向"，而 perf 栈采样给的是"CPU 上热点函数排名"，两者目标不同。
- 因此 NPU 推理 PGO 必须用编译器插桩（`-fprofile-generate`）+ 真实负载运行的方式采集。

### vLLM 推理 PGO 采集流程

完整流程（针对 PyTorch + torch_npu 两层）：

```
1. 编译 PGO1 instrumented 版本
   - PyTorch: -flto=thin -fprofile-generate=/tmp/profile
   - torch_npu: --enable_lto --enable_pgo=1
   - 安装到运行环境（pip install --force-reinstall --no-deps）

2. 启动 vLLM 服务，用真实模型和请求负载运行
   export LLVM_PROFILE_FILE=/tmp/profile/default_%m.profraw
   export OMP_PROC_BIND=false
   # 启动 vLLM 服务（真实模型，如 qwen2.5-1.5b）
   # 用真实请求负载压测，覆盖主要推理路径（prefill/decode）

3. 收集 .profraw 文件，合并为 .profdata
   llvm-profdata merge /tmp/profile -o /tmp/profile/default.profdata

4. 用 profdata 编译 PGO2 版本
   - PyTorch: -flto=thin -fprofile-use=/tmp/profile/default.profdata
   - torch_npu: --enable_lto --enable_pgo=2
   - 安装到运行环境
```

### Profile 必须匹配目标负载

实战对比（Ascend910 + vLLM qwen2.5-1.5b）：

| Profile 来源 | 收益（相对 PGO1 基线） | 说明 |
|-------------|---------------------|------|
| 通用 profile（w00664011 通用负载采集） | 基准 | profile 与目标负载不匹配 |
| qwen2.5-1.5b 真实推理负载 profile | +1.33% | profile 与目标负载匹配 |

- 用目标模型 + 目标请求形态采集的 profile，PGO2 收益显著高于通用 profile。
- profile 采集时必须覆盖推理主要路径：prefill、decode、KV cache 读写、sampling。
- 若服务有多种请求形态（不同 seq length、batch size），采集时应混合覆盖，否则 PGO2 只优化到部分路径。

### Profile 覆盖率指标

采集后用 `llvm-profdata` + `llvm-cov` 评估 profile 质量：

```
llvm-profdata show /tmp/profile/default.profdata -o text | head
# 关注: 函数覆盖率、最大函数计数、总计数

llvm-cov export -instr-profile=/tmp/profile/default.profdata \
  -object <libtorch_cpu.so 路径> -summary-only > coverage.json
```

实战指标（qwen2.5-1.5b profile）：

| 指标 | 实测值 | 说明 |
|-----|-------|------|
| profdata 大小 | 97MB | PyTorch + torch_npu 合并 |
| 覆盖函数数 | 242610 | PyTorch libtorch_cpu.so + torch_npu libtorch_npu.so |
| libtorch_cpu.so 大小（PGO2 产物） | 218MB | ThinLTO + PGO2 |
| libtorch_npu.so 大小（PGO2 产物） | 108MB | ThinLTO + PGO2 |

- 函数覆盖率过低（< 30%）→ profile 代表性不足，PGO2 收益打折，应延长采集时间或增加负载覆盖。
- 热点函数命中率低 → 推理主路径未走 instrumented build，检查是否真的安装了 PGO1 版本。

### PGO 是必须的，不能省

实战对比（Ascend910 + vLLM qwen2.5-1.5b，相对 PGO1 基线）：

| 编译方式 | tok/s | 相对基线 |
|---------|-------|---------|
| 纯 ThinLTO torch_npu（无 PGO） | 86.18 | -22.4% |
| ThinLTO + PGO torch_npu | 125.71 | +0%（满收益） |

- 纯 ThinLTO（无 PGO）不仅没收益反而退步 22.4%，说明 torch_npu 在无 profile 引导下 ThinLTO 的内联/布局决策劣于原版。
- NPU 适配层有大量 device launch 路径，ThinLTO 的跨模块内联若无 profile 引导，可能把冷路径内联进热路径。
- **结论：NPU 推理场景 torch_npu 必须 PGO，不能只做 ThinLTO。**

## vLLM 算子编译优化

vLLM 包含 C++/CUDA 扩展算子（attention、layernorm、activation、rotary embedding 等），在 NPU 场景下由 vLLM-Ascend 适配层用 AscendC/C++ 实现对应 NPU 算子。

### vLLM 算子结构

| 算子类别 | CPU/GPU 实现 | NPU 实现（vLLM-Ascend） |
|---------|------------|----------------------|
| attention | FlashAttention / xformers | AscendC flash attention |
| layernorm | CUDA kernel | AscendC kernel |
| activation | CUDA kernel（silu/gelu） | AscendC kernel |
| KV cache | paged attention | paged attention NPU 版 |
| sampling | CUDA kernel | AscendC kernel |

### NPU 场景编译优化点

- **算子内联**：vLLM-Ascend 的 C++ 适配层调用 torch_npu op，跨层内联能减少 dispatch 开销。
- **LTO**：vLLM-Ascend 的 .so 用 `-flto=thin` 编译，与 PyTorch/torch_npu 一致。
- **目标架构参数**：aarch64 + SVE，`-mcpu=klein -march=armv8.6-a+sve2+bf16`。
- **PGO**：理论上可用 PyTorch/torch_npu 同一份 profdata 覆盖 vLLM-Ascend 算子路径。

### 编译命令（vLLM-Ascend，理论路径）

```
export CC=clang
export CXX=clang++
export CMAKE_C_FLAGS="-flto=thin -fuse-ld=lld -mcpu=klein -march=armv8.6-a+sve2+bf16"
export CMAKE_CXX_FLAGS="-flto=thin -fuse-ld=lld -mcpu=klein -march=armv8.6-a+sve2+bf16"
# 若做 PGO:
# export CMAKE_C_FLAGS="${CMAKE_C_FLAGS} -fprofile-use=/tmp/profile/default.profdata"
# export CMAKE_CXX_FLAGS="${CMAKE_CXX_FLAGS} -fprofile-use=/tmp/profile/default.profdata"

cd vllm-ascend
pip install -e . --no-build-isolation
# 或:
# python setup.py bdist_wheel && pip install dist/*.whl --force-reinstall --no-deps
```

### 实战收益说明

实战（Ascend910 + vLLM qwen2.5-1.5b，+94.5%）中 vLLM 本身未重编译，收益主要来自 PyTorch/torch_npu 重编译：

| 优化轮次 | 动作 | tok/s | 累计收益 |
|---------|------|-------|---------|
| 基线 | gcc 预编译版 | 64.63 | 0% |
| C6 | BiSheng 编译 Python LTO+PGO | 110.92 | +71.6% |
| C7 | PyTorch ThinLTO+PGO + torch_npu ThinLTO+PGO + BiSheng tcmalloc | 125.71 | +94.5% |

- vLLM-Ascend 算子层有增量空间，但优先级低于 PyTorch/torch_npu（后两者是基础库，影响所有算子 dispatch 路径）。
- 若 PyTorch/torch_npu 已优化到顶，再考虑 vLLM-Ascend 重编译。
- vLLM-Ascend 重编译需 profile 覆盖其算子调用路径，可复用 PyTorch/torch_npu 的 profdata。

## torch_npu Adapter 编译

torch_npu 是 PyTorch → CANN 的适配层，把 PyTorch op 调用转译为 CANN ge_graph 或 npugraph_ex 执行图。

### 编译期图执行模式选择

| 模式 | 编译开关 | 说明 |
|-----|---------|------|
| ge_graph | 默认 | 通过 GE（Graph Engine）构图执行 |
| npugraph_ex | `-DENABLE_NPU_GRAPH_EX=ON` | 实验性 NPU 图执行，部分算子融合更激进 |

- 推理场景默认 ge_graph 即可。
- 若开启 npugraph_ex 需 CANN 版本匹配，且需回归验证算子融合正确性。

### ThinLTO + PGO 编译参数

```
export CC=clang
export CXX=clang++
cd torch_npu
git clean -dfx

# PGO1（插桩）
bash ci/build.sh --python=<Python版本> --enable_lto --enable_pgo=1 --disable_rpc

# PGO2（使用 profile）
bash ci/build.sh --python=<Python版本> --enable_lto --enable_pgo=2 --disable_rpc
```

参数说明：

| 参数 | 作用 | 必要性 |
|-----|------|-------|
| `--enable_lto` | 开启 ThinLTO | 必须（NPU 推理场景） |
| `--enable_pgo=1` | 一次编译（插桩） | PGO 流程必须 |
| `--enable_pgo=2` | 二次编译（使用 profile） | PGO 流程必须 |
| `--disable_rpc` | 禁用 RPC（训练场景才需要） | 推理场景必须，减少代码体积 |

### ABI 兼容性（关键）

`-D_GLIBCXX_USE_CXX11_ABI=0` 必须与 PyTorch 一致：

```
# 检查 PyTorch 的 ABI 值
python3 -c "import torch; print(torch._C._GLIBCXX_USE_CXX11_ABI)"

# torch_npu 编译时必须设置相同值
export CMAKE_CXX_FLAGS="${CMAKE_CXX_FLAGS} -D_GLIBCXX_USE_CXX11_ABI=0"
```

- PyTorch 默认 `_GLIBCXX_USE_CXX11_ABI=0`（历史兼容），torch_npu 必须匹配。
- 不一致会导致运行时 std::string/std::vector 等 STL 类型的符号不兼容，崩溃或乱码。

### -D_GNU_SOURCE 解决 PGO 下 CLOCK_MONOTONIC_RAW 未声明

PGO 编译时若报 `CLOCK_MONOTONIC_RAW` 未声明：

```
export CMAKE_CXX_FLAGS="${CMAKE_CXX_FLAGS} -D_GNU_SOURCE"
```

- `CLOCK_MONOTONIC_RAW` 是 GNU 扩展，需 `_GNU_SOURCE` 才能声明。
- PGO 插桩路径会引入额外的时钟读取代码，触发此问题。

### 编译时间与产物

实战数据（Ascend910 + vLLM qwen2.5-1.5b 优化）：

| 阶段 | 编译时间 | 产物 | 产物大小 |
|-----|---------|------|---------|
| torch_npu PGO2 | 850s | libtorch_npu.so | 108MB |
| PyTorch PGO2 | 502s | libtorch_cpu.so | 218MB |

- torch_npu PGO2 编译时间约 14 分钟，需预留编译窗口。
- 产物体积大（ThinLTO + PGO 保留大量元数据），运行环境磁盘需预留空间。

## NPU 推理编译顺序依赖

NPU 推理场景全链编译必须严格按依赖顺序，且全链 ABI、编译器、profile 一致。

### 依赖图

```
Python (LTO+PGO) → PyTorch (ThinLTO+PGO) → torch_npu (ThinLTO+PGO) → vLLM-Ascend / MindSpeed-LLM
```

### 一致性要求

| 一致项 | 要求 | 检查方法 |
|-------|------|---------|
| ABI | `-D_GLIBCXX_USE_CXX11_ABI=0` 全链统一 | `readelf -p .comment <so> \| grep bisheng` + `python3 -c "import torch; print(torch._C._GLIBCXX_USE_CXX11_ABI)"` |
| 编译器 | 全链用同一毕昇编译器 | `readelf -p .comment <so> \| grep -i bisheng` |
| Profile | PGO profile 在最终组合上采集 | profdata 覆盖所有目标组件的函数 |

### 编译顺序规则

1. **Python 先编译**：`--with-lto --enable-optimizations`，PGO 内置（Python 自带 benchmark）。
2. **PyTorch 次之**：依赖毕昇版 Python，ThinLTO + PGO（需跑模型采集）。
3. **torch_npu 再次**：依赖毕昇版 PyTorch，ThinLTO + PGO（需跑模型采集）。
4. **vLLM-Ascend / MindSpeed-LLM 最后**：依赖毕昇版 torch_npu。

- 前一层未完成不能开始后一层（组合编译时）。
- 允许从中间开始：若前置已满足（毕昇版），可直接编译后续组件。
- PGO profile 采集必须在最终组合上跑：先用 PGO1 编译 PyTorch + torch_npu，安装后跑真实负载采集，再用 profdata 编译 PGO2 版本。

### 实战收益（Ascend910 + vLLM qwen2.5-1.5b）

| 阶段 | 编译动作 | tok/s | 累计收益 |
|-----|---------|-------|---------|
| 基线 | gcc 预编译版 | 64.63 | 0% |
| C6 | BiSheng Python LTO+PGO | 110.92 | +71.6% |
| C7 | PyTorch ThinLTO+PGO + torch_npu ThinLTO+PGO + BiSheng tcmalloc | 125.71 | +94.5% |

- Python LTO+PGO 单步收益最大（+71.6%），因为 Python 解释器是 vLLM 调度路径的核心。
- PyTorch + torch_npu ThinLTO+PGO 增量 +13.3%（从 110.92 到 125.71），主要来自算子 dispatch 路径优化。
- BiSheng tcmalloc 替换 glibc malloc 是 C7 增量的一部分，需与编译优化同轮验证。
- 全链 PGO 是必须的：纯 ThinLTO（无 PGO）torch_npu 反而退步 22.4%。
