---
name: analyze-caller-context
description: 分析热点函数的调用上下文，从调用者视角发现函数体内部不可见的优化机会。适用于 analyze-hotspot 完成后，作为函数级分析的补充阶段。
---

# 调用上下文分析

你是一位鲲鹏性能优化流水线的分析专家。你的任务是从**调用者视角**分析热点函数的外部关系——谁调用了它、怎么调、传了什么参数、怎么消费返回值，从而发现仅在函数体内部永远看不到的优化机会。

## 输入

从 `$ARGUMENTS` 或对话上下文中获取：

```json
{
  "function": "<function_name>",
  "source_file": "<file_path>",
  "lines": [start, end],
  "decompose_tasks": { "folded": "<folded output>" },
  "project_path": "<project_root>",
  "analyze_hotspot": { "optimization_points": [], "static_analysis": {}, "dynamic_analysis": {} },
  "intent": { "optimization_goal": "throughput|latency|memory|balanced", "risk_tolerance": "safe|moderate|aggressive" },
  "architecture_file": "<prepareProject.architecture_file，ARCHITECTURE.md 绝对路径>",
  "performance_profile": "<testcaseAnalysis.performance_profile，测试用例性能画像>"
}
```

- `function`/`source_file`/`lines`：目标函数信息
- `decompose_tasks`：DecomposeTasks 输出，含 Profiling 调用栈数据
- `project_path`：项目根路径，用于搜索调用点
- `analyze_hotspot`：AnalyzeHotspot 的输出（已有的优化点和静态/动态分析数据）
- `intent`：用户优化意图，用于偏置和过滤
- `architecture_file`：ARCHITECTURE.md 绝对路径，Read 此文件可获取模块依赖关系、头文件引用热度，辅助判断调用影响面
- `performance_profile`：测试用例性能画像，辅助判断调用上下文分析价值

## 执行步骤

### 任务初始化

- gather_task_id = TaskCreate({ subject: "    └ 收集调用上下文", description: "从 perf 调用栈获取调用者信息" })
- analyze_task_id = TaskCreate({ subject: "    └ 多维调用点分析", description: "14维度调用点分析 + 置信度评分 + 生成优化点" })

### 步骤 0：收集调用上下文

TaskUpdate({ taskId: gather_task_id, status: "in_progress" })

**0a. 从 Profiling 数据获取调用栈**：

复用 DecomposeTasks 的 Profiling 结果中的调用栈信息（.folded 或 perf script 输出），提取：
- 热点函数的所有调用者
- 每个调用者的调用频率和 CPU 占比
- 调用链深度

**0b. 代码级搜索调用点**：

```bash
grep -rn "<function_name>(" <project_path>/ --include="*.c" --include="*.cpp" --include="*.h" --include="*.hpp"
```

排除声明和自身的定义，对每个调用点 Read 读取上下文代码（调用点前后 20-30 行），获取：
- 调用者函数名和调用上下文
- 调用是否在循环内、循环边界
- 传递的参数模式
- 返回值的消费方式

**0c. 读取目标函数结构**：

Read 读取 `source_file` 中 `lines[0]-lines[1]` 的函数签名和参数列表，与调用点分析对照。

### 步骤 1：多维调用点分析

TaskUpdate({ taskId: gather_task_id, status: "completed" })
TaskUpdate({ taskId: analyze_task_id, status: "in_progress" })

对 14 个分析维度逐项检测，每个维度按"信号定义 → 检测方法 → 优化建议"执行。

#### 维度 1：调用频率

**信号**：单个 call-site 对热点函数的调用频率。

**检测方法**：
- 从 Profiling 数据提取调用次数和 CPU 占比
- 检查调用点是否在循环内（for/while/do-while）、循环边界是否编译期可知
- 检查调用点是否有条件判断包裹（`if (cond) func()` → 条件不可预测时浪费了分支）

**优化建议模板**：
- 高频 + 小函数体 → `inline_candidate`：建议内联或 `__attribute__((always_inline))`
- 高频 + 循环内 + 循环边界固定 → `batch_interface`：建议提供批量接口，减少调用开销
- 高频 + 条件不可预测 → `hoist_condition`：建议把条件判断提到循环外，减少分支

#### 维度 2：参数模式

**信号**：调用者传递的参数特征。

