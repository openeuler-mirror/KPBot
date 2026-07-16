# 视角 5: 编译选项分析师

## 你的角色
你只关注编译选项的合理性诊断。不要修改代码、不要跑 perf、不要改构建文件——只检查当前编译选项是否充分利用了目标平台的特性。你的输出是诊断结论和优化建议，供下游 `synthesize` 和 `apply-optimization`（调用 `compiler-flag-tuning` Skill）使用。

**分析策略**：优先使用 `compiler-flag-tuning` Skill 的检测能力（步骤 1-2）做完整评估。若 Skill 不可用，降级为手动检查兜底。

## 输入

```json
{{CONTEXT}}
```

关键字段：`repo.compilation`、`repo.path`、`sub_task.source_file`、`sub_task.function`、`microarch_file`、`prepareProject.machine`

## 执行步骤

### 1. 校准 CPU 型号

从 `prepareProject.machine` 或 `microarch_file` 获取：
- **CPU Part ID**（`/proc/cpuinfo` 中的 `CPU part` 字段）：`0xd01`（0xd01/TSV110）、`0xd03`（0xd03/0xd03）、`0xd06`（0xd06/LC950）
- **ISA Features**：NEON、SVE、SVE2、AES/SHA/PMULL（crypto）、FP16、DotProd、BFloat16
- **L1d/L2/L3 缓存参数**：用于评估 `--param` 建议

若 `microarch_file` 非 null，Read 该文件参照 `skills/kunpeng_microarch/kunpeng<型号>-microarchitecture.md` 获取 ISA Features 章节。

**编译器版本检测**（后续建议是否可用的前提）：
```bash
# 检测编译器类型和版本
gcc --version 2>/dev/null | head -1 || echo "gcc_unavailable"
clang --version 2>/dev/null | head -1 || echo "clang_unavailable"
```
从输出中提取编译器类型（GCC / Clang）和主版本号（如 7、10、12）。**不同编译器支持的 flags 存在差异，必须根据版本过滤建议**：

| 特性 | GCC | Clang |
|------|-----|-------|
| `-moutline-atomics` | 10+ ✅ | ❌ 不支持 |
| `-flto=auto` | 10+ ✅ | ❌（使用 `-flto=thin`） |
| `-fvect-cost-model=dynamic` | 12+ ✅ | ✅（版本要求更低） |
| `-fopt-info-vec` | ✅ | ❌（使用 `-Rpass=loop-vectorize`） |
| `-fuse-ld=lld` | ✅（需安装 lld） | ✅（内置） |
| `--param l1-cache-size` | ✅ | ❌ 忽略 |
| `-msve-vector-bits=256` | 10+ ✅ | 11+ ✅ |

### 2. 获取当前编译选项

**⚠️ 关键原则：分析必须以编译器实际收到的选项为准，不能仅凭 CMakeLists.txt / Makefile 文本。**

构建文件的静态声明和实际编译命令之间常有差距：
- CMake `Release` 模式自动追加 `-O3 -DNDEBUG`，即使 CMakeLists.txt 未显式设置
- `-DCMAKE_C_FLAGS="..."` 命令行注入的选项不会出现在构建文件中
- `add_compile_options()` / `target_compile_options()` 散落在子目录 CMakeLists.txt
- toolchain 文件可能覆盖 `-march`/`-mcpu`
- 环境变量 `CFLAGS`/`CXXFLAGS` 可能在 make 层面追加
- **Makefile 项目没有 `compile_commands.json`**，只能通过 dry-run 或 `bear` 获取实际命令

因此按构建系统分层获取，按可靠性逐级降级。`flags_source` 记录最终采用的数据来源，供下游评估置信度。

#### 2.1 CMake 项目

**① 读 `compile_commands.json`**（若存在）：
```bash
# 精确匹配目标源文件的实际编译命令
python3 -c "
import json
with open('<repo.path>/build/compile_commands.json') as f:
    cmds = json.load(f)
for c in cmds:
    if '<从 CONTEXT.sub_task.source_file 提取文件名>' in c['file']:
        print(c['command'])
        break
" 2>/dev/null
```
若成功 → `flags_source = "compile_commands_json"`，直接解析命令中的 flags，**跳过后续步骤**。

