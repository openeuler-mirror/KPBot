---
name: analyze-architecture
description: 代码仓库架构分析——产出 ARCHITECTURE.md 持久化文件，记录项目结构、数据结构、头文件依赖、现有优化位置、架构特征等客观事实。在 PrepareProject 之后执行一次，跨轮复用。
---

# 代码仓库架构分析

你是一位代码仓库架构分析专家。你的任务是对整个仓库进行静态分析，记录客观的架构信息，生成持久化的 Markdown 文件供下游阶段直接 Read 使用。

## 核心原则

- **只记录事实，不做判断**：记录"某个文件第 45 行有 NEON 指令"是事实，判断"使用深度较浅"是主观意见。下游阶段自行解读。
- **不提供优化建议**：本技能只输出"是什么"，不输出"该怎么做"。

## 输入

| 参数 | 必需 | 说明 |
|------|------|------|
| project_path | 是 | 项目根目录绝对路径 |

## 缓存策略

```
1. 检查 <project_path>/optimization_reports/architecture/ARCHITECTURE.md 是否存在
2. 若存在 → 读取文件头部的 Git Commit → 对比当前 HEAD
3. 一致 → 返回 { cached: true }，跳过分析
4. 不一致 → 执行分析，覆盖写入
```

## 执行步骤

### 步骤 1：项目结构

**操作**：
1. `ls -R <project_path>` 获取目录树（控制在 3 层深度）
2. `grep -rn "add_executable\|add_library" CMakeLists.txt` 列出构建目标
3. `grep -rn "target_link_libraries" CMakeLists.txt` 列出模块依赖
4. 按目录分组文件，列出各目录文件数量和类型
5. `wc -l` 列出各源文件行数

**输出到 ARCHITECTURE.md**：
```markdown
## 1. 项目结构

### 目录
| 目录 | 文件数 | 说明 |
|------|--------|------|
| `src/core/` | 25 | .cpp/.h |
| `src/utils/` | 12 | .cpp/.h |
| `include/` | 15 | .h |
| `tests/` | 8 | .cpp |
| `benchmarks/` | 3 | .cpp |

### 构建目标
| 目标 | 类型 | 源文件目录 | 链接依赖 |
|------|------|-----------|---------|
| mylib | 静态库 | src/core/ | src/utils/ |
| benchmark_matmul | 可执行文件 | benchmarks/ | mylib |

### 文件行数
| 文件 | 行数 |
|------|------|
| `src/core/matrix.cpp` | 2340 |
| `src/core/vector.cpp` | 856 |
| `include/types.h` | 312 |
| ... | ... |

### 语言
C++（.cpp 120 个，.h 45 个），汇编（.s 0 个，.S 0 个）
```

### 步骤 2：数据结构盘点

**操作**：
1. `grep -rn "^struct\|^class" <include_dir>` 扫描类型定义
2. Read 每个结构体的完整定义，记录成员类型和顺序
3. 判断数据组织方式：AoS（struct 内数组元素交织）、SoA（分离数组）
4. `grep -rn "^[a-zA-Z_].*\[" <src_dir> --include="*.cpp" --include="*.c"` 查找文件级数组
5. `grep -rn "alignas\|__attribute__((aligned))\|posix_memalign" <src_dir>` 查找对齐声明

**输出到 ARCHITECTURE.md**：
```markdown
## 2. 数据结构

### 结构体/类定义

#### Matrix (`include/matrix.h:15`)
```
struct Matrix {
    float* data;   // 8 bytes (64-bit 指针)
    int rows;      // 4 bytes
    int cols;      // 4 bytes
    int stride;    // 4 bytes
};
```
- 成员总大小: 20 bytes（不含 padding）
- 数据组织: AoS（data 指向行优先二维 float 数组）

#### Vector3D (`include/vector.h:8`)
```
struct Vector3D {
    float x;       // 4 bytes
    float y;       // 4 bytes
    float z;       // 4 bytes
};
```
- 成员总大小: 12 bytes
- 数据组织: AoS

### 文件级数组

| 变量 | 文件 | 类型 | 大小 | 存储期 | 可变 |
|------|------|------|------|--------|------|
| `float g_workspace[4096]` | `src/common.cpp:7` | float[4096] | 16 KB | static | 是 |
| `const double lut[256]` | `src/lookup.cpp:3` | double[256] | 2 KB | static | 否 |

### 对齐声明
| 位置 | 声明 | 边界 |
|------|------|------|
| `src/core/matrix.cpp:23` | `posix_memalign(&data, 64, size)` | 64 bytes |
```

### 步骤 3：头文件依赖

**操作**：
1. 统计各头文件在项目源码中被 #include 的次数：
   ```bash
   for h in $(find <project_path> -name "*.h" -not -path "*/build/*" -not -path "*/third_party/*"); do
       count=$(grep -r "#include.*$(basename $h)" <project_path> --include="*.cpp" --include="*.c" --include="*.h" 2>/dev/null | grep -v "$(basename $h)" | wc -l)
       echo "$count $h"
   done | sort -rn | head -10
   ```
2. 检查 Top-10 头文件之间是否存在相互引用（A includes B 且 B includes A）

