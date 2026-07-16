---
name: apply-generic-optimization
description: >
  通用优化执行（兜底 Skill）— 当 apply-optimization 路由到无专用 Skill 的策略时调用。
  处理 bulk-memory-opt（memset/memcpy 替换）、code_hoisting（循环不变量提升）、
  lock_contention（锁竞争优化）、caller-context（调用者上下文优化）等策略。
  触发：当优化策略没有专用 Skill 时使用此兜底方案。
---

# apply-generic-optimization — 通用优化执行（兜底 Skill）

## 触发条件

当 `apply-optimization` 路由到以下策略时调用本 Skill：
- `bulk-memory-opt`：标量循环 → memset/memcpy 替换
- `code_hoisting`：循环不变量提升
- `lock_contention`：锁竞争优化（锁拆分/临界区缩小/读写锁/LSE 原子）
- `caller-context`：调用者上下文优化（批量接口/inline/特化/buffer 复用等）
- 未来新增的无专用 Skill 策略

## 输入

调用方传入的 args JSON 包含以下字段：
- `strategy`：优化策略类型
- `sub_type`：子类型（如 `lock_partition`、`batch_interface`、`inline_candidate`）
- `function`：目标函数名
- `source_file`：目标源文件路径
- `lines`：目标行号范围 `[start, end]`
- `target_arch`：目标架构（`neon`/`sve`/`sme`，可能为 null）
- `suggestion`：优化建议文本（从 synthesize 的 `optimization_point.evidence` 或 p8/p9 的 `optimization_points[].suggestion` 提取。若 args 中缺失，从调用方传入的 `analysis_context` 中按 `strategy` + `sub_type` 查找对应视角输出）
- `analysis_context`：完整的 analyzeHotspot 输出（含 static_analysis、dynamic_analysis、各视角原始 finding），供提取上下文
- `repo`：项目根目录路径
- `build_dir`：构建目录路径（相对 repo，如 `build/`）
- `language`：`c_cpp` | `pure_asm` | `inline_asm`

## 执行步骤

### 步骤 1：读取目标代码

Read `source_file`，定位到 `function` 和 `lines` 范围内的代码。

### 步骤 2：按 strategy 执行优化

#### 2a. bulk-memory-opt

- 识别循环体内的单 store 模式：`for (i=0; i<N; i++) dst[i] = const_val`
- 写值恒定 → `memset(dst, init_val, N * sizeof(*dst))`
- 写值来自连续 load → `memcpy(dst, src, N * sizeof(*dst))`
- 用 Edit 工具替换原循环代码
- 需 `#include <string.h>` 时确认已包含或添加

#### 2b. code_hoisting

- 识别循环体内不依赖迭代变量的计算表达式
- 将计算移到循环前，结果赋给临时变量
- 循环体内引用改为该临时变量
- 用 Edit 工具移动代码位置

#### 2c. lock_contention

根据 `sub_type` 执行对应优化：

| sub_type | 操作 |
|----------|------|
| `lock_partition` | 将共享数据结构拆分为 N 份（N = 线程数），每份配独立锁 |
| `lock_free` | 用 CAS/LSE 原子指令（`__atomic_compare_exchange`）替代 `pthread_mutex_lock` |
| `rwlock` | `pthread_mutex_t` → `pthread_rwlock_t`，读操作用 `_rdlock` |
| `critical_section_reduce` | 将锁外的预处理代码移到 `pthread_mutex_lock` 之前 |
| `lse_atomic` | `ldaxr/stlxr` 自旋循环 → `__atomic_fetch_add` 等 LSE 内置函数 |
| `spin_to_adaptive` | `pthread_spin_lock` → `pthread_mutex_lock`（自适应，减少 CPU 空转） |

**重要约束**：
- 修改锁相关代码后必须保证并发正确性（锁获取/释放成对）
- 数据结构拆分涉及全局变量改数组，所有引用点都需要更新
- 不确定正确性时，标记 `optimization_success: false`，在 error_message 中说明风险

#### 2d. caller-context

根据 `sub_type` 执行对应优化：

| sub_type | 操作 |
|----------|------|
| `batch_interface` | 新增批量接口函数，接收数组 + 长度参数，内部循环替代调用者循环 |
| `inline_candidate` | 函数体 < 10 行 → 直接内联到调用点，删除原函数 |
| `constant_specialization` | 对常量参数创建编译期特化版本，消除运行时判断分支 |
| `buffer_reuse` | 调用者层预分配 buffer，作为输出参数传入，消除每次分配的 malloc/free |
| `output_parameter` | 返回值 new/malloc → 改为输出参数 `func(T* out)`，调用者管理生命周期 |
| `isa_check_hoisting` | `if (has_neon())` 检测从循环内提升到调用者初始化阶段 |
| `raii_hoisting` | RAII 对象（如 `std::lock_guard`）从循环内提升到循环外 |
| `devirtualization` | `virtual` 调用 → `final` 或直接调用（已知具体类型时） |
| `error_path_separation` | 错误处理代码 → `__attribute__((cold))` 标记，移到函数尾部 |
| `forwarding_layer_elimination` | 纯转发中间层函数 → 消除，调用者直接调用底层 |
| `parallelize_calls` | 无依赖的单线程循环 → `#pragma omp parallel for` 并行化 |
| `thread_local_cache` | 每线程重复分配的数据 → `thread_local` 静态缓存 |
| `data_structure_flattening` | 指针链/树 → 数组扁平化（`std::vector` 替代 `std::list`） |
| `template_substitute` | `std::function` → 模板参数，编译期绑定消除类型擦除开销 |

**重要约束**：
- `batch_interface` 需同步修改所有调用者（从 ANALYZE_HOTSPOT_RESULT 的 p8 视角获取调用者列表）
- `inline_candidate` 需确认函数无递归、无外部引用
- 不确定正确性或有遗漏风险时，标记 `optimization_success: false`

### 步骤 3：编译验证

```bash
cd <build_dir> && make -j$(nproc) 2>&1 | tail -30
```

编译失败 → `status: "compilation_failed"`，记录错误。

### 步骤 4：汇编验证（编译通过后，可选）

```bash
objdump -d <binary> | grep -A30 "<function_name>:"
```

确认改动后的指令序列合理（无意外寄存器溢出、无冗余指令）。

## 输出格式

```json
{
  "function": "<function_name>",
  "optimization_point_id": "<opt_point_id>",
  "strategy": "bulk-memory-opt|code_hoisting|lock_contention|caller-context",
  "status": "applied|failed|compilation_failed",
  "optimization_success": true,
  "modified_files": ["<file_path>"],
  "compilation": {
    "attempted": true,
    "ok": true,
    "error": null
  },
  "assembly_check": {
    "performed": true,
    "issues": [],
    "details": ""
  },
  "error_message": null
}
```

## 规则

- **源码修改优先使用 Edit 工具**：先 Read 出精确文本，再 Edit 替换
- **编译失败不回退源文件**：保留变更供下游 fix-code 修复
- **并发正确性不确定时标记失败**：锁相关修改宁可不做也不要引入死锁/数据竞争
- **调用者修改需完整**：`caller-context` 策略修改接口时，所有调用点必须同步更新
- **不做 git 操作**：git 由 verify-optimization 阶段管理
