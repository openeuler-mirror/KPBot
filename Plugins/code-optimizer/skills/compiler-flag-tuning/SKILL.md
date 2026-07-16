---
name: compiler-flag-tuning
description: 调优编译选项以充分利用鲲鹏硬件能力，包括 -march、-ffast-math、LTO、PGO、strip 二进制优化等。适用于 apply-optimization 调用。
---

# 编译选项调优

你是一位鲲鹏性能优化流水线的编译选项调优专家。你的任务是分析当前构建配置中的编译选项，推荐并应用能够充分利用鲲鹏硬件特性的优化选项。

用户调用了 `/compiler-flag-tuning`，参数为：`$ARGUMENTS`

## 输入

从 `$ARGUMENTS` 或对话上下文中获取：

```json
{
  "function": "<function_name>",
  "source_file": "<file_path>",
  "context": {
    "prepareProject": "<prepare-project 输出 JSON>",
    "analyzeHotspot": "<analyze-hotspot 输出 JSON>"
  }
}
```

字段说明：
- `function`：目标函数名（影响范围参考）
- `source_file`：源文件路径（帮助定位构建配置）
- `context.prepareProject`：prepare-project 输出（包含构建系统信息、编译器版本、`repo.compilation` 编译参数）
- `context.analyzeHotspot`：analyze-hotspot 输出（包含 patterns 信息）

## 执行步骤

### 步骤 1：获取当前编译选项

**优先使用 `prepareProject.repo.compilation` 中已提取的编译参数**。该数据由 prepare-project 阶段从构建配置和实际编译命令中提取，包含完整的 cflags/cxxflags/ldflags/build_type/flag_sources/performance_flags。

1. 从 `prepareProject.repo.compilation` 读取当前编译选项：
   - `compilation.cflags` / `compilation.cxxflags` / `compilation.ldflags`：当前生效的编译/链接选项
   - `compilation.build_type`：构建类型（CMake 项目）
   - `compilation.performance_flags`：已分类的性能标志（优化级别、架构标志、数学标志、LTO/PGO/自动向量化状态）
   - `compilation.flag_sources`：各标志的来源文件和变量名（用于步骤 4 定位修改位置）

2. 若 `compilation` 为 `null` 或字段缺失（prepare-project 提取失败的降级场景），则按构建系统从磁盘重新读取：

1. 根据 `prepareProject.repo.build_system` 定位构建配置文件：
   - `cmake` / `cmake+ ninja`：
     - 查找 `CMakeLists.txt`（根目录及子目录）
     - 检查 `CMAKE_C_FLAGS`、`CMAKE_CXX_FLAGS`、`CMAKE_BUILD_TYPE`
     - 检查是否有 `toolchain.cmake` 或 `-DCMAKE_C_FLAGS` 传入的选项
   - `make` / `autotools`：
     - 查找 `Makefile`、`Makefile.am`、`configure.ac`
     - 检查 `CFLAGS`、`CXXFLAGS`、`AM_CFLAGS`、`AM_CXXFLAGS`
     - 检查 `configure` 脚本中硬编码的选项
   - `meson`：
     - 查找 `meson.build`
     - 检查 `c_args`、`cpp_args`

2. 用 Read 工具读取构建配置文件，提取当前编译选项

3. 检查实际编译命令：
   ```bash
   # CMake 项目
   cat <repo.path>/build/CMakeCache.txt | grep -E "CMAKE_C_FLAGS|CMAKE_CXX_FLAGS"
   # 或查看编译数据库
   cat <repo.path>/build/compile_commands.json | head -50

   # Make 项目
   cd <repo.path> && make -n 2>&1 | grep -E "gcc|g\+\+" | head -5

   # Autotools
   cat <repo.path>/config.log | grep -E "CFLAGS|CXXFLAGS" | head -10
   ```

### 步骤 2：评估可改进的编译选项

逐项检查以下优化选项：

#### 2a. 架构选项（收益高，风险低）

**⚠️ CRITICAL：`-mcpu=tsv110` / `-mtune=tsv110` 仅适用于Kunpeng-0xd01**

鲲鹏各代微架构不同，错误指定 `-mtune` 会**降低性能**：

| 型号 | CPU Part ID | 微架构 | 正确的 mcpu/tune |
|------|------------|--------|-----------------|
| Kunpeng-0xd01 | `0xd01` | **TSV110** | `-mcpu=tsv110` |
| Kunpeng-0xd03 | `0xd03` | **LinxiCore (0xd03)** | `-mcpu=native`（**不可用 tsv110**） |
| Kunpeng-0xd06 | `0xd06` | **LinxiCore (LC950)** | `-mcpu=native`（**不可用 tsv110**） |

