---
name: analyze-testcase
description: 测试用例分析——从测试代码中提取性能画像（规模/并发/访存模式/缓存场景），检测预热偏差和用例冲突，指导后续优化策略选择。在 DecomposeTasks 之后、AnalyzeHotspot 之前执行。
---

# 测试用例分析

你是一位性能测试分析专家。你的任务是从测试用例代码中提取**性能画像（performance_profile）**，检测可能误导优化方向的隐患，将分析结果向下游所有优化层透传。

## 核心价值

测试用例定义了优化场景——规模决定策略、并发度决定瓶颈类型、预热决定热点真实性。如果跳过测试分析直接优化，可能：
- 预热代码被误判为热点 → 优化了不该优化的代码
- 小规模测试导向 compute-bound 优化 → 生产环境大规模下 memory-bound 才是瓶颈
- 优化变更了数据布局 → 测试用例还在测旧路径 → 验证失效

## 输入

```json
{
  "decompose_tasks": "<context.decomposeTasks>",
  "prepare_project": "<context.prepareProject>",
  "parse_intent": "<context.parseIntent>",
  "project_path": "<context.project_path>",
  "test_cases": "<context.test_cases>",
  "test_method": "<context.test_method>",
  "detected_cases": "<context.detected_cases>",
  "architecture_file": "<context.prepareProject.architecture_file>"
}
```

需要的字段：
- `decompose_tasks.sub_tasks[]`：热点函数列表及 per-case profiling 数据
- `decompose_tasks.profiling.per_case[]`：各 case 的 profiling 状态和耗时
- `prepare_project.repo.path`：项目路径
- `prepare_project.repo.compilation`：编译参数
- `prepare_project.machine.cache_info`：机器缓存信息
- `test_cases`：用户选中的用例名列表（逗号分隔，来自 GatherContext）
- `test_method`：测试执行命令（来自 GatherContext）
- `detected_cases[]`：探测到的全部用例（含 name/type，来自 GatherContext）
- `parse_intent`：用户优化意图（优化目标、风险容忍度）
- `project_path`：项目根路径
- `architecture_file`：ARCHITECTURE.md 绝对路径，Read 此文件可获取数据结构布局、现有优化位置等客观事实

## 执行步骤

### 任务初始化

todowrite({
  todos: [
    { content: "多维分析", status: "pending", priority: "high" },
    { content: "生成性能画像", status: "pending", priority: "high" }
  ]
})

### 步骤 0：收集测试用例源码

// 标记任务进行中：多维分析

对 `decompose_tasks.profiling.per_case[]` 中的每个 case：

1. 从 `detected_cases` 中查找 case 的 `type`（ctest/googletest/executable）
2. 定位测试源码文件：
   - ctest：`grep -rl "TEST.*${case_name}" <project_path>` 或搜索 CMakeLists.txt 中的 `add_test`
   - googletest：搜索 `TEST` / `TEST_F` / `TEST_P` 宏引用
   - executable：从 `test_method` 推导可执行文件，再搜索 CMakeLists.txt 中的 `add_executable` 找到源文件
3. 读取测试源码（Read 工具），记录文件路径和行号范围
4. 若找不到源码 → 标记 `source_unavailable: true`，跳过需要源码的维度分析

### 步骤 1：六维度分析

对每个测试 case 执行以下六维度分析。每个维度输出 `risk: high|medium|low` 和具体证据。

#### 维度 1：预热/初始化干扰分析

**目的**：识别测试 setup 阶段是否引入了假热点。

**检测方法**：
1. 扫描测试源码，定位 setup/fixture 代码段：
   - googletest：`SetUp()` / `SetUpTestCase()` / fixture 构造函数
   - ctest：`main()` 中第一个计时区间之前的代码
   - 通用：数据填充循环（`for.*memset` / `for.*=.*value` 模式）、大段 `malloc` + 初始化、文件 I/O 读取
2. 提取 setup 代码行范围，计算 `setup_line_ratio = setup行数 / 测试总行数`
3. 交叉比对：从 `decompose_tasks.profiling.per_case[case].hotspots` 中各函数的调用栈（如有），检查热点样本是否落在 setup 代码段
4. 若 `setup_line_ratio > 0.3` 且有热点函数被 setup 调用 → 标记 `warmup_bias_risk: high`
5. 检查是否已有 `--delay` / warmup 迭代计数等跳过机制

