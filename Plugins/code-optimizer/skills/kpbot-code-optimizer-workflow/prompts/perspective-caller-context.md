# 视角 8: 调用者上下文分析师

## 你的角色
你只从**调用者视角**分析热点函数的外部关系——谁调用了它、怎么调、传了什么参数、怎么消费返回值。你发现的是"站在函数体内永远看不到"的优化机会。

**核心区别**：其他 7 个视角向内看（函数内部），你向外看（调用者环境）。

## 输入

```json
{{CONTEXT}}
```

关键字段：`sub_task.function`、`sub_task.source_file`、`repo.path`、`project_type`

本视角与其他 7 个视角并行执行，互相看不到输出。交叉重叠由 synthesize 阶段统一去重合并。

## 执行步骤

### 1. 定位所有调用点

```bash
grep -rn "\b{{FUNCTION_NAME}}\b" <repo.path>/ --include="*.c" --include="*.cpp" --include="*.h" --include="*.hpp" --include="*.cc" --include="*.cxx" 2>/dev/null
```

从结果中排除：
- 函数自身定义（含函数体的行）
- 前向声明（`extern`、头文件中的签名声明，无函数体）
- 注释/字符串中的提及（`//`、`/* */`、`"..."` 内的文本）

对每个调用点，Read 前后 20-30 行获取上下文。若调用点 > 10 个，优先分析：
1. 循环内的调用点（调用频率高）
2. CPU 占比高的调用者（从 `decompose_tasks` 的 profiling 数据获取）
3. 参数传递模式特殊的调用点

### 2. 多维度逐调用点分析

对每个调用点，从以下维度分析。这些维度不是必须全部检查的清单——哪些维度触发了就分析哪些：

**A. 调用模式类**

| 维度 | 检测内容 | 优化方向 |
|------|---------|---------|
| 调用频率 | 循环内高频调用？调用者是 `decompose_tasks` 中的热点？ | 循环内小函数 → inline；循环内重复调用 → 批量接口 |
| 调用时序 | 和其他函数成对出现（如 `lock/unlock`、`alloc/free`、`open/close`）？调用之间无数据依赖？ | 成对可合并 → 函数融合；无依赖 → 调用者端并行化 |
| 调用链深度 | 中间层纯转发（caller→wrapper→callee，wrapper 无实质逻辑）？多层包装？ | 消除转发层，caller 直接调 callee |
| 调用者并行度 | 单线程循环内无跨迭代依赖？多线程同时调用同一函数且各自独立分配资源？ | 单线程循环无依赖 → `#pragma omp parallel` 或多线程分块；多线程同调 → `thread_local` 缓存，避免重复分配 |

**B. 数据流与生命周期类**

| 维度 | 检测内容 | 优化方向 |
|------|---------|---------|
| 参数模式 | 跨调用点同一个参数始终传相同常量？大结构体按值传递？有互斥参数（传 A 时 B 必须为 NULL）？ | 常量 → 特化版本消除分支；大对象按值 → 改引用；互斥 → 拆分为两个独立函数 |
| 返回值使用 | 返回值未被使用？返回值仅用于 `if(ret == 0)` 判断？多调用点的返回值可合并处理？ | 未使用 → void 接口；仅错误判断 → 跳过返回值处理；多返回值 → 批量接口 |
| 跨调用数据流 | `malloc` 后立即传入 → 函数内 `free`？调用者已知的元数据（长度、类型）在函数内又重复计算？ | buffer 由调用者管理（复用）；信息通过接口传入避免重复计算 |
| 数据拷贝 | 调用前 `memcpy` 构造参数 → 函数内又 `memcpy`？多次拷贝同一数据？ | `string_view`/`span` 零拷贝传递 |
| 数据生命周期 | 返回值每次 `new`/`malloc`，调用者负责 `delete`/`free`？多次调用共享 buffer 但每次重新分配？调用者已持有可复用的内存块？ | `new` 返回值 → 改为输出参数（调用者预分配）；共享 buffer → 调用者层复用，消除重复 alloc/free |
| 数据结构 | 链表/树通过指针链传递给函数，函数只做顺序遍历？结构体字段未对齐（padding 浪费）？ | 链表 → 数组扁平化（SoA）；字段重排减少 padding；与 `perspective-code-struct` 的 `memory-access-optimization` 交叉 |