**添加 `-mcpu=tsv110` 之前必须确认**：
1. 从 `prepareProject.machine.cpu_part_id` 获取 CPU Part ID
2. 只有 `0xd01`（Kunpeng-0xd01）才能使用 `-mcpu=tsv110` / `-mtune=tsv110`
3. Part ID 为 `0xd03`/`0xd06`（LinxiCore / 0xd03 / LC950）**严禁**添加 `-mcpu=tsv110`
4. 无法确定 CPU 型号时，使用 `-mcpu=native` 或仅指定与硬件一致的 `-march`，**不加** `-mtune`
5. `-mcpu=tsv110` 会同时设置 `-march` 和 `-mtune`，在 LinxiCore 上会生成针对 TSV110 流水线优化的代码，指令调度和端口分配与实际硬件不匹配

**`-march` 必须与硬件 ISA 能力一致**，不能猜测或硬编码。参考 `skills/kunpeng_microarch/` 中各型号的微架构文档（`kunpeng920-microarchitecture.md`、`kunpeng_uarch_b-microarchitecture.md`、`kunpeng950-microarchitecture.md`），从 ISA Features 推导：

### 各型号 ISA 特性（依据微架构文档）

| CPU | 核心 | ISA 版本 | Features | 关键缺失 |
|-----|------|---------|----------|---------|
| **Kunpeng-0xd01** | TSV110 | ARMv8.2 | NEON, AES, SHA, PMULL, FP16 | **无 SVE**（文档标注"SVE 支持情况：不支持"） |
| **Kunpeng-0xd01 高配** | TSV110 | ARMv8.2 | 以上 + SVE, FPH, DCPOP | — |
| **Kunpeng-0xd03** (0xd03) | 0xd03 | ARMv9-A | v8.0: AES/SHA/PMULL; v8.2: FP16/DotProd/FHM/SHA512/SHA3/SM3/SM4/**SVE**; v8.6: BFloat16; v9.0: **SVE2**/SVE_BitPerm/ETE/TRBE | — |
| **Kunpeng-0xd06** | LC950 | ARMv8-A (v9.2兼容) | 与 0xd03 类似，NEON + SVE + SVE2 | — |

### 对应的 -march 推导

| CPU | 正确 -march / -mcpu | 错误（会 SIGILL 或降性能） |
|-----|--------------------|--------------------------|
| Kunpeng-0xd01（无 SVE） | `-mcpu=tsv110` 或 `-march=armv8.2-a` | `-march=armv8.2-a+sve` → **SIGILL** |
| Kunpeng-0xd01（高配 SVE） | `-march=armv8.2-a+sve` 或 `-mcpu=tsv110+sve` | 不加 `+sve` → SVE 代码无法编译 |
| Kunpeng-0xd03 | `-march=armv9-a` 或 `-mcpu=native` | `-mcpu=tsv110` → 错误 TSV110 调度，**性能下降** |
| Kunpeng-0xd06 | `-march=armv8.5-a` 或 `-mcpu=native` | `-mcpu=tsv110` → 错误 TSV110 调度，**性能下降** |

### 推导规则

1. **先读微架构文档**：Read `skills/kunpeng_microarch/kunpeng<型号>-microarchitecture.md`，定位 ISA Features 章节
2. isa_features 中**存在**的特性 → 对应的 `-march` 扩展可以加
3. isa_features 中**不存在**的特性 → **严禁**加（会 SIGILL）
4. **Kunpeng-0xd01 必须检查 SVE**：文档第 110 行明确"SVE 支持情况：不支持"，不要默认给 0xd01 加 `+sve`
5. 无法获取硬件信息 → 保守不动 `-march`，只调 `-O3`/`-flto` 等非指令集选项

### 架构选项表（仅在硬件确认支持时添加）

