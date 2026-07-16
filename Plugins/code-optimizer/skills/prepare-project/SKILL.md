---
name: prepare-project
description: 验证项目状态、定位优化目标函数、建立性能基线。适用于用户请求优化某个项目/模块时，作为流水线第一步。
---

# 准备项目

你是一位鲲鹏性能优化流水线的项目准备专家。你的任务是验证目标项目状态、定位优化目标、建立性能基线。

用户调用了 `/prepare-project`，参数为：`$ARGUMENTS`

## 输入

从 `$ARGUMENTS` 解析以下信息：

| 参数 | 必需 | 说明 |
|------|------|------|
| project_path | 是 | 项目根目录绝对路径 |
| target | 否 | 目标模块名或函数名，不提供则全项目扫描 |

## 执行步骤

### 任务初始化

创建本阶段子任务，追踪内部执行进度。执行以下 TaskCreate 并捕获每个返回的 task ID：

- validate_task_id = TaskCreate({ subject: "    └ 验证项目", description: "检查项目路径、构建系统和源文件" })
- compile_task_id = TaskCreate({ subject: "    └ 提取编译参数", description: "提取编译器选项、架构标志和性能flags" })
- machine_task_id = TaskCreate({ subject: "    └ 收集机器信息", description: "收集CPU架构、ISA特性、缓存层次等" })
- locate_task_id = TaskCreate({ subject: "    └ 定位目标", description: "定位优化目标函数/模块的源文件" })
- baseline_task_id = TaskCreate({ subject: "    └ 建立基线", description: "建立编译、测试和性能基线" })
- arch_task_id = TaskCreate({ subject: "    └ 架构分析", description: "分析仓库架构，生成ARCHITECTURE.md" })

### 步骤 1：验证项目

TaskUpdate({ taskId: validate_task_id, status: "in_progress" })

用 Bash 工具执行以下检查，逐项记录结果：

1. **路径检查**：`test -d <project_path>` 确认目录存在
2. **VCS 检查**：`test -d <project_path>/.git` 确认是 Git 仓库
3. **Git 状态**：`git -C <project_path> status --porcelain` 检查是否有未提交变更
   - 如果有未提交变更，向用户报告并停止，提示先 commit 或 stash
4. **构建系统**：检查以下文件是否存在（按优先级）：
   - `CMakeLists.txt` → cmake
   - `Makefile` 或 `makefile` → make
   - `meson.build` → meson
   - `BUILD` 或 `WORKSPACE` → bazel
   - `configure` → autotools
   - 都不存在 → unknown
5. **编译器**：`which gcc && gcc --version | head -1` 或 `which clang && clang --version | head -1`
6. **测试框架**：在源码中 Grep 以下模式：
   - `#include <gtest/gtest.h>` → googletest
   - `#include <catch2/catch_test_macros.hpp>` → catch2
   - `#include <unity.h>` → unity
   - `TEST(` 或 `TEST_F(` → googletest
   - 都未找到 → none

### 步骤 2：提取编译参数

TaskUpdate({ taskId: validate_task_id, status: "completed" })
TaskUpdate({ taskId: compile_task_id, status: "in_progress" })

根据步骤 1 检测到的构建系统，读取构建配置文件提取当前编译参数。

1. **CMake 项目**：
   - 用 Read 工具读取项目根目录及子目录的 `CMakeLists.txt`，提取以下变量：
     - `CMAKE_C_FLAGS`、`CMAKE_CXX_FLAGS`
     - `CMAKE_BUILD_TYPE`（Release/Debug/RelWithDebInfo/MinSizeRel）
     - `CMAKE_EXE_LINKER_FLAGS`
   - 用 Glob 查找 `toolchain.cmake` 或 `*.cmake` 中的编译选项
   - 若 `<project_path>/build/CMakeCache.txt` 存在，用 Bash 提取实际生效的 flags：
     ```bash
     grep -E "CMAKE_C_FLAGS:|CMAKE_CXX_FLAGS:|CMAKE_BUILD_TYPE:|CMAKE_EXE_LINKER_FLAGS:" <project_path>/build/CMakeCache.txt
     ```
   - 若 `<project_path>/build/compile_commands.json` 存在，用 Bash 提取实际编译命令中的 flags：
     ```bash
     cat <project_path>/build/compile_commands.json | python3 -c "import json,sys; cmds=json.load(sys.stdin); flags=set(); [flags.update(a for a in c['command'].split() if a.startswith('-') and not a.startswith('-I')) for c in cmds]; print(' '.join(sorted(flags)))" 2>/dev/null || echo "parse_failed"
     ```