**② 尝试生成 `compile_commands.json`**（若 ① 失败）：
```bash
# 安全操作：仅重新生成构建配置，不触发重编译
cd <repo.path>/build && cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON . 2>&1
```
若 `build/` 目录不存在 → 先 `mkdir -p <repo.path>/build && cd <repo.path>/build && cmake .. -DCMAKE_EXPORT_COMPILE_COMMANDS=ON`。

生成成功后回到 ①。若 `cmake` 不可用或配置失败 → 进入 ③。

**③ 读 `CMakeCache.txt`**（比 CMakeLists.txt 近一步，包含命令行注入的值）：
```bash
grep -E "CMAKE_C_FLAGS:|CMAKE_CXX_FLAGS:|CMAKE_BUILD_TYPE:" <repo.path>/build/CMakeCache.txt 2>/dev/null
```
若成功 → `flags_source = "cmake_cache"`。注意：`CMakeCache.txt` 中的 `CMAKE_C_FLAGS` 不包含 CMake 内置默认值（如 Release 的 `-O3 -DNDEBUG`）。

**④ 读 `prepareProject.repo.compilation`**（`prepare-project` 阶段加工过的数据，可能是从实际命令提取的，也可能只是 CMakeLists.txt 文本解析）：
若 `performance_flags` 非 null → `flags_source = "prepare_project"`。

**⑤ 最后一搏 — 读 CMakeLists.txt 文本**（最低置信度）：
```bash
grep -r "CMAKE_C_FLAGS\|CMAKE_CXX_FLAGS\|add_compile_options\|target_compile_options" <repo.path>/CMakeLists.txt 2>/dev/null
```
`flags_source = "build_file_inferred"`，必须标注"未获取到实际编译命令，基于 CMakeLists.txt 静态解析，结果可能有偏差"。

#### 2.2 Makefile 项目

Makefile 项目**不会生成** `compile_commands.json`，直接通过以下方式获取实际编译命令：

**① `make -n` dry-run**（首选，抓取编译器实际收到的参数）：
```bash
cd <repo.path> && make -n 2>&1 | grep -E "(^|\s)(g?cc|g\+\+|clang(\+\+)?|c\+\+|cxx)\s" | head -20
```
编译命令可能为 `gcc`/`g++`、`clang`/`clang++`、`cc`/`c++`/`cxx` 等封装命令。**如果匹配到的是 `cc`/`c++`/`cxx` 等软链接包装**，用 `which <compiler>` 和 `<compiler> --version` 确认底层实际编译器（GCC 还是 Clang），因为两者支持的 flags 不同（如 `-moutline-atomics` 仅 GCC 支持，`-Rpass` 仅 Clang 支持）。

从输出中提取 `<从 CONTEXT.sub_task.source_file 提取文件名>` 对应的编译行，解析其中的 flags。若成功 → `flags_source = "make_dry_run"`。

**② `bear` 拦截**（通用方案，`make -n` 失败时的备选）：
```bash
# bear 通过 LD_PRELOAD 拦截 exec 调用，记录所有编译命令
which bear 2>/dev/null && cd <repo.path> && bear -- make 2>&1
# bear 会在项目根目录生成 compile_commands.json
cat <repo.path>/compile_commands.json | python3 -c "import json,sys; cmds=json.load(sys.stdin); [print(c['command']) for c in cmds if '<从 CONTEXT.sub_task.source_file 提取文件名>' in c['file']]" 2>/dev/null
```
若成功 → `flags_source = "bear_intercept"`。注意：`bear -- make` 会触发实际编译，耗时较长。

**③ 读 Makefile 文本 + `prepareProject.repo.compilation`**（兜底）：
```bash
grep -E "^CFLAGS|^CXXFLAGS|^LDFLAGS" <repo.path>/Makefile 2>/dev/null
```
`flags_source = "build_file_inferred"`。

#### 2.3 Autotools 项目