| 选项 | 说明 | 硬件条件 |
|------|------|---------|
| `+crypto` | AES/SHA/PMULL | **所有鲲鹏均支持** |
| `+dotprod` | SDOT/UDOT 点积 | 0xd01 高配 + 0xd03/0xd06 均支持。0xd01 基础版需确认 |
| `+sve` | SVE 向量指令 (128/256-bit) | **0xd01 高配 + 0xd03/0xd06**。0xd01 基础版**不支持** |
| `+sve2` | SVE2 扩展 | **仅 0xd03/0xd06**（ARMv9）。0xd01 不支持 |
| `+bf16` | BFloat16 | **仅 0xd03/0xd06**（v8.6）。0xd01 不支持 |
| `-mcpu=tsv110` | TSV110 微架构调度 | **仅 0xd01**（Part ID 0xd01） |
| `-mcpu=native` | 自动探测当前 CPU | **推荐**：0xd03/0xd06（LinxiCore）或未知型号 |
| `-moutline-atomics` | 运行时 LSE 探测 | **推荐所有多线程程序**（GCC 10+，所有鲲鹏均支持 v8.1+） |
| `-msve-vector-bits=256` | 固定 SVE 宽度 | 0xd01 高配 / 0xd03 / 0xd06。消除 VLA 开销 |

### 微架构感知的 Cache 参数（来自架构文档，可传给 --param）

| CPU | L1D | L2 | L3 | 建议 --param |
|-----|-----|----|----|-------------|
| 0xd01 | 64KB/4-way/4c | 512KB/core/10c | Ring Bus/36~90+c | `l1-cache-size=64 l1-cache-line-size=64 l2-cache-size=512` |
| 0xd03 | 64KB/core | 1MB/core | ~23MB/16c/19-way | `l1-cache-size=64 l1-cache-line-size=64 l2-cache-size=1024` |
| 0xd06 | 64KB/core | 1MB/core | ~23MB/16c/19-way | `l1-cache-size=64 l1-cache-line-size=64 l2-cache-size=1024` |

**检测当前硬件 ISA 和 CPU 型号**：
```bash
cat /proc/cpuinfo | grep -E "Features|CPU part" | head -10
gcc -dM -E - < /dev/null | grep -E "__ARM_ARCH|__ARM_FEATURE"
# 获取 CPU Part ID
python3 -c "import struct;f=open('/proc/cpuinfo');[print(l.strip().split(':')[1].strip()) for l in f if 'CPU part' in l]" | head -1
# 微架构文档路径（根据 CPU 型号选择）
ls skills/kunpeng_microarch/kunpeng*-microarchitecture.md
```

#### 2b. 优化级别（收益中，风险低）

| 选项 | 说明 | 注意 |
|------|------|------|
| `-O2` → `-O3` | 启用更激进的优化（含自动循环展开、向量化） | `-O3` 可能增大代码体积 |
| `-O3` → `-O2` | 降级优化级别，缓解 O3 激进内联/展开/函数克隆导致的 I-cache 压力 | 适用于大型 C++ 模板项目、热点分散场景。ARM 平台上 L1I 较小（鲲鹏 64KB），O3 代码膨胀可能反降性能 |
| `-O3` + `-fno-unroll-loops` | 使用 O3 但禁用自动展开（与手写展开冲突时） | 仅当手写展开已存在时 |

##### 2b-extra. O3 降级检测逻辑

`-O3` 并不总是比 `-O2` 快。在以下场景中，`-O3` 的激进优化可能导致性能反降：

**O3 劣化机制**：
- **激进内联**：`-O3` 启用 `-finline-functions`（即使未标记 `inline` 的函数也可能被内联），导致调用点代码膨胀
- **循环展开**：`-O3` 启用 `-funroll-loops`，展开体占用更多 I-cache 行
- **函数克隆**：`-O3` 启用 `-fipa-cp`（过程间常量传播）和 `-fclone-hot-version-paths`，为不同调用上下文生成函数副本
- **ARM 尤其敏感**：鲲鹏 L1I 缓存仅 64KB/core，代码膨胀后热点函数无法全部驻留，I-cache 抖动导致 IPC 下降

**检测方法**：

1. **确认当前优化级别**：
   - 从 `prepareProject.repo.compilation.performance_flags.optimization_level` 获取
   - 若为 `-O3`（且来自 CMake 默认值而非显式指定），标记为候选

2. **I-cache 压力证据收集**：
   ```bash
   # 检查 I-cache miss（需要硬件 PMU 支持）
   perf stat -e L1-icache-load-misses,icache_stall_cycles -a -- <test_method> 2>&1

   # 检查二进制 .text 段大小
   size <build_dir>/lib*.so | grep -E "text|data|bss"
   objdump -h <build_dir>/lib*.so | grep .text
   ```

