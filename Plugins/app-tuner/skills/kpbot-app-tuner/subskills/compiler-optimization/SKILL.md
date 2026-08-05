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
- MySQL LSE outline atomics patch：`patches/mysql-8.0.25-lse-outline-atomics.patch`
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

每个 `candidate_actions[]` 必须包含 `action_id`、`action_type`、`precondition`、`commands_dry_run`、`commands_execute`、`expected_gain`、`risk`、`validation`、`rollback`、`stop_or_reject_condition` 和 `evidence_sources`。重编译、补丁、PGO/LTO/BOLT、编译器切换和二进制替换必须在 rollback 中记录恢复基线二进制、配置、运行库和目标实例身份复核步骤。