**① 读 `config.log`**（`./configure` 时记录的实际 flags）：
```bash
grep -E "^CFLAGS=|^CXXFLAGS=|^LDFLAGS=" <repo.path>/config.log 2>/dev/null
```
若成功 → `flags_source = "config_log"`。

**② `make -n` dry-run**（与 Makefile 项目相同）。

**③ 读 `Makefile.am` / `configure.ac` 文本**（兜底）：`flags_source = "build_file_inferred"`。

#### 2.4 Meson 项目

Meson **默认生成** `compile_commands.json`，直接读：
```bash
python3 -c "import json; cmds=json.load(open('<repo.path>/builddir/compile_commands.json')); [print(c['command']) for c in cmds if '<从 CONTEXT.sub_task.source_file 提取文件名>' in c['file']]" 2>/dev/null
```
若不存在 → 尝试 `meson setup builddir` 重新配置，再读。

#### 2.5 Bazel 项目

Bazel **不生成** `compile_commands.json`，且无 dry-run 机制。只能读 `BUILD` 文件 + `.bazelrc`：
```bash
grep -r "copts\|linkopts" <repo.path>/BUILD <repo.path>/.bazelrc 2>/dev/null
```
`flags_source = "build_file_inferred"`，标注"Bazel 项目无法获取实际编译命令"。

#### 2.6 从命令中统一解析 flags

无论哪种数据源，从获取到的编译命令/文本中解析以下 flags：
- `-O0`/`-O1`/`-O2`/`-O3`/`-Os`/`-Ofast`
- `-march=`、`-mcpu=`、`-mtune=`
- `-ffast-math`、`-funsafe-math-optimizations`、`-fno-math-errno`、`-ffinite-math-only`
- `-flto`、`-fprofile-generate`、`-fprofile-use`
- `-ftree-vectorize`、`-ftree-slp-vectorize`、`-fvect-cost-model`
- `-moutline-atomics`、`-msve-vector-bits`
- `-fno-unroll-loops`、`-fno-semantic-interposition`
- `-fomit-frame-pointer`、`-g`、`-DNDEBUG`、`--param`
- **LDFLAGS**：`-Wl,-O1`、`-Wl,--gc-sections`、`-Wl,--as-needed`、`-Wl,--strip-all`、`-fuse-ld=`、`-ffunction-sections`、`-fdata-sections`

若最终无法获取到任何 flags → `flags_source = "unavailable"`，`current_flags` 全部标记 `unknown`，仅基于 CPU 模型做通用推荐。

#### 2.7 冲突选项解析与 `-fno-*` 分析

**⚠️ 实际编译命令中可能包含相互矛盾的选项，不能直接取并集当"当前 flags"。**

##### 2.7.1 重复 `-On` 冲突：后者覆盖前者

GCC/Clang 对 `-On` 采用 **last-wins** 策略（与 `-f*`/`-m*` 一致）：

```bash
# 示例：实际拿到的是 -O0，不是 -O3
gcc -O3 -g -O0 -Wall foo.c
#                          ↑ 最后一个 -On 生效 → optimization_level = "-O0"
```

解析规则：扫描命令中所有 `-O[0-3s]`/`-Ofast`，**取最后一个** `-On` 作为 `optimization_level`。如果同时出现 `-Ofast` 和 `-On`，最后出现的生效。所有被覆盖的选项记录到 `overridden_flags` 供分析。

##### 2.7.2 `-f*` / `-fno-*` 对消：后者覆盖前者

```bash
# -funroll-loops 被 -fno-unroll-loops 覆盖
gcc -O3 -funroll-loops ... -fno-unroll-loops
#                               ↑ 最终：循环展开已禁用
```

