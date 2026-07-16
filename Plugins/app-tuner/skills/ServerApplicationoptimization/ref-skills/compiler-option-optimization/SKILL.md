---
name: compiler-option-optimization
description: 优化C/C++应用的编译配置以提升目标应用在ARM64/AArch64平台的运行时性能。当用户提到ARM64/AArch64优化、ARM NEON/SVE/SME优化、编译选项优化、LTO、PGO、PLT、大页优化时使用此skill。
---

# C/C++应用编译优化（ARM64平台）

此skill帮助您**优化**C/C++应用的编译配置以提升目标应用在ARM64平台的运行时性能。支持GCC和Clang编译器，以及CMake和Make构建系统，专注于ARM64架构的极致性能优化。

**重要说明**：
- 此skill专注于**运行时性能最大化**，会推荐激进的优化选项
- 激进优化可能带来副作用：增加编译时间、增大二进制文件、可能影响调试能力
- 此skill只提供优化建议和配置示例，不会主动修改您的工程代码
- 所有修改都需要您手动应用，建议在测试环境验证后再部署到生产环境

## 工作流程

创建ARM64优化配置涉及5个主要阶段：

1. **项目分析** — 识别构建系统、定位配置文件、分析现有编译器标志
2. **CPU特性检测** — 检测目标CPU支持的ARM64特性，生成对应的编译器标志
3. **应用运行时分析** — 分析函数热点和微架构数据
4. **优化配置生成** — 根据CPU特性、运行时特征，生成优化的CMake/Make配置
5. **验证和测试** — 验证优化配置是否正确应用，测试性能提升效果

---

## 阶段1：项目分析

### 目标
了解项目结构和当前构建配置，识别优化机会。

### 步骤

#### 1.1 识别构建系统

检查项目目录中的构建文件：

```bash
find . -maxdepth 3 \( -name "CMakeCache.txt" -o -name "CMakeLists.txt" -o -name "Makefile" -o -name "Makefile.am" \)
```

**支持的构建系统**：
- **CMake**：优先查找`CMakeCache.txt`，然后才是`CMakeLists.txt`文件
- **Make**：优先查找`Makefile`然后才是`Makefile.am`文件
- **混合**：同时支持CMake和Make

#### 1.2 分析现有编译器标志

**对于CMake项目**：
```bash
# 从CMakeCache提取编译器标志
grep -E "CMAKE_CXX_FLAGS|CMAKE_C_FLAGS|CMAKE_BUILD_TYPE" CMakeCache.txt

# 从CMakeLists.txt提取自定义标志
grep -rn "CMAKE_CXX_FLAGS\|CMAKE_C_FLAGS\|target_compile_options\|add_compile_options" --include="CMakeLists.txt" .
```

**对于Make项目**：
```bash
# 查找编译器标志设置
grep -E "CXXFLAGS|CFLAGS|LDFLAGS" Makefile
```

#### 1.3 识别编译器类型

```bash
# 检测使用的编译器
gcc --version 2>/dev/null && echo "GCC detected"
clang --version 2>/dev/null && echo "Clang detected"
```

GCC和Clang在ARM64优化选项上有差异，后续阶段需根据编译器类型分别推荐。

#### 1.4 识别优化机会

检查以下内容：
- 当前优化级别（-O0, -O1, -O2, -O3）
- 架构标志（-march, -mcpu, -mtune）
- LTO启用状态（-flto）
- 向量化启用状态（-ftree-vectorize / -fvectorize）
- ARM64特定标志（NEON, SVE, SME）
- 符号可见性（-fvisibility, -fno-semantic-interposition）
- 链接时优化（-Bsymbolic, --as-needed）

### 输出
- 构建系统类型
- 编译器类型（GCC/Clang）及版本
- 主要构建文件路径
- 现有编译器标志分析

---

## 阶段2：CPU特性检测

### 目标
检测目标CPU支持的ARM64特性，生成对应的编译器标志。

### 步骤

#### 2.1 运行lscpu检测

```bash
lscpu
```

**重点关注字段**：
- **Architecture**：确认是aarch64
- **CPU(s)**：CPU核心数
- **Model name**：CPU型号
- **Flags**：CPU支持的特性列表

#### 2.2 分析CPU特性

**基本ARM64特性**：
- `fp`：浮点支持（始终启用）
- `asimd`：NEON SIMD（始终启用）

**加密扩展**：
- `aes`、`pmull`、`sha1`、`sha2` → `+crypto`
- `sha3` → `+sha3`（ARMv8.2+）
- `sm3`、`sm4`→ `+sm4`（ARMv8.2+）