**检测方法**：
- 检查每个参数在各调用点是否跨多次调用不变（常量传播机会）
- 检查是否大对象（sizeof > 2 × pointer_size）按值传递（拷贝开销）
- 检查是否有重复计算：调用者先计算 A = expr，传递给 callee，callee 内部又重新计算 expr
- 检查是否有互斥参数：调用点传 A=true 和 A=false 走函数内完全不同的路径

**优化建议模板**：
- 参数跨调用不变 → `const_specialization`：建议模板特化或 constexpr if 分支，消除运行时判断
- 大对象按值传递 → `ref_parameter`：建议改为 const 引用或指针传递
- 调用者已知值 callee 重算 → `parameterize`：把 callee 重算的部分参数化，由调用者传入
- 互斥参数 → `function_split`：拆为两个专用函数，消除分支

#### 维度 3：返回值使用模式

**信号**：调用者如何消费 callee 的返回值。

**检测方法**：
- 返回值是否被使用（赋值给变量、参与运算、传给其他函数）
- 返回值是否直接参与条件判断（`if (func() == X)` → 比较值下沉）
- 多个调用点的返回值是否合并处理（`sum += func()` → 向量归约）
- 返回值是否在调用者中被立即丢弃（`(void)func()` 或忽略返回值）

**优化建议模板**：
- 返回值未使用 → `dead_result_elimination`：建议提供 void 版本的轻量接口
- 返回值直接比较 → `compare_inline`：建议 inline，然后由编译器消除中间值
- 多返回值合并 → `reduce_merge`：建议批量接口，一次调用返回 N 个结果

#### 维度 4：调用时序关系

**信号**：同一调用者内多个函数调用的顺序关系。

**检测方法**：
- A() 后总是紧跟 B()（成对调用模式）
- A() 和 B() 共享相同或部分相同的参数
- A() 和 B() 之间无数据依赖（可并行）
- A() 的输出是 B() 的输入（生产者-消费者链）

**优化建议模板**：
- 成对调用 + 无依赖 → `parallelize_pair`：建议并行化或 SIMD 批量处理
- 共享参数 → `shared_computation`：建议提取 A 和 B 的公共计算为一个公共函数
- 生产者-消费者 → `fuse_pipeline`：建议融合为一个 pass，消除中间 buffer
- 无依赖 + 独立数据 → `multi_variant`：建议实现 X_N 变体消费更多数据

#### 维度 5：调用链结构

**信号**：函数在调用栈中的位置和深度。

**检测方法**：
- 从 Profiling 数据获取调用链深度
- 检查中间层是否纯转发（函数体仅做参数转换或类型适配后调用下层）
- 检查是否存在同名函数多层包装（`func()` → `func_impl()` → `func_inner()`）

**优化建议模板**：
- 中间层纯转发 → `inline_wrapper`：内联消除中间层
- 多层包装 → `direct_call`：建议调用者直接调用底层实现

#### 维度 6：跨调用数据流

**信号**：调用者和被调用者之间的数据流向。

**检测方法**：
- 调用者分配的内存、被调用者释放（alloc/free 跨层）
- 调用者已知的信息、被调用者重新计算（如调用者知道数组长度但传了指针，被调用者用 sizeof 或循环计数）
- 调用者加锁、被调用者不做并发保证（抽象层次不一致）

**优化建议模板**：
- alloc/free 跨层 → `buffer_reuse`：在调用者层实现 buffer 复用，减少每调用 alloc
- 已知信息丢失 → `pass_context`：扩充接口传递已知信息，消除 callee 重复计算
- 锁粒度不一致 → `lock_contract`：明确锁的归属层级，上移或下沉

#### 维度 7：错误处理模式

**信号**：调用点如何处理 callee 的错误返回值。

**检测方法**：
- 是否每次调用后立即检查返回值（`if (ret < 0) goto err` / `if (ret != OK) return ret`）
- 错误路径是否与正常路径有相同的计算开销（hot/cold 未分离）
- 是否有多个连续调用共享同一错误处理标签（`goto cleanup` 模式）

**优化建议模板**：
- 每调用必检查 → `fast_path_inline`：inline 函数后利用编译器分支预测优化
- hot/cold 混排 → `hot_cold_split`：建议 `__attribute__((cold))` 或 `.text.unlikely` 分离错误路径
- 共享错误处理 → `error_merge`：合并为集中式错误处理宏或函数

#### 维度 8：资源管理模式

**信号**：调用点围绕函数调用的资源获取/释放模式。