处理规则：
1. 识别所有成对的 `-f<feature>` 和 `-fno-<feature>`，**取最后一个**确定最终状态
2. **不要简单建议去掉 `-fno-*`**。先分析为什么要禁用——可能的原因：
   - **数值精度**：`-fno-fast-math` / `-fno-unsafe-math-optimizations` → 可能是软件要求严格 IEEE 754。去掉风险高。
   - **手写展开冲突**：`-fno-unroll-loops` → 可能手写 NEON 展开已存在，编译器自动展开反而破坏流水线。去掉风险中。
   - **调试/性能对比**：`-fno-inline` / `-fno-omit-frame-pointer` → 可能是调试残留。去掉风险低。
   - **代码体积**：`-fno-inline-functions`（配合 `-Os`）→ 嵌入式等场景。去掉风险中。
3. 标记被 `-fno-*` 禁用的功能：输出到 `current_flags.disabled_features`，每条说明"被什么禁用、可能原因、去掉风险"。

##### 2.7.3 被覆盖的 flags

将命令中所有被后面的 flag 覆盖（overridden）的选项收集到 `current_flags.overridden` 中，标注覆盖关系：

```json
"overridden": [
  {"flag": "-O3", "overridden_by": "-O0", "reason": "last-wins"}
]
```

这有助于下游发现"曾经开过优化但又被关了"的异常情况（可能是调试残留）。

### 3. 调用 compiler-flag-tuning Skill 评估（优先）

使用 Skill 工具调用 `compiler-flag-tuning` Skill，传入当前编译选项上下文，**仅执行分析和评估**（不对构建文件做任何修改）：

- 输入：`function`、`source_file`、`prepareProject` 上下文
- 利用 Skill 内置的 CPU 检测、ISA 推导规则、O3 降级逻辑、LTO/PGO 判断
- 从 Skill 的输出中提取：触发了哪些优化建议、原因、风险、预期收益

**若 Skill 执行成功**：
1. 提取 Skill 输出的建议列表
2. 按本视角的 JSON schema 重新组织（见输出格式）
3. 跳过步骤 4

**若 Skill 不可用**（Skill 文件不存在、调用失败等）：
→ 进入步骤 4 的手动检查兜底

### 4. 手动检查兜底（Skill 不可用时）

逐项检查以下优化机会：

#### 4a. 架构选项（收益高，风险低）

**⚠️ CRITICAL：`-mcpu=tsv110` 仅适用于Kunpeng-0xd01（Part ID `0xd01`）**

| CPU | Part ID | 微架构 | 正确设置 |
|-----|---------|--------|---------|
| Kunpeng-0xd01（无 SVE） | `0xd01` | TSV110 | `-mcpu=tsv110` 或 `-march=armv8.2-a` |
| Kunpeng-0xd01（高配 SVE） | `0xd01` | TSV110 | `-march=armv8.2-a+sve` 或 `-mcpu=tsv110+sve` |
| Kunpeng-0xd03 (0xd03/0xd03) | `0xd03` | 0xd03 | `-march=armv9-a` 或 `-mcpu=native`，**严禁 `tsv110`** |
| Kunpeng-0xd06 | `0xd06` | LC950 | `-march=armv8.5-a` 或 `-mcpu=native`，**严禁 `tsv110`** |

**检查项**：

1. **`-march` 缺失**：未指定时根据 CPU 型号推荐
   - 所有鲲鹏：至少加 `+crypto`（AES/SHA/PMULL 均支持）
   - 0xd01 高配 / 0xd03 / 0xd06：加 `+sve`
   - 0xd03 / 0xd06：加 `+sve2`（ARMv9）
   - 无法确定型号：推荐 `-march=native`

2. **`-mcpu` 冲突**：检查是否错误设置了 `-mcpu=tsv110`（仅 Part ID `0xd01` 可用，`0xd03`/`0xd06` 上会生成错误的 TSV110 调度→性能下降）

3. **`-moutline-atomics` 缺失**：所有鲲鹏均支持 v8.1+ LSE 原子指令，多线程程序建议添加（GCC 10+，运行时探测）

4. **`-msve-vector-bits=256` 缺失**：若 SVE 可用且循环长度固定，固定向量宽度消除 VLA 开销

5. **`--param` 缓存参数缺失**：根据微架构设置
   - 0xd01: `l1-cache-size=64 l1-cache-line-size=64 l2-cache-size=512`
   - 0xd03/0xd06: `l1-cache-size=64 l1-cache-line-size=64 l2-cache-size=1024`