**输出**：
```
warmup_bias: {
  risk: high|medium|low,
  setup_line_ratio: 0.35,
  affected_functions: ["func_a"],  // 可能被 setup 干扰的热点函数
  has_skip_mechanism: false
}
```

#### 维度 2：测试规模敏感性分析

**目的**：提取测试的数据规模，判断瓶颈类型（compute-bound vs memory-bound）。

**检测方法**：
1. 从测试源码中提取规模变量：
   - 数组维度：`#define N (1024)` / `const.*int.*SIZE.*=.*2048` / `malloc\((\d+)\s*\*`
   - 循环边界：`for.*< (\w+)`，回溯变量定义提取值
   - 矩阵 shape：`(M|N|K|rows|cols)\s*=\s*(\d+)` 模式
2. 估算数据量：`data_size_bytes = 维度乘积 × sizeof(element)`（从变量类型推导 element size）
3. 分级（对照 `prepare_project.machine.cache_info`，若不可用则使用典型值 L1=64KB, L2=512KB, L3=32MB）：
   - `small`：≤ L1 容量
   - `medium`：≤ L2 容量
   - `large`：≤ L3 容量
   - `xlarge`：> L3 容量
4. 检测危险值：
   - 2 的幂维度（如 256、512、1024）→ cache thrashing 风险
   - 质数维度（如 257、509）→ tile 边界处理可能漏
   - 非对称 shape（如 16×1000000）→ 列访问 stride 过大

**输出**：
```
scale_profile: {
  level: small|medium|large|xlarge,
  data_size_bytes: 1048576,
  dimensions: { "M": 512, "N": 512, "K": 512 },
  element_type: "float",
  fits_in: "L2",
  dangerous_values: ["M=512 是 2 的幂，与 N=512 同幂可能导致 cache thrashing"],
  bottleneck_hint: "compute_bound"  // small/medium → compute, large/xlarge → memory
}
```

#### 维度 3：优化-测试共演检查

**目的**：预判当前热点函数的优化是否会破坏测试用例，提前标记需要同步修改的测试点。

**检测方法**：
1. 对 `decompose_tasks.sub_tasks[]` 中的每个热点函数，提取其函数签名（grep 函数定义行）
2. 在测试源码中搜索对该函数的调用（`grep -n "函数名" <test_source>`）
3. 记录每次调用的行号和上下文（前后 3 行）
4. 分析变更影响面：
   - 函数签名变更风险：参数数量/类型是否可能因优化改变（如向量化可能要求对齐指针、unroll 可能添加 remainder 参数）
   - 数据布局变更风险：热点函数是否操作全局数组（AoS→SoA 会影响测试的初始化代码和断言）
   - 硬编码值风险：测试中是否有针对当前实现的硬编码期望值（如 `EXPECT_EQ(result[0], 42)`）
5. 标记需要关注的调用点

**输出**：
```
co_evolution: {
  call_sites: [{ "caller": "test_matmul_basic", "file": "...", "line": 42, "context": "..." }],
  signature_change_risk: low|medium|high,
  layout_change_risk: low|medium|high,
  hardcoded_assertion_risk: low|medium|high,
  alerts: ["测试 test_matmul_basic:42 使用硬编码期望值，向量化后精度可能变化"]
}
```

#### 维度 4：多用例冲突预检

**目的**：当有多个测试用例时，提前发现优化可能提升 A 但劣化 B 的风险。

**检测方法**：
1. 对比各 case 的 `scale_profile`，找出规模差异：
   - 若 case A 的 `scale_profile.level` 是 small（compute-bound）而 case B 是 xlarge（memory-bound）→ 标记冲突
2. 对每个热点函数，检查其 `case_distribution`：
   - 若函数仅在部分 case 中是热点 → 优化可能对其他 case 无益甚至有害
3. 计算冲突分数：`conflict_score = |level_diff| × call_frequency_overlap`
   - `level_diff`：small=1, medium=2, large=3, xlarge=4 → 差值
   - `call_frequency_overlap`：函数在所有 case 中的 CPU% 几何平均值

**输出**：
```
conflict_warnings: [{
  function: "matmul",
  cases: ["benchmark_small", "benchmark_large"],
  scale_divergence: "small vs xlarge",
  conflict_score: 0.75,
  risk: "tiling 优化对 small case 可能无收益甚至有开销",
  suggestion: "优先测试 large case，small case 作为回归检查"
}]
```