**SVE（可缩放向量扩展）**：
- `sve` → `+sve`（ARMv8.2+）
- `sve2` → `+sve2`（ARMv8.4+）
- `svebf16` → `+svebf16`
- `sveaes`、`svepmull` → `+sve`相关扩展

**SME（可缩放矩阵扩展）**：
- `sme` → `+sme`（ARMv9）

**原子操作**：
- `atomics` → `+lse`

**其他特性**：
- `crc32` → `+crc`
- `lse` → `+lse`
- `dotprod` → `+dotprod`
- `bf16` → `+bf16`
- `i8mm` → `+i8mm`

#### 2.3 确定ARMv8版本

根据CPU Flags确定ARMv8版本：
- 基本特性：`fp asimd` → `armv8-a`
- ARMv8.1：`atomics` → `armv8.1-a`
- ARMv8.2：`sve`或`uscat` → `armv8.2-a`
- ARMv8.3：`paca`或`pacg` → `armv8.3-a`
- ARMv8.4：`sve2`或`sha3` → `armv8.4-a`
- ARMv8.5：`sb`或`ssbs` → `armv8.5-a`
- ARMv9：`sme` → `armv9-a`

#### 2.4 构造-march标志

将ARMv8版本与CPU特性组合为完整的`-march`标志：

```
-march=<armv_version>+<feature1>+<feature2>+...
```

**示例**：CPU支持`atomics`、`sve`、`crc32`、`aes`、`sha1`、`sha2`：
```
-march=armv8.2-a+crypto+crc+lse+sve
```

**GCC vs Clang差异**：
- GCC：`+crypto` 是组合特性（包含aes+sha1+sha2+pmull）
- Clang：`+crypto` 同样支持，但某些版本需单独指定 `+aes,+sha2`
- GCC：`+lse` 对应 `atomics` flag
- Clang：`+lse` 同样支持，也可用 `-moutline-atomics` / `-mno-outline-atomics`

### 输出
- CPU型号和特性
- ARMv8版本
- 构造的`-march`标志（区分GCC和Clang）

---

## 阶段3：应用运行时分析

### 目标
分析应用运行时热点函数和微架构指标。

### 步骤

#### 3.1 采集应用函数热点

**必须先询问用户提供目标应用的PID**。使用AskUserQuestion工具向用户询问PID，并提供以下选项：
- 选项1：用户输入PID（用户选择"Other"并提供PID数值）
- 选项2：跳过此阶段

如果用户提供了PID，使用脚本`scripts/perf_hotspot.sh`采集热点：

```bash
bash ~/.claude/skills/compiler-option-optimization/scripts/perf_hotspot.sh <PID>
```

如果用户选择跳过或未提供PID，则跳过本阶段，后续阶段4.2中基于热点函数的推荐将不可用，需在报告中标注"未采集热点数据，热点相关推荐不可用"。

#### 3.2 采集应用的微架构指标

仅在3.1中用户提供了PID时执行。若3.1已跳过，则本步骤一并跳过，后续阶段4.3中基于微架构指标的推荐将不可用，需在报告中标注"未采集微架构数据，指标相关推荐不可用"。

```bash
perf stat -ddd -p "$PID" -- sleep 5
```

**重点关注指标**：
- **IPC**：Instructions Per Cycle，衡量CPU流水线效率
- **branch-misses %**：分支预测失败率
- **L1-dcache-load-misses %**：L1数据缓存未命中率
- **L1-icache-load-misses %**：L1指令缓存未命中率
- **iTLB-load-misses %**：指令TLB未命中率
- **dTLB-load-misses %**：数据TLB未命中率

### 输出
- 热点函数列表（top 50）（如3.1已执行）
- 微架构指标数据（如3.2已执行）
- 若3.1/3.2均跳过，输出标注"阶段3已跳过：未提供PID，运行时分析数据不可用"

---

## 阶段4：优化配置生成

### 目标
根据CPU特性、运行时特征，生成优化的CMake/Make配置。

### 步骤

#### 4.1 根据当前编译标记推荐编译选项

- **模式a**：`CMAKE_BUILD_TYPE`是Debug → 推荐`CMAKE_BUILD_TYPE=Release`
- **模式b**：编译选项低于`-O3` → 推荐`-O3`
- **模式c**：未设置`-mtune` → 推荐`-mtune=native`

#### 4.2 根据热点函数推荐编译选项