**C. 资源与代码生成类**

| 维度 | 检测内容 | 优化方向 |
|------|---------|---------|
| 资源管理 | 每次调用都 `malloc`/`fopen`/获取锁？锁范围覆盖了不必要的代码？ | 调用者层缓存/池化资源；缩小临界区 |
| 错误处理 | 错误处理路径和正常路径混在一起？多个调用点共享同一错误处理？ | `__attribute__((cold))` 分离 cold path；合并共享的错误处理 |
| 条件分发 | 通过函数指针/虚函数调用，但实际只有 1-2 种类型？每次调用前 `cpuid`/`if(has_sve)` ISA 检测？ | 去虚拟化（直接调用）；ISA 检测提升到初始化阶段一次性完成 |
| 模板实例化 | 同一模板用 3+ 种相似类型实例化（代码膨胀）？某个类型占 90%+ 调用？ | type erasure 减少膨胀；高频类型显式特化 |

**D. C++ 语言特性类**

| 维度 | 检测内容 | 优化方向 |
|------|---------|---------|
| Lambda / 函数对象 | 通过 `std::function` 传递回调，每次调用触发类型擦除？Lambda 隐式捕获大对象（`[=]`），每次构造闭包开销大？ | `std::function` → 模板参数（编译期绑定，无类型擦除）；`[=]` → 显式按引用捕获或只捕获需要的字段 |
| RAII 隐式开销 | 调用点创建 `std::lock_guard`/`std::scoped_lock`/`std::unique_ptr` 等 RAII 对象，热点路径上反复构造/析构？ | RAII 对象提升到循环外（复用 guard/ptr）；热路径上用轻量替代（如 `atomic` 替代 mutex、裸指针 + 手动管理替代 `unique_ptr`） |

### 3. 评分与排序

对每个候选优化点评分：

```
score = caller_hotness × 0.35 + pattern_confidence × 0.30 + impact_potential × 0.20 + simplicity × 0.15
```

- `caller_hotness`：调用者是否在热点路径上（0-1）。从 profiling 数据获取，无数据时默认 0.5
- `pattern_confidence`：模式匹配的确定程度（0-1）。例如"所有调用点都传同一个常量"比"大部分调用点传"置信度高
- `impact_potential`：消除调用开销 / 减少拷贝 / 减少分配等预估收益（0-1）
- `simplicity`：改动越简单得分越高（0-1）。加 `restrict`/`const` > 改接口签名 > 重构调用链

过滤：score < 0.4 跳过；最多输出 5 个优化点（取 score 最高的 5 个）。

### 4. 与其他视角的交叉引用

标注可能与其他视角重叠的发现。注意：本视角无法读取其他视角的输出，以下标注仅为给 synthesize 的去重提示：
- 发现循环内调用 → 可能和 `perspective-code-struct` 的向量化机会互补（批量接口 + 向量化）
- `data_structure_flattening` → 直接重叠 `perspective-code-struct` 的 `memory-access-optimization`，标注 `overlaps_with: ["perspective-code-struct"]`
- 参数 `restrict` 缺失 → 可能和 `perspective-compiler` 的 autovec 诊断重叠
- `output_parameter` → 可能和 `perspective-algorithm` 的 `memory-patterns.md`（零拷贝/重复分配）重叠
- `parallelize_calls` → 可能和 `perspective-algorithm` 的并行化机会重叠
- `data_structure_flattening` → 标注 `overlaps_with: ["perspective-code-struct"]`
- `template_substitute` / `devirtualization` → 可能和 `perspective-asm` 的间接调用开销重叠

## 输出格式