**检测方法**：
- 调用前是否有 alloc/lock/open 配对、调用后是否有 free/unlock/close
- 是否在每次调用时重复获取/释放（而非跨调用复用）
- 是否有 RAII 或 scope guard 模式中的函数调用

**优化建议模板**：
- 每调用重复获取资源 → `resource_reuse`：在调用者层缓存/池化资源
- 每次调用后释放 → `deferred_cleanup`：批量延迟释放
- 锁范围过大 → `lock_contraction`：缩小临界区，在 callee 内部只保护必要的操作

#### 维度 9：调用者并行度

**信号**：调用者如何调度并发访问同一个 callee。

**检测方法**：
- 调用者是否在多个线程中调用同一函数
- 调用者是否用 OpenMP/pthread 并行化（检测 `#pragma omp parallel` 或 `pthread_create`）
- 调用者是否单线程但用大循环调用 → 可并行化的信号
- 每个线程的工作粒度是否均匀

**优化建议模板**：
- 单线程循环调用 → `parallel_discovery`：建议调用者改为并发调用（如 `#pragma omp parallel for`）
- 多线程调同一函数 → `thread_local_cache`：建议 callee 使用 thread-local 缓存减少竞争
- 粒度不均 → `task_grained`：建议调整任务划分粒度

#### 维度 10：条件分发模式

**信号**：调用者如何根据条件选择调用哪个函数。

**检测方法**：
- `switch(isa)` / `if(cpu_supports_feature)` 分发不同变体
- 函数表/函数指针调用 vs 直接调用
- 条件分发的结果在多次调用中是否不变（只在初始化时检查一次）

**优化建议模板**：
- 函数指针调用 → `devirtualize`：如果可以推断类型，直接调用
- ISA dispatch + 每调用检查 → `init_once`：把 ISA 检测提到初始化，每调用只查函数指针
- 分支链分发 → `dispatch_table`：建议改为跳转表或 indirect branch

#### 维度 11：模板/泛型实例化

**信号**：同一函数模板在多个调用点有不同的实例化。

**检测方法**：
- 搜索函数名在项目中是否作为模板/泛型出现
- 不同调用点的类型参数和调用模式是否高度相似
- 是否存在实例化膨胀（同一函数体因不同类型参数重复生成代码）

**优化建议模板**：
- 类型相似的多个实例化 → `type_erasure`：建议用 void* 或 type-erased 接口减少实例化
- 模板中部分代码不依赖类型参数 → `hoist_type_independent`：提取类型无关部分为非模板基类
- 特定类型高频调用 → `explicit_specialization`：为高频类型提供特化版本

#### 维度 12：数据结构

**信号**：调用者和被调用者之间传递的数据结构特征。

**检测方法**：
- 从函数签名提取参数类型（struct/class/数组/容器/指针）
- grep 查找 struct/class 定义，Read 获取字段布局和对齐
- 如果有 Profiling 的 cache miss 数据，关联到访存热点指令所在的数据结构字段
- 检查是否有反复使用的指针链（`ptr->next->data`、多次间接寻址）

**优化建议模板**：
- 指针链/大量间接寻址 + cache miss 高 → `pointer_chasing`：建议扁平化存储（链表→数组、SoA、column store）
- 结构体包含未用字段 → `struct_slimming`：建议删除或冷热分离
- 容器类型不匹配 → `container_swap`：list→vector（少量插入遍历多）、unordered_map→flat_map（key 小且多）
- 数据结构 align/padding 开销大 → `layout_compact`：建议重排字段或使用 packed 属性

#### 维度 13：数据生命周期

**信号**：数据在调用者和被调用者之间的所有权和生命周期。

**检测方法**：
- 检查参数是 const ref（共享）/ 值（拷贝）/ 指针（转移所有权）/ 右值（移动）
- 检查返回值是新分配还是填充已有 buffer
- 跨多次调用数据是否存在同一块 buffer 中（可复用）

**优化建议模板**：
- 返回值每次都 new/malloc → `output_param`：建议改为输出参数，调用者预分配
- 多次调用共享同一 buffer → `buffer_reuse`：在调用者层复用 buffer
- 值拷贝频繁 → `move_semantic`：建议使用移动语义或 swap 避免拷贝

#### 维度 14：数据拷贝/序列化

**信号**：调用链中数据被多次拷贝或序列化/反序列化。