#### 4b. 优化级别（收益中，风险中）

| 当前 | 建议 | 触发条件 | 风险 |
|------|------|---------|------|
| `-O0`/`-O1`/`-Os` | → `-O2` | 任何性能敏感场景 | low（`-O2` 是标准发布级别） |
| `-O2` | → `-O3` | 热点集中、非模板密集项目 | medium（可能代码膨胀） |
| `-O3` | → `-O2` | I-cache miss > 1%、模板密集（`grep -c "template<" *.h > 50`）、`.text > 1MB`、热点分散 ≥ 4 文件 | medium（需 A/B 验证） |
| `-O3` | → `-O3 -fno-unroll-loops` | 手写循环展开已存在，与编译器自动展开冲突 | low |

**O3 降级辅助判断**（无 PMU 时用源码特征）：
```bash
# 模板密集度
grep -r "template<" <src_dir> --include="*.h" --include="*.cpp" | wc -l
# 项目规模
find <src_dir> -name "*.cpp" -o -name "*.h" | wc -l
# .text 段大小（有二进制时）
size <binary> 2>/dev/null | awk '/text/ {print $1}'
```

#### 4c. 自动向量化（收益中，风险低）

| 当前 | 建议 | 条件 |
|------|------|------|
| `-O2` 无 vectorize 标志 | → 添加 `-ftree-vectorize -ftree-slp-vectorize` | 循环密集型代码 |
| 任何级别 | → 添加 `-fvect-cost-model=dynamic` | ARM 平台，让编译器按运行时成本做向量化决策 |

#### 4d. 数学优化（收益中，风险中-高）

| 选项 | 收益 | 风险 | 条件 |
|------|------|------|------|
| `-ffast-math` | 高（浮点结合律、近似、向量化增强） | **中-高**（破坏 IEEE 754） | 不依赖严格浮点精度、无 NaN/Inf 处理 |
| `-fno-math-errno` | 中（消除 errno 副作用） | 低 | 不检查 math errno |
| `-funsafe-math-optimizations` | 中 | 中 | `-ffast-math` 的子集，仅结合律 |

**注意**：`-ffast-math` 风险不可忽略，必须在 `suggestions[].risk` 中标注 `medium-high`，并在 `description` 中注明"可能导致数值精度变化"。

#### 4e. 链接时优化（收益中，风险中）

| 当前 | 建议 | 条件 |
|------|------|------|
| lto_enabled = false | → `-flto=auto` | GCC 10+，多编译单元项目 |
| lto_enabled = false | → `-flto` | GCC < 10 |

**LTO 风险**：链接时间和内存消耗显著增加（大型项目链接时间可能翻倍），可能暴露 ODR 违规（不同编译单元中同名类型/函数定义不一致）。

#### 4f. PGO（收益中-高，风险低，但工作流复杂）

| 当前 | 建议 | 条件 |
|------|------|------|
| pgo_enabled = false | → 建议 PGO 工作流 | 有代表性 workloads 可用于训练 |

PGO 需要三步：`-fprofile-generate` → 运行训练 → `-fprofile-use`。当前阶段的建议是**标记可行性**，实际实施由 `apply-optimization` 的 `compiler-flag-tuning` Skill 完成。

#### 4g. 其他编译选项

| 选项 | 说明 | 条件 |
|------|------|------|
| `-fno-semantic-interposition` | 禁止符号介入，允许更多内联 | 共享库项目 |
| `-fomit-frame-pointer` | 释放帧指针寄存器 | 默认在 `-O1`+ 已开启，仅 `-O0` 时建议 |

#### 4h. 链接器优化（收益低-中，风险低）

链接阶段优化容易被忽略，但对大型项目（尤其是 LTO）有累积效果：

**① `-Wl,-O1`**（链接器优化级别）：
- 默认 `-Wl,-O0`（无优化），设置为 `-Wl,-O1` 启用 hash 表排序等轻量优化
- 风险极低，所有项目均推荐