2. **Makefile 项目**：
   - 用 Read 工具读取 `Makefile`（及 `Makefile.inc`、`common.mk` 等），提取：
     - `CFLAGS`、`CXXFLAGS`、`LDFLAGS`
     - `AM_CFLAGS`、`AM_CXXFLAGS`（autotools 生成的 Makefile）
   - 若构建目录干净，可用 Bash dry-run 提取：
     ```bash
     cd <project_path> && make -n 2>&1 | grep -E "gcc|g\+\+" | head -5
     ```

3. **Autotools 项目**：
   - 用 Read 工具读取 `configure.ac` 或 `configure.in`，提取 `AC_PROG_CC`/`AC_PROG_CXX` 相关 flags
   - 用 Read 工具读取 `Makefile.am`，提取 `AM_CFLAGS`、`AM_CXXFLAGS`、`AM_LDFLAGS`
   - 若 `config.log` 存在，用 Bash 提取实际使用的 flags：
     ```bash
     grep -E "^CFLAGS=|^CXXFLAGS=|^LDFLAGS=" <project_path>/config.log
     ```

4. **Meson 项目**：
   - 用 Read 工具读取 `meson.build`，提取：
     - `c_args`、`cpp_args`、`buildtype` 选项
     - `default_options` 中的编译选项

5. **Bazel 项目**：
   - 用 Read 工具读取 `WORKSPACE` 和 `BUILD` 文件，提取 `copts`、`linkopts`
   - 检查 `.bazelrc` 中的编译选项

6. **性能相关标志分类**：从提取的所有 flags 中识别并分类：
   - **优化级别**：匹配 `-O0`/`-O1`/`-O2`/`-O3`/`-Os`，未找到则标记 `unknown`
   - **架构标志**：匹配 `-march=`、`-mcpu=`、`-mtune=`
   - **数学标志**：匹配 `-ffast-math`、`-funsafe-math-optimizations`、`-fno-math-errno`、`-ffinite-math-only`
   - **LTO**：匹配 `-flto`，标记 `lto_enabled: true/false`
   - **PGO**：匹配 `-fprofile-generate`、`-fprofile-use`，标记 `pgo_enabled: true/false`
   - **自动向量化**：`-O3` 或 `-ftree-vectorize` 存在时标记 `enabled`，`-fno-tree-vectorize` 存在时标记 `disabled`，否则 `unknown`

7. **编译参数提取失败时**：不影响 `status`，`compilation` 中对应字段记为 `null`，`performance_flags.optimization_level` 记为 `unknown`

8. **二进制体积检测**（当 `build_ok == true` 时，辅助下游判断 O3 代码膨胀程度）：
   ```bash
   # 检查构建产物的 .text 段大小
   size <project_path>/build/lib*.so 2>/dev/null | grep -E "text|data|bss" | head -5
   # 或使用 objdump 获取精确 .text 段大小
   objdump -h <project_path>/build/lib*.so 2>/dev/null | grep .text | head -5
   ```
   记录到 `compilation.binary_size`：
   - `text_bytes`：.text 段大小（字节），用于判断 O3 代码膨胀程度（> 1MB 时 O3→O2 降级收益可能较大）
   - `total_bytes`：二进制总大小
   - `stripped`：是否已 strip（`file <binary> | grep "not stripped"` 为空 → 已 strip）

### 步骤 3：收集机器信息

TaskUpdate({ taskId: compile_task_id, status: "completed" })
TaskUpdate({ taskId: machine_task_id, status: "in_progress" })

收集当前机器的硬件和系统信息，用于判断优化策略与当前平台是否匹配。

用 Bash 工具执行以下命令，逐项记录结果：

1. **架构类型**：
   ```bash
   uname -m
   ```
   输出示例：`aarch64`、`x86_64`