3. **源码特征辅助判断**：
   - 模板密集：`grep -r "template<" <src_dir> --include="*.h" --include="*.cpp" | wc -l` > 50
   - 头文件量：`find <src_dir> -name "*.h" | wc -l` > 50
   - 源文件量：`find <src_dir> -name "*.cpp" | wc -l` > 100

4. **触发条件**（满足任一即可建议降级实验）：
   - I-cache miss rate > 1%（有硬件 PMU 时）
   - `.text` 段 > 1MB
   - 模板密集 + 热点分散 + 无显式 `-O3` 指定（说明是 CMake 默认值，可安全覆盖）

**应用方式**：
- CMake 项目：`string(APPEND CMAKE_CXX_FLAGS_RELEASE " -O2")` — 追加在默认 `-O3` 之后，GCC 使用最后一个 `-On`
- Makefile 项目：将 `-O3` 替换为 `-O2`
- Autotools/Meson 项目：修改对应的 `*FLAGS` 变量

**建议**：降级后应实测对比 `-O3` vs `-O2` 的性能差异，以验证降级收益。本 Skill 输出中标记 `downgrade_experiment: true` 提示需 A/B 对比验证。

#### 2c. 自动向量化增强（收益中，风险低）

| 选项 | 说明 |
|------|------|
| `-ftree-vectorize` | 启用循环自动向量化（`-O3` 默认开启，`-O2` 需手动加） |
| `-fopt-info-vec` | 输出向量化诊断信息（哪些循环被/未被向量化） |
| `-fopt-info-vec-missed` | 输出未向量化的原因（帮助定位优化机会） |
| `-fno-math-errno` | 数学函数不设置 errno（允许更多优化） |
| `-funsafe-math-optimizations` | 不安全的数学优化（浮点结合律等） |
| `-ffast-math` | 集成上述所有数学优化 + 更多（**可能影响数值精度**） |

#### 2d. 链接时优化（LTO）（收益中，风险中）

| 选项 | 说明 | 注意 |
|------|------|------|
| `-flto` | 链接时优化（跨文件内联和优化） | 增加链接时间和内存消耗 |
| `-flto=auto` | 自动选择 LTO 并行度 | GCC 10+ |
| `-ffat-lto-objects` | 生成同时包含 LTO 和普通代码的目标文件 | 兼容不支持 LTO 的目标文件 |

**LTO 风险**：
- 链接时间显著增加（大项目可能 2-3x）
- 需要 GCC 和链接器版本兼容
- 某些项目可能与 LTO 不兼容（如汇编文件、特定属性）

#### 2e. Profile-Guided Optimization（PGO）（收益高，风险低但流程复杂）

PGO 需要两次构建 + 一次代表性负载运行：

```bash
# 第 1 步：生成 profiling 构建
gcc -fprofile-generate=./profdata -O3 -march=armv8.2-a ...

# 第 2 步：运行代表性负载
./benchmark_workload

# 第 3 步：使用 profile 数据重新构建
gcc -fprofile-use=./profdata -O3 -march=armv8.2-a ...
```

**PGO 收益**：
- 更精确的分支预测（减少 misprediction）
- 更好的函数内联决策
- 更优的代码布局（热点代码集中）
- 典型收益：5-15%

**PGO 复杂性**：
- 需要可运行的代表性负载
- 两次完整构建 + 一次中间运行
- profile 数据与代码版本绑定（代码变更后需重新生成）

#### 2f. 循环优化（收益低-中，风险低）

| 选项 | 说明 | 注意 |
|------|------|------|
| `-funroll-loops` | 自动循环展开 | 可能与手写展开冲突，增大代码体积 |
| `-fvariable-expansion-in-unroller` | 展开时拆分累加器 | 配合 `-funroll-loops` 使用 |

#### 2g. 链接器/二进制优化（发布体积收益，运行时性能需验证）

该类别关注**链接后二进制层面**的优化。`strip` 类选项主要减少磁盘文件体积、发布包大小、文件页缓存占用和调试符号暴露；它不会改变已生成的机器码，也不会减少 `.text` 段大小。不要把 `-s` / `--strip-all` 当作提升 steady-state 热点函数 I-cache 命中的依据。若用户目标是 FAISS 搜索 QPS、延迟或核心计算吞吐，应优先验证 `-O2`/`-O3`、`-march`、LTO/PGO、代码布局等直接影响 `.text` 或执行路径的选项。

**检测方法**：

```bash
# 检查当前二进制是否已 strip
file <binary> | grep "not stripped"
readelf -S <binary> | grep "\.symtab"
# 检查二进制段大小
size <binary>
```