- **模式a**：热点函数中有PLT函数调用（如`func@plt`）→ 推荐`-fno-plt`减少函数调用开销
- **模式b**：热点函数中出现`__aarch64_cas*`、`__aarch64_ldadd`等辅助函数 → 推荐`-mno-outline-atomics`关闭兼容模式对LSE的调用，减少调用开销
- **模式c**：在模式b基础上，根据并发规模选择LSE或LL/SC：
  - 多核（>4核）并发场景：`-march=...+lse`（LSE原子指令吞吐更高）
  - 小规模（<=4核）并发场景：`-march=...+nolse`（LL/SC在此场景延迟更低）

#### 4.3 根据微架构指标推荐编译选项

- **模式a**：`iTLB-miss >= 1%` → 推荐代码段大页优化：`-Wl,-z,common-page-size=2097152 -Wl,-z,max-page-size=2097152`
- **模式b**：`dTLB-miss >= 1%` → 推荐数据段大页优化：`-Wl,-z,common-page-size=2097152 -Wl,-z,max-page-size=2097152`
- **模式c**：`L1i-cache-miss >= 1%` 或 `L1d-cache-miss >= 1%` 或 `branch-miss >= 1%` → 推荐 PGO+LTO 优化
- **模式d**：`IPC <= 3` → 推荐 PGO+LTO 优化

#### 4.4 推荐通用优化选项

以下优化选项适用于大多数ARM64应用，建议默认启用：

| 选项 | 效果 | GCC | Clang |
|------|------|-----|-------|
| `-fno-semantic-interposition` | 允许编译器内联PLT存根，减少函数调用开销 | >= 5.1 | >= 6.0 |
| `-fvisibility=hidden` | 减少动态符号表大小，加快动态链接 | Yes | Yes |
| `-fvisibility-inlines-hidden` | 隐藏内联函数符号 | Yes | Yes |
| `-Wl,-Bsymbolic` | 链接时绑定符号到本地定义，避免PLT查询 | Yes | Yes |
| `-fno-plt` | 消除PLT间接调用，直接使用GOT | Yes | Yes |

#### 4.5 LTO优化配置

**GCC LTO**：
```cmake
set(CMAKE_INTERPROCEDURAL_OPTIMIZATION TRUE)
# 或
set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -flto=auto")
set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -flto=auto")
set(CMAKE_EXE_LINKER_FLAGS "${CMAKE_EXE_LINKER_FLAGS} -flto=auto -fuse-ld=gold")
```

**Clang Thin LTO**（推荐，比Full LTO编译更快、内存更低）：
```cmake
set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -flto=thin")
set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -flto=thin")
set(CMAKE_EXE_LINKER_FLAGS "${CMAKE_EXE_LINKER_FLAGS} -flto=thin")
```

#### 4.6 PGO优化工作流

PGO（Profile-Guided Optimization）需要三次构建，步骤如下：

**GCC PGO**：
```bash
# 步骤1：生成插桩二进制
cmake -DCMAKE_C_FLAGS="-fprofile-generate=/tmp/pgo_data" \
      -DCMAKE_CXX_FLAGS="-fprofile-generate=/tmp/pgo_data" \
      -DCMAKE_EXE_LINKER_FLAGS="-fprofile-generate=/tmp/pgo_data" ..
make -j$(nproc)

# 步骤2：运行代表性工作负载采集profile
./your_program --run-representative-workload

# 步骤3：使用profile重新编译
cmake -DCMAKE_C_FLAGS="-fprofile-use=/tmp/pgo_data -fprofile-correction" \
      -DCMAKE_CXX_FLAGS="-fprofile-use=/tmp/pgo_data -fprofile-correction" \
      -DCMAKE_EXE_LINKER_FLAGS="-fprofile-use=/tmp/pgo_data -fprofile-correction" ..
make -j$(nproc)
```

**Clang PGO**：
```bash
# 步骤1：生成插桩二进制
cmake -DCMAKE_C_FLAGS="-fprofile-instr-generate=/tmp/pgo_data/default_%m.profraw" \
      -DCMAKE_CXX_FLAGS="-fprofile-instr-generate=/tmp/pgo_data/default_%m.profraw" \
      -DCMAKE_EXE_LINKER_FLAGS="-fprofile-instr-generate" ..
make -j$(nproc)

# 步骤2：运行代表性工作负载采集profile
./your_program --run-representative-workload

# 步骤3：合并profile数据
llvm-profdata merge -o /tmp/pgo_data/merged.profdata /tmp/pgo_data/*.profraw

# 步骤4：使用profile重新编译
cmake -DCMAKE_C_FLAGS="-fprofile-instr-use=/tmp/pgo_data/merged.profdata" \
      -DCMAKE_CXX_FLAGS="-fprofile-instr-use=/tmp/pgo_data/merged.profdata" ..
make -j$(nproc)
```