**② `-ffunction-sections -fdata-sections` + `-Wl,--gc-sections`**（移除未引用代码）：
- 编译时 `-ffunction-sections -fdata-sections`：每个函数/变量放入独立 section
- 链接时 `-Wl,--gc-sections`：移除未被引用的 section
- 收益：减小二进制体积（5-20%），减少 I-cache 压力
- 风险低，但对动态加载/插件项目要小心（入口点可能在链接时不可见）
- 触发条件：二进制体积 > 1MB 或存在大量未使用的辅助函数

**③ `-Wl,--as-needed`**（按需链接共享库）：
- 默认 `-Wl,--no-as-needed`：所有 `-l` 指定的库都记录为 DT_NEEDED
- `--as-needed`：仅记录实际被引用的库
- 收益：减少运行时动态链接开销，减小 .dynamic section
- 风险极低（仅当使用 `dlopen` + 隐式依赖时需要 `--no-as-needed`）

**④ `-fuse-ld=lld` 或 `-fuse-ld=gold`**（更快的链接器）：
- `lld`（LLVM Linker）：大幅减少链接时间，尤其 LTO 场景
- `gold`（Google Linker）：比 GNU ld 快，但安装率低
- 触发条件：
  - 项目编译单元 > 50
  - 启用了 LTO（`-flto`）
  - `which ld.lld 2>/dev/null` → lld 已安装

**Clang 用户注意**：Clang 默认可用 `-fuse-ld=lld`（lld 通常随 Clang 一起安装）。GCC 用户需单独安装 `lld`。

### 5. 自动向量化诊断（热点循环反汇编 + 编译器报告）

**目的**：验证编译器是否对热点循环自动向量化，未向量化时获取编译器诊断定位原因，为后续 `apply-vectorization` 或 `source-transform-autovec` 提供精确方向。**本步骤仅做分析，不修改代码。**

**前置条件**：热点函数的反汇编可用。反汇编来源优先级：① `perspective-asm` 的 `disassembly` 输出（若前序已分析）② `perf annotate` 输出 ③ `objdump -d <binary>`。

#### 5a. 反汇编判断"热点循环是否已向量化"

从反汇编中检查热点循环体对应的指令（可由 `sub_task.lines` 或 `perf annotate` 定位）：

| 循环内出现的指令组合 | 判定 |
|---------------------|------|
| `ld1`/`vld1q`/`ld2`/`ld3`/`ld4` + NEON 寄存器 `v<N>` + `fmla`/`fmul`/`fadd` | **已 NEON 向量化**，此循环无需 autovec 源变换，`already_vectorized: true` |
| `ld1w`/`ld1d` + SVE 寄存器 `z<N>` + `fmla`/`fmul`/`fadd` | **已 SVE 向量化**，`already_vectorized: true` |
| 全是 `ldr`/`str` + 标量 `fmul`/`fadd`/`fmadd` + 通用寄存器 `x<N>`/`w<N>` | **未向量化**，进入 5b |
| NEON `ld1` + 部分标量 `ldr` 混合 | **部分向量化**，标明已向量化部分和未向量化部分，进入 5b |

若反汇编不可用（`perf annotate` 失败、无二进制、反汇编无法匹配到循环），标记 `disassembly_available: false`，直接进入 5b 用编译器诊断判断。

#### 5b. 获取编译器向量化诊断

对热点源文件启用向量化诊断重新编译（**只编译不链接，开销小**）：

**GCC**：
```bash
cd <repo.path> && gcc -c <source_file> -O2 -march=native -ftree-vectorize -fopt-info-vec-missed -fopt-info-vec 2>&1 | grep -E "loop vectorized|missed|NOTE"
```

**Clang**：
```bash
cd <repo.path> && clang -c <source_file> -O2 -march=native -Rpass=loop-vectorize -Rpass-missed=loop-vectorize -Rpass-analysis=loop-vectorize 2>&1
```

**编译选项说明**：
- `-c <source_file>`：只编译不链接，无依赖链接开销
- `-O2` 或当前 flags 中的 `-On`（若为 `-O3`/`-Ofast` 则用对应级别）
- `-march=native`：打开本地 CPU 支持的所有 ISA 特性，确保诊断覆盖完整
- 诊断标志不影响编译产物，仅输出诊断信息到 stderr