2. **CPU 型号**：
   ```bash
   # ARM: /proc/cpuinfo 中 "Processor" 字段
   grep "Processor" /proc/cpuinfo | head -1 | cut -d: -f2 | sed 's/^ //'
   # x86: /proc/cpuinfo 中 "model name" 字段
   grep "model name" /proc/cpuinfo | head -1 | cut -d: -f2 | sed 's/^ //'
   # 通用降级
   lscpu | grep "Model name" | head -1 | awk -F: '{print $2}'
   ```

3. **操作系统与内核**：
   ```bash
   uname -s -r          # 如 "Linux 5.14.0-284.11.1.el9_2.aarch64"
   cat /etc/os-release 2>/dev/null | head -5
   ```

4. **ISA 特性检测**（判断支持的 SIMD 指令集）：
   ```bash
   # aarch64：读取 Features 行
   grep "Features" /proc/cpuinfo | head -1
   # x86_64：读取 flags 行
   grep "flags" /proc/cpuinfo | head -1
   # 编译器预定义宏补充检测
   echo "" | gcc -dM -E - 2>/dev/null | grep -E "__ARM_ARCH|__ARM_FEATURE|__AVX|__SSE|__MMX" || echo "compiler_macros_unavailable"
   ```

5. **缓存信息**：
   ```bash
   lscpu | grep -iE "cache|cluster|socket|core|thread" 2>/dev/null
   # 或使用 getconf
   getconf LEVEL1_DCACHE_LINESIZE 2>/dev/null
   getconf LEVEL1_DCACHE_SIZE 2>/dev/null
   getconf LEVEL2_CACHE_SIZE 2>/dev/null
   getconf LEVEL3_CACHE_SIZE 2>/dev/null
   ```

6. **Kunpeng 特定检测**（判断是否为鲲鹏平台及具体型号）：
   ```bash
   grep "CPU implementer" /proc/cpuinfo | head -1
   # 0x48 = Huawei (HiSilicon), 0x41 = ARM Ltd
   grep "CPU part" /proc/cpuinfo | head -1
   # 0xd01 = Kunpeng-0xd01 (TaiShan v110)
   # 0xd03 = Kunpeng-0xd03
   # 0xd06 = Kunpeng-0xd06
   ```

   **鲲鹏型号→微架构文档映射**（Part ID 严格匹配，不依赖 dmidecode Version 字符串（各型号显示相同，无法区分变体））：

   | CPU Part | 芯片型号 | 微架构文档 |
   |----------|---------|-----------|
   | 0xd01 | Kunpeng-0xd01 (TaiShan v110) | `skills/kunpeng_microarch/kunpeng920-microarchitecture.md` |
   | 0xd03 | Kunpeng-0xd03 | `skills/kunpeng_microarch/kunpeng_uarch_b-microarchitecture.md` |
   | 0xd06 | Kunpeng-0xd06 | `skills/kunpeng_microarch/kunpeng950-microarchitecture.md` |

   若 Part ID 不在上表中 → 鲲鹏型号未知，`microarch_file` 记为 `null`，不依赖微架构特定参数，使用通用 ARM 优化策略。

7. **检测结果汇总与平台匹配检查**：
   - 若 `arch == "aarch64"` 且检测到鲲鹏 CPU → 标记 `platform_match = true`，所有 ARM/Kunpeng 优化策略可用
   - 若 `arch == "aarch64"` 但非鲲鹏 → 标记 `platform_match = false`，但 ARM NEON/SVE 优化仍可用（标为 `partial_match`）
   - 若 `arch != "aarch64"` → 标记 `platform_match = false`，输出警告：**当前机器为 `$arch`，不是 ARM aarch64 架构。ARM NEON/SVE/SME 向量化优化和鲲鹏专有编译选项（`-mcpu=tsv110` 等）无法在当前机器上验证。优化后需在 ARM 鲲鹏机器上重新验证。**
   - 该警告信息需在输出的 `warnings` 字段中记录

8. **微架构文档路径**：根据 Part ID 查表，将对应文档的绝对路径（`<pipeline_root>/skills/kunpeng_microarch/<file>.md`）填入输出的 `microarch_file` 字段。查不到则 `null`。