**PGO+LTO联合优化**：在步骤3的CMAKE_EXE_LINKER_FLAGS中同时添加LTO标志即可。

#### 4.7 大页运行时配置

编译时指定大页对齐后，还需运行时配置：

```bash
# 查看当前大页配置
cat /proc/meminfo | grep Huge

# 分配大页（需要root）
sudo sysctl -w vm.nr_hugepages=1024

# 使用libhugetlbfs自动映射（推荐）
# 编译时链接：-lhugetlbfs
# 运行时：HUGETLB_MORECORE=yes ./your_program

# 或使用透明大页（无需修改程序）
echo always | sudo tee /sys/kernel/mm/transparent_hugepage/enabled
```

### 输出
- 推荐优化配置（区分GCC和Clang）
- PGO工作流命令（如适用）
- 大页运行时配置（如适用）

---

## 阶段5：编译验证和性能测试

### 目标
验证优化配置是否正确应用，测试性能提升效果。

### 步骤

#### 5.1 验证编译器标志

**对于CMake项目**：
```bash
cd build
make VERBOSE=1 2>&1 | grep -o '\-march=[^ ]*' | sort -u
make VERBOSE=1 2>&1 | grep -o '\-O[0-3s]' | sort -u
```

**对于Make项目**：
```bash
make VERBOSE=1 2>&1 | grep -o '\-march=[^ ]*' | sort -u
```

#### 5.2 检查二进制文件

```bash
# 查看二进制架构
file your_binary

# 查看二进制大小（对比优化前后）
size your_binary

# 查看是否使用了SVE/LSE等指令
objdump -d your_binary | grep -cE "ldadd|ldclr|ldeor|ldset|swp|cas" && echo "LSE instructions found"
objdump -d your_binary | grep -cE "\swhilelo\s|\sptrue\s" && echo "SVE instructions found"

# 查看动态符号数量（-fvisibility=hidden后应显著减少）
readelf -Ws your_binary | grep -c "FUNC.*GLOBAL"
```

#### 5.3 性能测试

**使用项目自带的基准测试**：
```bash
./benchmark --benchmark_filter=all
```

**使用perf对比优化前后**：
```bash
# 优化前
perf stat -ddd -o before.perf ./your_program

# 优化后
perf stat -ddd -o after.perf ./your_program

# 对比关键指标
perf diff before.perf after.perf
```

---

## 最终输出报告（强制模板）

分析完成后**必须**按以下模板输出：

```markdown
# ARM64编译优化报告

## 项目信息
- **项目名称**：<项目名>
- **构建系统**：<CMake/Make>
- **编译器**：<GCC x.x.x / Clang x.x.x>
- **目标CPU**：<CPU型号>
- **ARM架构**：<armv8.x-a>

## 当前配置
| 项目 | 当前值 |
|------|--------|
| 优化级别 | -O? |
| 架构标志 | -march=... |
| LTO | 启用/未启用 |
| PGO | 启用/未启用 |

## 优化建议

### 高优先级（预期收益显著）
| # | 优化项 | 标志 | 适用条件 | 预期收益 |
|---|--------|------|----------|----------|
| 1 | ... | ... | ... | ... |

### 中优先级（需验证收益）
| # | 优化项 | 标志 | 适用条件 | 预期收益 |
|---|--------|------|----------|----------|
| 1 | ... | ... | ... | ... |

### 低优先级（场景特定）
| # | 优化项 | 标志 | 适用条件 | 预期收益 |
|---|--------|------|----------|----------|
| 1 | ... | ... | ... | ... |

## 配置示例

### CMake
```cmake
# 优化配置
```

### Make
```makefile
# 优化配置
```

## 副作用说明
| 优化项 | 编译时间影响 | 二进制大小影响 | 可移植性影响 | 调试影响 |
|--------|-------------|---------------|-------------|---------|
| ... | ... | ... | ... | ... |

## 性能预期
- **整体性能提升**：<预估范围>
- **主要收益来源**：<哪些优化贡献最大>
- **需验证项**：<哪些优化需要实际测试确认收益>
```

---

## 常见陷阱