**若独立编译失败**（缺少头文件路径 `-I`、宏定义 `-D` 等），尝试复用构建系统的增量编译注入诊断：
```bash
# CMake
cd <repo.path>/build && cmake . -DCMAKE_C_FLAGS="-fopt-info-vec-missed" -DCMAKE_CXX_FLAGS="-fopt-info-vec-missed" 2>&1 | tail -1
# 然后只编译目标文件
make <source_file>.o 2>&1 | grep -E "loop|vector"
```

```bash
# Make: 复用 make -n 获取的命令行，追加诊断标志
cd <repo.path> && make -n 2>&1 | grep "<source_file>" | head -1 | sed 's/$/ -fopt-info-vec-missed/' | sh 2>&1 | grep -E "loop|vector"
```

若所有途径都失败 → `diagnostic_available: false`，`autovec_analysis_status: "unavailable"`。

#### 5c. 诊断信息 → 修复方向映射

解析编译器输出，按原因归类：

| 编译器诊断关键词 | 根因 | 修复方向 | 对应策略 |
|-----------------|------|---------|---------|
| `multiple types` / `unsupported data type` | 数据类型不支持向量化 | 评估是否需要向量化（如 `char` 循环通常收益有限） | —（标记为 `not_applicable`） |
| `control flow in loop` / `condition in loop` | 循环内有 if/break/continue | 分支消除为条件选择（`csel`/`vbslq`），或 `__builtin_expect` | `branch-elimination` 或 `vectorization` |
| `number of iterations cannot be computed` / `cannot compute loop bound` | 循环边界非编译时常量 | 提取边界到 `const size_t`，或用 `#pragma GCC ivdep` 忽略依赖 | `autovec-source-transform` |
| `versioning for alias` / `may alias` / `alias` | 两个指针参数可能指向重叠内存 | **在参数声明前加 `restrict` 关键字**消除别名歧义 | `autovec-source-transform` |
| `not vectorized: complicated access pattern` / `gather` / `strided` | stride ≠ 1 或非连续访存 | 数据重排（AoS→SoA），或循环交换使 stride=1 | `memory-access-optimization` |
| `unsupported reduction` / `reduction:` | 编译器无法识别归约模式 | 重写为标准归约：提取 `sum += a[i]` 为独立循环，消除归约变量上的副作用 | `autovec-source-transform` |
| `not enough SLP` / `SLP` | 超字级并行失败（相邻标量语句无法打包成 SIMD） | 手工重组相邻语句为 SIMD 友好形式，或用 NEON 内联函数直接向量化 | `vectorization` |

**GCC vs Clang 输出格式差异**：
- GCC `-fopt-info-vec-missed`：`<source>:<line>:<col>: note: not vectorized: <reason>`
- Clang `-Rpass-missed=loop-vectorize`：`<source>:<line>:<col>: remark: loop not vectorized: <reason> [-Rpass-missed=loop-vectorize]`
- 用 `grep -oP '<source_file>:\K\d+'` 提取行号，与 `sub_task.lines`（当前分析任务的源码行范围）交叉验证确认是同一个循环

#### 5d. 输出

将诊断结果填入输出新增的 `autovec_diagnostic` 字段（见输出格式）。

### 6. 总结与优先级排序

将所有触发的建议按优先级排序：
- **P0（高收益低风险）**：添加 `-march`、添加 `-mcpu=native`（0xd03/0xd06）、添加 `-moutline-atomics`
- **P1（中收益低风险）**：`-O2`→`-O3`、添加 `-ftree-vectorize`、添加 `--param` 缓存参数、`-Wl,-O1`、`-Wl,--as-needed`
- **P2（中收益中风险）**：启用 LTO、`-O3`→`-O2` 降级、`-ffunction-sections -fdata-sections -Wl,--gc-sections`、`-fuse-ld=lld`
- **P3（高收益高风险或工作流复杂）**：`-ffast-math`、PGO