**判断逻辑**：
- `file` 输出包含 "not stripped" → 未 strip，优化空间大
- `readelf -S` 中存在 `.symtab` section → 符号表还在
- 对比 strip 前后的二进制体积：strip 后通常缩小 10-30%，但 `.text` 段通常不变

**优化项**：

| 选项 | 说明 | 注意 |
|------|------|------|
| `-s` / `-Wl,--strip-all` | 去除全部符号表（`.symtab`、`.strtab`），减小 `.so` 文件体积 | 不改变 `.text` 或代码生成；会影响调试、perf 符号化和线上问题定位。仅在发布包体积/符号暴露是目标时推荐 |
| `-Wl,--gc-sections` | 移除未引用的代码段（需配合 `-ffunction-sections -fdata-sections`） | 对静态链接或包含大量未用函数的库收益明显 |
| `-Wl,-O1` | 启用链接器优化级别 1（代码布局重排、hash 优化） | 增加链接时间但不影响运行时 |
| `-Wl,--hash-style=gnu` | 使用 GNU hash 风格加速动态符号查找 | 对动态库加载和符号解析有微收益 |

**`-s` 的适用边界**：
- 符号表（`.symtab` + `.strtab`）在运行时不会被加载到可执行段，因此 strip 不会提升 L1-I cache 中可容纳的实际执行代码量
- Strip 后 `.so` 文件体积缩小，可能改善发布包大小、冷启动文件读取、页缓存压力或符号暴露风险
- Strip 会降低调试和 profiling 可观测性，可能让 `perf report`、core dump 分析和线上定位缺少函数/行号信息
- 对 FAISS 这类长期运行的搜索 benchmark，不能仅凭 strip 前后二进制体积变化声明 QPS 收益；若确有收益，必须用固定数据集、固定 recall 阈值、固定线程/亲和配置做 A/B 实测，并在 evidence 中记录指标

**推荐优先级**：
1. `-Wl,--hash-style=gnu`（低风险，主要影响动态符号查找/加载路径）
2. `-Wl,--gc-sections`（低风险，需配合编译选项；可能减少未引用 `.text`）
3. `-Wl,-O1`（低风险，增加链接时间）
4. `-s`（发布体积/符号暴露优化；不作为默认性能优化，需用户接受调试信息损失）

### 步骤 3：交互式确认

用 `AskUserQuestion` 向用户展示推荐选项，让用户确认：

```json
{
  "questions": [{
    "question": "当前编译选项：${current_flags}\n\n推荐新增选项及预期收益：\n${flag_recommendations}\n\n注意：\n- -ffast-math 可能影响浮点精度（允许结合律、非正规数处理等）\n- -flto 会增加链接时间\n- PGO 需要两次构建 + 一次中间运行\n\n请确认要应用的选项：",
    "header": "编译选项",
    "multiSelect": true,
    "options": [
      {"label": "-s (strip 符号表)", "description": "减小发布二进制体积并去除符号；不改善 .text/I-cache，且会影响调试和 perf 符号化"},
      {"label": "-O3 + -march=armv8.2-a", "description": "升级优化级别和架构选项（安全）"},
      {"label": "-O2 降级（从 -O3）", "description": "降级到 -O2 以缓解 O3 激进内联/展开导致的 I-cache 压力，ARM 平台上 O2 常优于 O3（低风险，建议 A/B 对比验证）"},
      {"label": "-ffast-math", "description": "浮点数学优化（可能影响精度）"},
      {"label": "-flto", "description": "链接时优化（增加链接时间）"},
      {"label": "PGO", "description": "Profile-Guided Optimization（需要两次构建）"}
    ]
  }]
}
```

- 用户确认的选项 → 应用到构建配置
- 用户未选择或拒绝 → 跳过对应选项

### 步骤 4：应用编译选项变更

根据构建系统类型修改对应配置文件：

**CMake 项目**：
```cmake
# 在 CMakeLists.txt 顶层添加/修改
set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -O3 -march=armv8.2-a")
set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -O3 -march=armv8.2-a")
```

**Makefile 项目**：
```makefile
# 在 Makefile 中修改
CFLAGS += -O3 -march=armv8.2-a
CXXFLAGS += -O3 -march=armv8.2-a
```

**Autotools 项目**：
- 优先通过 `./configure CFLAGS="..."` 传入（不修改 configure.ac）
- 若必须修改源文件，修改 `configure.ac` 或 `Makefile.am`