#### 维度 5：测试断言充分性评估

**目的**：评估测试的 correctness assertion 是否足以捕获 SIMD 边界 bug。

**检测方法**：
1. 统计测试源码中的断言语句：
   - googletest：`EXPECT_*` / `ASSERT_*`
   - ctest/custom：`assert(` / `if.*exit` / 返回值检查
2. 分类断言：
   - 值正确性：`EXPECT_EQ` / `EXPECT_FLOAT_EQ` / `EXPECT_NEAR` / `assert(a == b)`
   - 性能断言：`EXPECT_LT(duration, ...)` / 时间阈值检查
   - 边界检查：对最后一个元素、边界索引的断言
3. 计算密度：`assertion_density = 值断言数 / 被测函数数`
4. 检查覆盖：
   - 是否有边界元素断言（`result[0]`, `result[N-1]`）？
   - 是否有中间元素断言（非边界位置的抽样检查）？
   - 浮点测试是否使用了容差比较（`EXPECT_NEAR`）而非精确相等（`EXPECT_EQ`）？

**输出**：
```
assertion_adequacy: {
  level: adequate|borderline|insufficient,
  density: 1.2,
  correctness_assertions: 5,
  performance_assertions: 1,
  boundary_coverage: true,
  float_tolerance_used: false,
  suggestions: ["浮点结果使用 EXPECT_EQ 而非 EXPECT_NEAR，SIMD 重排后可能因结合律差异导致误报"]
}
```

#### 维度 6：冷/热缓存场景识别

**目的**：判断测试是在测首次访问延迟（cold cache）还是稳态吞吐（warm cache），指导 prefetch 和 tiling 策略。

**检测方法**：
1. 扫描测试源码中的迭代结构：
   - `for (int iter = 0; iter < N; iter++) { ... }` 模式 → 可能是 warm cache（多次迭代复用数据）
   - 内存分配 `malloc` 在循环内还是外？
     - 循环内：每次迭代重新分配 → cold cache
     - 循环外：所有迭代复用 → warm cache（第一轮 cold，后续 warm）
2. 从 `decompose_tasks.profiling.per_case` 中提取 perf stat 数据（如有）：
   - L1 dcache miss rate > 5% → 偏向 cold cache
   - L1 dcache miss rate < 2% → 偏向 warm cache
3. 从 `parse_intent` 中推断测试意图：
   - "测试延迟" / "benchmark_latency" → cold
   - "测试吞吐" / "benchmark_throughput" → warm

**输出**：
```
cache_scenario: {
  type: cold|warm|mixed,
  allocation_location: "loop_outside",
  iteration_structure: "single_pass",
  l1_miss_rate: null,  // 有 perf stat 数据时填入
  intent_hint: "throughput"
}
```

### 步骤 2：生成性能画像（performance_profile）

todowrite({ todos: [{ content: "多维分析", status: "completed", priority: "high" }] })
todowrite({ todos: [{ content: "生成性能画像", status: "pending", priority: "high" }] })

汇总六维度分析，生成下游优化层可直接消费的性能画像：

```json
{
  "performance_profile": {
    "scale": {
      "level": "medium",
      "data_size_bytes": 1048576,
      "dimensions": { "M": 512, "N": 512, "K": 512 },
      "element_type": "float",
      "fits_in": "L2"
    },
    "concurrency": {
      "threads": 1,
      "model": "single_thread",
      "sync_overhead_risk": "none"
    },
    "bottleneck_type": "compute_bound",
    "cache_scenario": "warm",
    "access_pattern": "streaming",
    "precision": "fp32",
    "test_intent": "max_throughput",
    "warmup_bias": {
      "risk": "low",
      "affected_functions": []
    },
    "co_evolution_alerts": [],
    "conflict_warnings": [],
    "assertion_adequacy": "adequate"
  }
}
```

**画像字段消费指南**（下游优化层参考）：