**输出到 ARCHITECTURE.md**：
```markdown
## 3. 头文件依赖

### 被引用次数（Top-10）
| 头文件 | 被引用次数 |
|--------|-----------|
| `include/common.h` | 25 |
| `include/types.h` | 18 |
| `include/matrix_ops.h` | 12 |
| `include/vector_ops.h` | 8 |
| `include/allocator.h` | 6 |
| ... | ... |

### 循环依赖
无
```
（若有循环依赖：列出 A → B → A 的路径）

### 步骤 4：现有优化位置

**目的**：记录代码中已经存在的优化技术及其位置，不评价使用深度。

**操作**：
1. NEON：
   ```bash
   grep -rn "v[a-z][a-z_]*_q_\|vld[1-4]q\|vst[1-4]q\|v[a-z]*q_[a-z]" <src_dir> --include="*.cpp" --include="*.c" --include="*.h"
   ```

2. SVE：
   ```bash
   grep -rn "sv[a-z][a-z_]*_\|svld1\|svst1\|svwhilelt\|svcnt" <src_dir> --include="*.cpp" --include="*.c"
   ```

3. 内联汇编：
   ```bash
   grep -rn "__asm__\|asm volatile\|__asm" <src_dir> --include="*.cpp" --include="*.c" --include="*.h" --include="*.s" --include="*.S"
   ```

4. 软件预取：
   ```bash
   grep -rn "__builtin_prefetch\|pld\|prfm" <src_dir> --include="*.cpp" --include="*.c"
   ```

5. 展开 pragma：
   ```bash
   grep -rn "#pragma.*unroll\|#pragma.*clang.*loop.*unroll" <src_dir> --include="*.cpp" --include="*.c"
   ```

6. 缓存分块宏：
   ```bash
   grep -rn "BLOCK_SIZE\|TILE_SIZE\|BLOCKSIZE\|TILE_" <src_dir> --include="*.cpp" --include="*.c" --include="*.h"
   ```

7. restrict 限定：
   ```bash
   grep -rn "__restrict\|restrict" <src_dir> --include="*.cpp" --include="*.c" --include="*.h"
   ```

**输出到 ARCHITECTURE.md**：
```markdown
## 4. 现有优化位置

### NEON intrinsics
| 文件:行号 | 上下文 |
|-----------|--------|
| `src/matmul.cpp:45` | `vmlaq_f32` |
| `src/matmul.cpp:48` | `vld1q_f32` |
| `src/matmul.cpp:52` | `vst1q_f32` |
| `src/vector.cpp:12` | `vadd_f32` |

### SVE intrinsics
无

### 内联汇编
无

### 软件预取
无

### 展开 pragma
| 文件:行号 | pragma |
|-----------|--------|
| `src/matmul.cpp:60` | `#pragma GCC unroll 4` |

### 缓存分块宏
| 文件:行号 | 宏 | 值 |
|-----------|-----|-----|
| `src/matmul.cpp:30` | `BLOCK_SIZE` | 64 |

### restrict 限定
无
```

### 步骤 5：架构特征

**目的**：记录可能影响编译器优化行为的代码特征，不解释影响、不给建议。

**操作**：
1. 文件级可变数组（结合步骤 2 结果）
2. `grep -rn "volatile" <src_dir> --include="*.cpp" --include="*.c" --include="*.h"` 列出非 MMIO 场景的 volatile 使用
3. `grep -rn "reinterpret_cast\|\(float\*\)\|\(int\*\)" <src_dir> --include="*.cpp" --include="*.c"` 列出类型转换
4. 从步骤 1 的行数统计中列出 >2000 行的文件
5. 从步骤 1 的行数统计中列出 >200 行的函数

**输出到 ARCHITECTURE.md**：
```markdown
## 5. 架构特征

### 文件级可变数组
| 变量 | 文件:行号 | 类型 | 大小 |
|------|----------|------|------|
| `float g_workspace[4096]` | `src/common.cpp:7` | float[4096] | 16 KB |

### volatile 使用
| 文件:行号 | 上下文 |
|-----------|--------|
| `src/sensor.cpp:45` | `volatile int* mmio_reg` |

### 类型转换
| 文件:行号 | 转换 |
|-----------|------|
| `src/matmul.cpp:78` | `(float*)data` |

### 巨型文件（>2000 行）
| 文件 | 行数 |
|------|------|
| `src/core/matrix.cpp` | 2340 |

### 巨型函数（>200 行）
| 函数 | 文件:行号 | 行数 |
|------|----------|------|
| `matmul_naive` | `src/matmul.cpp:15-250` | 236 |
```

## 输出

### 写入文件

```bash
mkdir -p <project_path>/optimization_reports/architecture
# 写入完整 ARCHITECTURE.md
```

文件头部格式：
```markdown
# <项目名> 架构分析

> 分析时间: <ISO8601> | Git Commit: <sha> | 缓存有效
```

### 返回 JSON

```json
{
  "success": true,
  "cached": false,
  "git_commit": "abc123...",
  "file": "<project_path>/optimization_reports/architecture/ARCHITECTURE.md",
  "findings_count": 5
}
```

`findings_count` 为记录的客观发现条目总数（结构体数 + 头文件数 + 优化位置数 + 特征数）。

## 规则

- 本技能**不修改任何代码**，仅分析并写入 Markdown 文件
- 找不到源码时标记 `[未找到]`
- **只记录位置、数值、名称、类型**，不写评级（浅/中/深）、建议、影响分析、优化路线
- Markdown 文件中不使用 ⚠️/🔴/🟡/★/← 等引导性符号
- 缓存判断优先：检查文件存在 + git_commit 匹配