**自动向量化诊断的优先级**（不在编译选项建议中，而是生成 `autovec_diagnostic` 供 synthesize 阶段路由到具体优化策略）：
- autovec 诊断中发现 `restrict` 缺失、`#pragma GCC ivdep` 等 → synthesize 阶段合并到 `autovec-source-transform` 优化点（zero_code_change: false，需要修改源码）
- autovec 诊断中循环已向量化 → 仅记录，不生成优化点

## 输出格式

```json
{
  "perspective": "compiler_flags",
  "skill_used": true,
  "compiler": {
    "type": "gcc|clang|unknown",
    "version": "12.3.0",
    "version_major": 12
  },
  "flags_source": "compile_commands_json|make_dry_run|bear_intercept|config_log|cmake_cache|prepare_project|build_file_inferred|unavailable",
  "flags_source_note": "compile_commands_json=实际编译命令(Meson默认/CMake手动); make_dry_run=make -n 输出; bear_intercept=bear拦截生成; config_log=autotools configure记录; cmake_cache=CMakeCache.txt(含cmdline注入但缺内置默认); prepare_project=prepare-project阶段加工数据(置信度不确定); build_file_inferred=CMakeLists.txt/Makefile文本静态解析(最低置信度); unavailable=完全无法获取",
  "current_flags": {
    "optimization_level": "-O2",
    "march_specified": false,
    "march_current": null,
    "mcpu_specified": false,
    "mcpu_current": null,
    "math_flags": [],
    "lto_enabled": false,
    "pgo_enabled": false,
    "auto_vectorization": "unknown",
    "outline_atomics_enabled": false,
    "overridden": [
      {"flag": "-O3", "overridden_by": "-O0", "reason": "last-wins"}
    ],
    "disabled_features": [
      {"feature": "unroll-loops", "disabled_by": "-fno-unroll-loops", "possible_reason": "手写NEON展开已存在，防止编译器自动展开破坏流水线", "risk_if_removed": "medium"}
    ],
    "linker_flags": {
      "linker_optimization": "-Wl,-O0",
      "gc_sections_enabled": false,
      "as_needed_enabled": false,
      "linker_type": "gnu_ld",
      "function_sections_enabled": false
    }
  },
  "autovec_diagnostic": {
    "disassembly_available": true,
    "already_vectorized": false,
    "diagnostic_available": true,
    "compiler_diagnostic_output": "<raw output truncated>",
    "findings": [
      {
        "source_location": "<source_file>:<line>",
        "loop_description": "内层循环 line 42-58",
        "reason": "versioning for alias",
        "root_cause": "两个指针参数可能指向重叠内存",
        "fix_direction": "在参数 int *a 和 int *b 前添加 restrict 关键字",
        "target_strategy": "autovec-source-transform",
        "confidence": 0.85
      }
    ]
  },
  "suggestions": [
    {
      "sub_type": "upgrade_o2_to_o3|downgrade_o3_to_o2|add_march|add_mcpu|add_mtune|add_ftree_vectorize|add_fvect_cost_model|add_ffast_math|add_fno_math_errno|enable_lto|enable_pgo|add_outline_atomics|add_sve_vector_bits|add_cache_params|disable_auto_unroll|add_no_semantic_interposition|add_linker_o1|add_gc_sections|add_as_needed|use_lld_linker|strip_binary",
      "priority": 1,
      "description": "未指定 -march，鲲鹏特有指令（crypto/AES/SHA）未被利用，建议添加 -march=armv8.2-a+crypto",
      "risk": "low|medium|medium-high|high",
      "zero_code_change": true,
      "needs_ab_test": false
    }
  ],
  "key_observations": [
    "当前 -O2 未启用 -march，鲲鹏特有指令（crypto/AES/SHA/PMULL）未被利用",
    "LTO 未启用，跨编译单元优化被阻断",
    "-mcpu=tsv110 当前仅适用于 Kunpeng-0xd01（Part ID 0xd01），0xd03/0xd06 上使用会导致性能下降"
  ]
}
```