| 优化层 | 关键字段 | 决策影响 |
|--------|---------|---------|
| vectorization | `scale.fits_in`, `bottleneck_type`, `precision`, `dimensions` | small→不用宽向量；memory_bound→不过度向量化；shape 决定行列方向 |
| loop-unrolling | `scale.level`, `bottleneck_type`, `cache_scenario` | compute_bound→激进展开；memory_bound→保守；small→控制 I-cache |
| prefetch-optimization | `cache_scenario`, `access_pattern`, `scale.level` | cold→激进预取；small→可能不需要预取 |
| memory-access-opt | `access_pattern`, `scale`, `bottleneck_type` | strided→AoS→SoA；memory_bound→优先此优化；fits_in 指导 tile |
| compiler-flag-tuning | `precision`, `bottleneck_type` | fp32→-ffast-math 安全；compute_bound→-mcpu=native 收益大 |
| asm-optimization | `access_pattern`→post-index 选择；`precision`→指令选择 |
| branch-elimination | 较少依赖 | |

### 步骤 3：置信度评分与过滤

对每个维度的发现进行置信度评分：

```
confidence = evidence_strength × 0.4 + detectability × 0.35 + impact × 0.25
```

| 因子 | 说明 | 打分规则 |
|------|------|---------|
| evidence_strength | 证据确定性 | 代码直接证据 1.0 / perf 数据 0.8 / 启发式推断 0.5 / 猜测 0.3 |
| detectability | 检测方法可靠性 | 静态扫描直接命中 1.0 / 正则模式匹配 0.7 / 间接推断 0.4 |
| impact | 判断正确时对优化方向的影响 | 影响策略选择 1.0 / 影响参数调整 0.7 / 仅提示参考 0.4 |

过滤：confidence < 0.4 的发现降级为 `advisory`（仅记录，不作为强建议）。

### 步骤 4：生成优化指导建议

基于分析结果，为下游阶段生成具体的优化指导：

```json
{
  "optimization_guidance": [
    {
      "target": "all",
      "guidance": "优先选择 memory-access-optimization（tiling），compute_bound 特征明确，向量化收益有限",
      "reason": "bottleneck_type: compute_bound, scale.level: small, fits_in: L1",
      "confidence": 0.85
    }
  ]
}
```

todowrite({ todos: [{ content: "生成性能画像", status: "completed", priority: "high" }] })

## 输出

完成后输出以下 JSON 契约：

```json
{
  "testcase_analysis": {
    "success": true,
    "cases_analyzed": ["benchmark_matmul"],
    "cases_skipped": [],
    "performance_profile": { /* 见步骤 2 */ },
    "case_analyses": [{
      "case_name": "benchmark_matmul",
      "source_file": "/path/to/test.cpp",
      "source_available": true,
      "warmup_bias": { "risk": "low", "setup_line_ratio": 0.05, "affected_functions": [], "has_skip_mechanism": false },
      "scale_profile": { "level": "medium", "data_size_bytes": 1048576, "fits_in": "L2", "bottleneck_hint": "compute_bound" },
      "co_evolution": { "call_sites": [], "signature_change_risk": "low", "layout_change_risk": "low", "hardcoded_assertion_risk": "low", "alerts": [] },
      "cache_scenario": { "type": "warm", "allocation_location": "loop_outside", "iteration_structure": "multi_pass" },
      "assertion_adequacy": { "level": "adequate", "density": 1.2, "suggestions": [] }
    }],
    "conflict_warnings": [],
    "assertion_adequacy": { "level": "adequate|borderline|insufficient", "details": "..." },
    "optimization_guidance": [],
    "findings": [{
      "id": "tc_finding_1",
      "dimension": "scale_sensitivity",
      "confidence": 0.85,
      "severity": "high|medium|low",
      "description": "...",
      "implication": "对该函数优化策略的影响描述",
      "suggestion": "具体建议"
    }],
    "skipped_dimensions": [{ "dimension": "multi_case_conflict", "reason": "仅单个测试用例，无冲突检测场景" }],
    "error_message": ""
  }
}
```

## 规则

- 找不到测试源码时标记 `source_unavailable: true`，该 case 的源码相关维度跳过，但 profiling 数据相关维度（cache scenario L1 miss rate、case duration 等）仍可分析
- `performance_profile` 是下游透传的核心字段，必须生成，缺失值用 `null`
- `findings[]` 按 confidence 降序排列
- 每个 finding 必须有 `implication`（对优化方向的影响），不能只描述不指导
- 警告/提示用语使用中文，JSON 字段名使用英文 snake_case
- 本阶段**不修改任何代码**，仅分析