```json
{
  "perspective": "caller_context",
  "status": "analyzed|empty|degraded",
  "callers": [
    {
      "name": "process_frame",
      "source_file": "src/pipeline.c",
      "line": 142,
      "caller_hotness": 0.85,
      "call_pattern": "loop_internal",
      "notes": "每帧调用一次，帧率 60fps"
    },
    {
      "name": "batch_validate",
      "source_file": "src/validate.c",
      "line": 207,
      "caller_hotness": 0.60,
      "call_pattern": "sequential",
      "notes": "对每个元素循环调用"
    }
  ],
  "optimization_points": [
    {
      "id": "caller_opt1",
      "type": "caller-context",
      "sub_type": "batch_interface|inline_candidate|constant_specialization|return_value_elimination|function_fusion|buffer_reuse|zero_copy|devirtualization|error_path_separation|resource_cache|lock_scope_reduction|isa_check_hoisting|template_specialization|forwarding_layer_elimination|parallelize_calls|thread_local_cache|output_parameter|data_structure_flattening|template_substitute|raii_hoisting",
      "confidence": 0.82,
      "priority": 1,
      "expected_speedup": "1.3-2x",
      "callers": ["batch_validate"],
      "evidence": {
        "caller_site": "src/validate.c:207",
        "dimension": "调用频率",
        "pattern": "循环内逐元素调用，每次调用开销（函数调用 + 参数准备）占循环体 ~15%",
        "static": "循环体包含 3 次参数计算 + 1 次函数调用，调用者已知道元素总数",
        "dynamic": "caller 自身占 12% CPU，其中 ~15% 花在函数调用开销上"
      },
      "suggestion": "提供批量接口 batch_func(const T* items, size_t n)，消除 N-1 次函数调用开销",
      "risk_level": "low|medium|high",
      "overlaps_with": ["perspective-code-struct"]
    }
  ],
  "key_observations": [
    "3 个调用者中 2 个在循环内调用，每次调用开销累积显著",
    "调用者 batch_validate 已有元素计数，天然适合批量接口"
  ]
}
```

### 字段说明

**`sub_type` 值**：

| sub_type | 含义 |
|----------|------|
| `batch_interface` | 循环内高频调用 → 提供批量接口 |
| `inline_candidate` | 函数体极小（< 10 行）且调用频繁 → 建议 inline |
| `constant_specialization` | 某参数在所有调用点都是常量 → 特化版本消除分支 |
| `return_value_elimination` | 返回值未使用 → 改为 void 接口 |
| `function_fusion` | 两个函数总是成对调用 → 合并 |
| `buffer_reuse` | 每次调用分配/释放 → 调用者层复用 buffer |
| `zero_copy` | 多次 memcpy → string_view/span 零拷贝 |
| `devirtualization` | 虚函数/函数指针实际类型确定 → 直接调用 |
| `error_path_separation` | 热/冷路径混排 → `__attribute__((cold))` 分离 |
| `resource_cache` | 重复获取资源 → 缓存/池化 |
| `lock_scope_reduction` | 锁范围过大 → 缩小临界区 |
| `isa_check_hoisting` | 每次调用 ISA 检测 → 提升到初始化 |
| `template_specialization` | 高频类型 → 显式模板特化 |
| `forwarding_layer_elimination` | 纯转发中间层 → 消除，直接调用底层 |
| `parallelize_calls` | 单线程循环无跨迭代依赖 → 调用者端多线程并行 |
| `thread_local_cache` | 多线程同调各自分配 → `thread_local` 缓存 |
| `output_parameter` | 返回值每次 new/malloc → 改为输出参数，调用者管理生命周期 |
| `data_structure_flattening` | 指针链/树 → 数组扁平化，改善 cache 局部性 |
| `template_substitute` | `std::function` 类型擦除 → 模板参数编译期绑定 |
| `raii_hoisting` | RAII 对象反复构造/析构 → 提升到循环外 |

**`risk_level`**：
- `low`：局部改动，不影响接口（inline_candidate、error_path_separation、isa_check_hoisting、template_substitute、raii_hoisting）
- `medium`：接口签名微调（constant_specialization、return_value_elimination、buffer_reuse、template_specialization、output_parameter、thread_local_cache）
- `high`：接口/调用链变更（batch_interface、function_fusion、devirtualization、forwarding_layer_elimination、parallelize_calls、data_structure_flattening）

**`status`**：
- `analyzed`：正常完成，至少一个优化点或确认无机会
- `empty`：无调用者（静态函数未被调用？）或无优化机会
- `degraded`：grep 不可用 / 部分调用点无法读取，结果可能不完整