9. **指令性能数据**：若 Part ID 匹配且有对应数据文件，将路径填入 `instruction_perf_file`：
   - `0xd01` (Kunpeng-0xd01) → `<pipeline_root>/skills/kunpeng_microarch/scripts/tsv110_full.json`（207 条指令模式，可用 `python3 query_tsv110.py <指令名>` 查询）
   - `0xd03` (Kunpeng-0xd03) → `<pipeline_root>/skills/kunpeng_microarch/scripts/[REDACTED]_full.json`（29 张表、754 条助记符，可用 `python3 query_uarch_b.py <指令名>` 查询）
   - `0xd06` (Kunpeng-0xd06) → 同上 `[REDACTED]_full.json`（0xd06 复用 0xd03 指令性能数据）
   - 其他型号暂无数据 → `null`
   - 下游可查询单条指令的延迟、吞吐量、端口压力

### 步骤 4：定位目标

TaskUpdate({ taskId: machine_task_id, status: "completed" })
TaskUpdate({ taskId: locate_task_id, status: "in_progress" })

1. 如果用户提供了 `target`（模块名或函数名）：
   - 用 Grep 在项目源码中搜索 `target` 的定义位置（函数定义、结构体定义等）
   - 用 Glob 找到目标模块的源文件（文件名包含 target 的 `.c`/`.cpp`/`.h`/`.s`/`.S` 文件）
   - 用 LSP `findReferences` 或 Grep 找到调用链
   - 用 Grep 搜索与目标相关的测试文件
2. 如果用户未提供 `target`：
   - 用 Glob 列出项目中所有 `.c`/`.cpp`/`.s`/`.S` 源文件
   - 用 Grep 搜索计算密集型模式（嵌套 `for` 循环、矩阵运算等）
   - 将发现的候选函数全部列入 `entry_functions`

### 步骤 5：建立基线

TaskUpdate({ taskId: locate_task_id, status: "completed" })
TaskUpdate({ taskId: baseline_task_id, status: "in_progress" })

1. **构建项目**：
   - cmake 项目：`mkdir -p build && cd build && cmake .. && make -j$(nproc)`
   - make 项目：`make -j$(nproc)`
   - 记录构建结果 `build_ok: true/false`
   - 构建失败时记录错误但不停止

2. **运行测试**（如果 `test_framework != none` 且 `build_ok == true`）：
   - cmake + googletest：`cd build && ctest --output-on-failure`
   - make + googletest：`./test_runner`
   - 记录 `tests_pass: true/false`
   - 测试框架不可用时记录 `tests_pass: null`

3. **性能基线**（如果 `build_ok == true`）：
   - 检查项目是否有 benchmark 脚本或目标（Grep `benchmark`、`perf`、`--bench`）
   - 如果有，运行 benchmark 并记录指标
   - 如果没有，对目标函数所在可执行文件用 `time` / `perf stat` 获取粗略指标
   - 无法获取时记录 `metrics: null`

### 步骤 6：架构分析

TaskUpdate({ taskId: baseline_task_id, status: "completed" })
TaskUpdate({ taskId: arch_task_id, status: "in_progress" })

调用 `analyze-architecture` skill 生成仓库架构文档：

1. 使用 Skill tool，skill 名称为 `analyze-architecture`，参数：`{"project_path": "<project_path>"}`
2. 从返回 JSON 中提取 `file` 字段，即为 ARCHITECTURE.md 的绝对路径
3. 若 `success == true`，将路径填入输出的 `architecture_file` 字段
4. 若 `success == false`，记录 `architecture_file: null` 和错误原因到 warnings

TaskUpdate({ taskId: arch_task_id, status: "completed" })

## 输出

完成后，输出以下 JSON 契约（不要输出其他内容）：