### 性能优化陷阱
- **不要盲目使用-Ofast**：可能破坏浮点语义和IEEE 754标准合规性（-Ofast = -O3 + -ffast-math）
- **避免对模板密集代码使用-O3**：可能导致过长的编译时间（可能增加10倍以上）
- **不要混合调试和发布标志**：保持配置分离，避免调试困难
- **小心使用-march=native**：创建不可移植的二进制文件，只能在相同CPU上运行
- **不要忽略编译器警告**：特别是使用LTO时，警告通常指示真正的问题

### 副作用陷阱
- **不要忽视编译时间增长**：-O3和-flto可能导致编译时间增加10倍以上
- **不要忽视二进制大小增长**：-O3可能导致二进制大小增加50%以上
- **不要忽视内存使用增长**：-flto可能导致编译时内存使用增加2-3倍，可能触发OOM
- **不要忽视可移植性损失**：-march=native和特定CPU标志会显著降低可移植性
- **不要忽视调试困难**：-fomit-frame-pointer和-O3会使调试和堆栈跟踪变得困难

### LTO陷阱
- **不要在增量构建中使用LTO**：LTO需要重新链接所有目标文件，失去增量编译优势
- **不要忽视链接器兼容性**：某些链接器可能不完全支持LTO的所有特性
- **不要忽视隐藏bug**：LTO的全局优化可能暴露在非LTO构建中不可见的bug（如ODR违反）
- **不要在调试构建中使用LTO**：会使调试信息变得不完整或不可用

### PGO陷阱
- **不要使用非代表性的工作负载**：PGO基于运行时数据，非代表性工作负载会导致错误优化
- **不要忽视profile过时**：代码变化后需要重新生成profile
- **不要在CI/CD中滥用PGO**：额外的构建和运行步骤可能显著增加CI时间
- **不要忽略PGO的构建复杂度**：需要两次完整编译和运行工作负载

### 大页优化陷阱
- **不要在没有大页资源时启用**：会导致运行时分配失败
- **不要在小型应用中使用**：收益不明显，但会浪费大页资源
- **不要忽略root权限要求**：配置大页需要系统管理员权限
- **不要忽略内存占用增加**：大页会有内部碎片，实际内存占用可能显著增加

---

## 故障排除

### 编译错误处理

**LTO相关问题**：
```bash
# 禁用LTO或使用兼容的链接器
# CMake: set(CMAKE_INTERPROCEDURAL_OPTIMIZATION FALSE)

# 或使用gold链接器
cmake -DCMAKE_EXE_LINKER_FLAGS="-fuse-ld=gold" ..

# GCC LTO内存不足时，限制并行链接任务
make -j1 LDFLAGS="-flto=auto -flto-partition=1to1"
```

**SVE相关问题**：
```c
// 使用运行时检测，避免在不支持SVE的CPU上崩溃
#ifdef __ARM_FEATURE_SVE
    // SVE代码
#else
    // 回退代码
#endif
```

**大页相关问题**：
```bash
# 检查大页配置
cat /proc/meminfo | grep Huge

# 增加大页数量
sudo sysctl -w vm.nr_hugepages=1024

# 检查透明大页状态
cat /sys/kernel/mm/transparent_hugepage/enabled
```

### 性能问题诊断

**性能未提升**：
```bash
# 检查编译器是否应用了优化
gcc -O3 -fopt-info-all -c source.c

# 查看关键函数的汇编代码
objdump -d your_binary | grep -A 30 "<hot_function>:"

# 使用perf分析热点
perf record -g ./your_program
perf report
```

**内存使用过高**：
```bash
# 检查二进制大小
size your_binary

# 使用strip减小大小
strip your_binary

# 检查符号表
nm --size-sort your_binary | tail -20
```

---

## 参考资源

### 官方文档
- **ARM Architecture Reference Manual**: https://developer.arm.com/documentation/ddi0487/latest
- **ARM C Language Extensions**: https://developer.arm.com/documentation/101028/latest
- **SVE Programming Guide**: https://developer.arm.com/documentation/101284/latest
- **NEON Intrinsics Reference**: https://developer.arm.com/documentation/101075/latest

### 编译器文档
- **GCC ARM Options**: https://gcc.gnu.org/onlinedocs/gcc/ARM-Options.html
- **Clang ARM Options**: https://clang.llvm.org/docs/UsersManual.html#arm-specific-options
- **LTO Documentation**: https://gcc.gnu.org/wiki/LinkTimeOptimization
- **PGO Documentation**: https://gcc.gnu.org/onlinedocs/gcc/Instrumentation-Options.html

### 优化资源
- **ARM Performance Reports**: https://developer.arm.com/documentation/101985/latest
- **Optimization Guide**: https://developer.arm.com/documentation/100748/latest