**检测方法**：
- 调用者构造参数时是否有 memcpy/strcpy 或显式 copy 构造
- 返回值回到调用者后是否又立即被拷贝到其他地方
- 是否有编码/解码（序列化/反序列化）对出现

**优化建议模板**：
- 多次拷贝 → `eliminate_copy`：建议用零拷贝（slice/view/span/string_view）
- 每调用编解码 → `lazy_decode`：延迟解压，缓存已解码形式

### 步骤 2：置信度评分与过滤

14 个维度不是每个都值得深入。先做快速预检，过滤掉不满足前置条件的维度，再对剩余维度做置信度评分，最后按分数排序输出。

#### 2a. 快速预检（Pre-check）

每个维度满足前置条件才算"候选维度"，否则直接入 `skipped_dimensions`：

| 维度 | 前置条件 | 不满足则跳过原因 |
|------|---------|----------------|
| 调用频率 | 至少一个调用点的 CPU 占比 ≥ 1% 或调用次数 ≥ benchmark 总采样的 1% | "无高频调用点" |
| 参数模式 | 调用者传递 ≥ 2 个参数 且 至少一个参数 sizeof > 8 | "参数简单，无优化空间" |
| 返回值使用 | callee 有非 void 返回值 且 调用者至少有一个调用点消费了返回值 | "返回值未使用或无返回值" |
| 调用时序 | 至少一个调用者内 ≥ 2 次调用同一 callee 或成对调用 | "无双调用时序" |
| 调用链结构 | 调用链深度 ≥ 2 | "调用深度 1，无转发层" |
| 跨调用数据流 | 调用者有 alloc/lock 代码且 callee 有 free/unlock 代码 | "数据所有权未跨层" |
| 错误处理 | 调用点有 `if (ret < 0)` 或 `if (ret != OK)` 模式 | "无错误检查模式" |
| 资源管理 | 调用点周围有 alloc/free 或 lock/unlock 配对 | "无资源管理代码" |
| 调用者并行度 | 调用者含 `pthread`/`std::thread`/`OpenMP` 或多线程框架 | "单线程程序" |
| 条件分发 | callee 通过函数指针调用或 if/switch 分发 | "直接调用" |
| 模板实例化 | 同一函数名有 ≥ 2 个模板实例化或不同调用点使用了不同重载 | "无模板使用" |
| 数据结构 | 调用者传递了 struct/class 类型参数（非基本类型和指针） | "未传递结构体" |
| 数据生命周期 | callee 在内部分配并返回指针，或接受指针参数并转移所有权 | "数据所有权明确，无优化空间" |
| 数据拷贝 | 调用者构造参数时有 memcpy/copy 构造，或 callee 返回值后立即拷贝 | "无冗余拷贝" |

未通过预检的维度不入候选集，不参与后续评分，直接记录到 `skipped_dimensions`。

#### 2b. 候选维度评分

通过预检的维度按以下公式计算 `dimension_score`：

```
dimension_score = (caller_frequency × 0.35) + (static_confidence × 0.30) + (dynamic_evidence × 0.20) + (impact_estimate × 0.15)
```

各因子取值：

- **caller_frequency**（调用频率权重）：调用点 CPU 占比 ≥ 10% → 1.0；5-10% → 0.7；1-5% → 0.4；< 1% → 0.1
- **static_confidence**（代码证据强度）：明显可优化（如 8 字节值传值 → 改引用）→ 1.0；可能有优化（如函数指针可能可去虚拟化）→ 0.5；仅启发式推断（如"循环内调用可能值得内联"）→ 0.2
- **dynamic_evidence**（profiling 数据交叉验证）：Profiling 数据支持该维度的优化信号（如 cache miss 高 + 指针追逐数据结构）→ 1.0；Profiling 数据无直接支持 → 0.3；无 profiling 数据 → 0.0
- **impact_estimate**（预估影响力）：预期 speedup > 2x → 1.0；1.5-2x → 0.7；1.1-1.5x → 0.4；< 1.1x → 0.1

#### 2c. 过滤与排序

1. **过滤**：`dimension_score < 0.4` 的候选不入 `optimization_points`，入 `skipped_dimensions`
2. **排序**：通过过滤的候选按 `dimension_score` 降序排列
3. **上限**：最多输出 5 个 `optimization_points`，超出部分标记 `confidence: "filtered_by_limit"` 入 `skipped_dimensions`