```json
{
  "repo": {
    "path": "<project_path>",
    "vcs": "git|none",
    "build_system": "cmake|make|meson|bazel|autotools|unknown",
    "compiler": "<compiler version or null>",
    "test_framework": "googletest|catch2|unity|none",
    "compilation": {
      "cflags": "<CFLAGS value or null>",
      "cxxflags": "<CXXFLAGS value or null>",
      "ldflags": "<LDFLAGS value or null>",
      "build_type": "Release|Debug|RelWithDebInfo|MinSizeRel|unknown",
      "flag_sources": [
        {
          "file": "CMakeLists.txt",
          "variable": "CMAKE_C_FLAGS",
          "value": "-O2 -g"
        }
      ],
      "performance_flags": {
        "optimization_level": "-O0|-O1|-O2|-O3|-Os|unknown",
        "arch_flags": ["-march=armv8-a"],
        "cpu_flags": ["-mcpu=tsv110"],
        "math_flags": [],
        "lto_enabled": false,
        "pgo_enabled": false,
        "auto_vectorization": "enabled|disabled|unknown"
      },
      "binary_size": {
        "text_bytes": 2097152,
        "total_bytes": 5242880,
        "stripped": false
      }
    }
  },
  "machine": {
    "arch": "aarch64|x86_64|unknown",
    "cpu_model": "<CPU model name>",
    "cpu_implementer": "0x48",
    "cpu_part": "0xd01|0xd03|0xd06|null",
    "isa_features": {
      "simd": ["neon", "sve", "avx2", "avx512f", "sse4_2"],
      "architecture_extensions": ["crc32", "crypto", "dotprod"]
    },
    "cache_info": {
      "l1d_size_kb": 64,
      "l1i_size_kb": 64,
      "l2_size_kb": 512,
      "l3_size_kb": 32768,
      "cache_line_size": 64
    },
    "os": "Linux 5.14.0-284.11.1.el9_2.aarch64",
    "platform_match": "true|false|partial_match",
    "platform_note": "当前机器为 x86_64 架构，非 ARM aarch64。ARM NEON/SVE/SME 向量化优化和鲲鹏专有编译选项无法在当前机器上验证。优化后需在 ARM 鲲鹏机器上重新验证。"
  },
  "target": {
    "module": "<module name or null>",
    "source_files": ["<file1>", "<file2>"],
    "entry_functions": ["<func1>", "<func2>"],
    "call_chains": {
      "<func1>": ["<sub_func1>", "<sub_func2>"]
    }
  },
  "baseline": {
    "build_ok": true,
    "tests_pass": true,
    "metrics": {
      "<func1>_avg_ns": 125000
    }
  },
  "architecture_file": "<project_path>/optimization_reports/architecture/ARCHITECTURE.md",
  "microarch_file": "<pipeline_root>/skills/kunpeng_microarch/kunpeng920-microarchitecture.md 或 null>",
  "instruction_perf_file": "<pipeline_root>/skills/kunpeng_microarch/scripts/tsv110_full.json 或 null>",
  "warnings": [
    "当前机器为 x86_64，不是 ARM aarch64 架构。部分优化策略（NEON/SVE/SME 向量化、-mcpu=tsv110 等）将无法在当前环境验证。"
  ],
  "status": "ready"
}
```

`status` 取值：
- `ready`：项目验证通过，可以进入分析阶段
- `blocked`：存在阻塞问题（路径无效、无 Git、有未提交变更等），需用户处理
- `partial`：部分检查失败但可继续（如构建失败但能定位目标）

## 规则

- 构建失败时不停止——记录 `build_ok: false` 但继续定位目标
- 测试框架不可用时跳过测试运行，记录 `tests_pass: null`
- 基线性能获取不到时记录 `metrics: null`
- Git 工作区有未提交变更时 `status: "blocked"`，提示用户先 commit 或 stash
- 目标函数不存在时 `status: "blocked"`，提示用户确认函数名
- 机器信息收集失败不影响 `status`（仍为 `ready` 或 `blocked`），仅记录 `machine.arch` 为 `unknown`
- 编译参数提取失败不影响 `status`，`compilation` 中对应字段记为 `null`，`performance_flags.optimization_level` 记为 `unknown`
- 优先使用实际生效的 flags（CMakeCache.txt / compile_commands.json / config.log）而非仅读取配置文件，因为配置文件中的值可能被命令行参数覆盖
- 平台不匹配（`machine.platform_match == false`）不会阻塞流水线，但需在 `warnings` 字段中明确提醒用户