**PGO 特殊处理**：
- 不自动执行 PGO 流程（太复杂且需要代表性负载）
- 仅在构建配置中注释 PGO 步骤供用户手动执行
- 输出中标记 `rebuild_required: true` 和 `pgo_recommended: true`

### 步骤 5：返回结果

将编译选项变更通过 JSON 契约返回。配置文件修改由上游 `apply-optimization` 统一执行。

## 输出

完成后，输出以下 JSON 契约（不要输出其他内容）：

```json
{
  "compiler_flag_result": {
    "success": true,
    "original_flags": "-O2 -g",
    "optimized_flags": "-O3 -march=armv8.2-a -ffast-math -funroll-loops",
    "added_flags": ["-O3", "-march=armv8.2-a", "-ffast-math", "-funroll-loops"],
    "removed_flags": ["-O2"],
    "flag_rationale": {
      "-march=armv8.2-a": "启用Kunpeng-0xd01 支持的 ARM v8.2 指令集",
      "-ffast-math": "允许浮点结合律，有助于自动向量化",
      "-O3": "启用更激进的优化，包含自动循环向量化",
      "-funroll-loops": "自动循环展开，隐藏指令延迟"
    },
    "build_config_changes": [
      {
        "file": "CMakeLists.txt",
        "change_type": "add_flags",
        "description": "在顶层 CMakeLists.txt 中添加优化选项"
      }
    ],
    "modified_files": ["CMakeLists.txt"],
    "rebuild_required": true,
    "pgo_recommended": false,
    "pgo_steps": null,
    "error_message": ""
  }
}
```

PGO 推荐时额外输出：
```json
{
  "pgo_recommended": true,
  "pgo_steps": {
    "generate_flags": "-fprofile-generate=./profdata -O3 -march=armv8.2-a",
    "run_command": "<prepareProject 中的 test_method>",
    "use_flags": "-fprofile-use=./profdata -O3 -march=armv8.2-a",
    "note": "需要两次完整构建 + 一次代表性负载运行"
  }
}
```

失败时：
- `success=false`
- `added_flags` 为空
- `error_message` 具体说明拒绝或失败原因

## 明确拒绝的情况

- 当前编译选项已是该项目的最优配置
- 无法定位构建配置文件
- 构建系统中硬编码了编译选项且无法安全修改
- 项目有严格的编译选项约束（如安全认证、发行版打包规则）
- **当前 CPU 不是Kunpeng-0xd01，但代码生成建议中包含 `-mcpu=tsv110` / `-mtune=tsv110`**（严禁对 LinxiCore 使用 TSV110 微架构参数）

## 规则

- **优先推荐安全选项**：架构选项 > 优化级别 > 循环优化 > LTO > -ffast-math > PGO
- **`-O3 → -O2` 降级也是有效优化**：ARM 平台上 I-cache 较小（鲲鹏 L1I 典型 64KB），O3 的激进内联/展开/函数克隆可能导致性能反降。当项目为大型 C++ 模板代码库且热点分散时，应主动评估 O3→O2 降级
- **CMake 项目的 O3→O2 降级方式**：通过 `string(APPEND CMAKE_CXX_FLAGS_RELEASE " -O2")` 追加在默认 `-O3` 之后（而非替换），GCC 使用命令行最后一个 `-On` 标志生效
- **`-march` 必须与硬件一致**：从 `prepareProject.machine.isa_features` 推导，isa_features 中不存在的特性严禁加入 `-march`（会导致 SIGILL）
- **`-moutline-atomics` 推荐所有多线程程序**：运行时自动探测 LSE，零风险，GCC 10+ 支持
- **-ffast-math 需要用户确认**：可能影响浮点精度，不能默认添加
- **PGO 不自动执行**：流程复杂，仅提供建议步骤
- **不修改 configure 脚本**：autotools 项目优先通过环境变量传入
- **记录变更理由**：每个新增选项必须有 `flag_rationale` 说明
- **严禁盲加 `-mcpu=tsv110` / `-mtune=tsv110`**：必须先通过 `prepareProject.machine.cpu_part_id` 确认 CPU 为Kunpeng-0xd01（0xd01）。Kunpeng-0xd03/0xd06 使用 LinxiCore 微架构，加 tsv110 参数会生成错误调度代码，降低性能
- **未知 CPU 使用 `-mcpu=native`**：无法确定型号时让编译器自动探测，不要手动指定微架构
- 配置文件修改由上游 `apply-optimization` 统一执行，本 Skill 只返回变更内容
- 不做 Git 操作