每条 `optimization_point` 的 `confidence` 直接取 `dimension_score`。

### 步骤 3：生成调用上下文优化点

从通过过滤的候选维度生成 `optimization_point`。每个点的格式与 AnalyzeHotspot 输出兼容，且必须映射到可被下游路由的通用策略，不输出裸 `caller-context` 类型。

调用点结果映射规则：
- `const_specialization`、`function_split`、`hoist_condition`、`init_once` → `type: "special-case-optimization"`
- `fuse_pipeline`、`shared_computation`、`reduce_merge`、`eliminate_copy` → `type: "operation-fusion"`
- `batch_interface` → 若建议是批量接口合并多次调用，映射到 `operation-fusion`；若主要收益来自批量 SIMD 化，映射到 `vectorization`
- 其余调用者建议若无法映射到现有 strategy，则放入 `skipped_dimensions`，原因写明 `"no_downstream_route"`

```json
{
  "id": "func_caller_opt1",
  "type": "special-case-optimization|operation-fusion|vectorization",
  "sub_type": "const_specialization|function_split|fuse_pipeline|shared_computation|batch_interface|...",
  "confidence": 0.8,
  "priority": 1,
  "caller": "<caller_function_name>",
  "intermediate_lifetime": "local_only|escaped|unknown",
  "strategy_payload": { "source": "caller-context", "dimension": "<分析维度>" },
  "evidence": {
    "dimension": "<分析维度>",
    "caller_site": "<source_file:line>",
    "pattern": "<检测到的具体模式>",
    "static": "<静态证据描述>",
    "dynamic": "<动态数据支持（如有）>"
  },
  "suggestion": "<优化建议描述>",
  "risk_level": "low|medium|high",
  "expected_speedup": "<预估提升幅度>"
}
```

**优先级**：根据 `dimension_score` 直接映射：≥ 0.8 → priority=1；0.5-0.8 → priority=2；< 0.5 → priority=3。

TaskUpdate({ taskId: analyze_task_id, status: "completed" })

## 输出

```json
{
  "caller_analysis_result": {
    "success": true,
    "function": "<function_name>",
    "callers": [
      { "name": "<caller_name>", "source_file": "<file>", "line": 42, "frequency": "high|medium|low" }
    ],
    "optimization_points": [
      {
        "id": "func_caller_opt1",
        "type": "operation-fusion",
        "sub_type": "batch_interface",
        "dimension_score": 0.82,
        "confidence": 0.82,
        "priority": 1,
        "caller": "<caller_name>",
        "intermediate_lifetime": "local_only",
        "strategy_payload": { "source": "caller-context", "dimension": "调用频率" },
        "evidence": {
          "dimension": "调用频率",
          "caller_site": "main.c:42",
          "pattern": "循环内调用，循环边界 1024 次/每迭代",
          "static": "调用者在 for(i=0;i<1024;i+=2) 中每 2 个元素调用一次",
          "dynamic": "Profiling 显示该 call-site 占比 28% CPU"
        },
        "suggestion": "提供批量接口处理 N 个元素，消除调用开销 + 做 SIMD 向量化",
        "risk_level": "medium",
        "expected_speedup": "2-4x"
      }
    ],
    "skipped_dimensions": [
      { "dimension": "调用链结构", "reason": "调用深度 1，无转发层" },
      { "dimension": "模板实例化", "reason": "预检未通过：无模板使用" },
      { "dimension": "错误处理", "reason": "dimension_score=0.35，低于阈值 0.4" }
    ],
    "error_message": ""
  }
}
```

## 规则

- **本阶段仅做分析**，不做代码变更。源码修改由下游 apply-optimization 统一执行
- **每个优化点需同时有 caller 端和 callee 端的证据**，仅凭单端推断降低 confidence
- **大改动标记 high risk**：接口变更（修改函数签名/返回值类型）→ risk_level=high，仅建议不自动执行
- **不输出裸 caller-context**：本阶段发现的点必须映射到 `special-case-optimization`、`operation-fusion`、`vectorization` 或现有可路由 strategy；无法映射则跳过并说明原因
- **多个调用者的优化点分别生成**：不同调用者的同一维度问题生成独立 optimization_point
- **14 维度先预检再评分**：预检未通过的维度不参加评分，直接入 skipped_dimensions。评分 < 0.4 的也不生成优化点。最多输出 5 个优化点，避免下游负担
