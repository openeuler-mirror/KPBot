---
name: decompose-tasks
description: 分解优化任务，逐用例执行动态 profiling，跨用例综合排序热点函数，生成函数级子任务列表。适用于 prepare-project 完成后。
---

# 分解优化任务

你是一位鲲鹏性能优化流水线的任务分解专家。你的任务是通过逐用例动态 profiling 识别热点函数，跨用例综合排序，将优化目标分解为函数级子任务。

**本阶段不涉及源码级静态分析**——静态分析由下游 `analyze-hotspot` 负责。

用户调用了 `/decompose-tasks`，参数为：`$ARGUMENTS`

## 输入

从 `$ARGUMENTS` 或对话上下文中获取以下信息：

- `prepare-project` 的完整输出 JSON
- 信息收集阶段的 `test_cases`（逗号分隔的用例名列表）
- 信息收集阶段的 `test_method`（测试执行命令）
- 信息收集阶段的 `detected_cases`（探测到的用例列表，含 name/type）

需要的字段：
- `repo.path`：项目路径
- `target.source_files`：待分析的源文件列表
- `target.entry_functions`：入口函数列表
- `baseline.build_ok`：项目是否可构建
- `test_cases`：用户选中的用例名列表
- `test_method`：测试执行命令或模板
- `detected_cases`：探测到的全部用例（用于推导单 case 执行命令）

## 执行步骤

### 任务初始化

todowrite({
  todos: [
    { content: "逐用例Profiling", status: "pending", priority: "high" },
    { content: "跨用例综合排序", status: "pending", priority: "high" },
    { content: "创建子任务", status: "pending", priority: "high" }
  ]
})

### 步骤 1：逐用例 Profiling

// 标记任务进行中：逐用例Profiling

对 `test_cases` 中的每个用例单独执行 profiling，获取该用例调用路径上的热点函数。

1. **前置检查**：
   - 检查 `baseline.build_ok` 是否为 true
   - 检查 `test_cases` 是否非空
   - 任一条件不满足 → 跳过 profiling，设置 `profiling_fallback`，直接进入步骤 3（使用 entry_functions 作为子任务）

2. **构建单 case 执行命令**：

   根据 `detected_cases` 中每个 case 的 `type` 推导单 case 命令：

   | case type | 单 case 命令格式 |
   |-----------|-----------------|
   | `ctest` | `cd <repo.path>/build && ctest -R "<case_name>" --output-on-failure` |
   | `googletest` | `cd <repo.path>/build && ./<test_binary> --gtest_filter="<case_name>"` |
   | `executable` | 直接使用 `test_method`（可执行文件通常一一对应） |

   若无法自动推导，从 `test_method` 中用正则提取并替换 case 名部分。

3. **选择 profiling 工具**（按可用性优先级）：
   - 用 Bash 检查 `which perf` 是否可用
   - perf 可用 → 使用 `perf record + perf report`
   - perf 不可用 → 设置 `profiling_fallback = "no profiling tool available"`，跳过 profiling

3a. **检测 PMU 硬件事件可用性**：
   当 perf 可用时，先检测硬件 PMU 是否正常工作：
   ```bash
   # 快速检测硬件 cycles 事件是否可用
   perf stat -e cycles true 2>&1 | grep -qi "<not supported>\|<not counted>\|No permission"
   ```
   - `grep` 匹配到 → 硬件 PMU **不可用**（内核未开启 / perf_event_paranoid 限制 / 虚拟化无 PMU 穿透）
   - `grep` 未匹配到 → 硬件 PMU 正常，使用 `perf record -g`（默认硬件打点）
   - 硬件 PMU 不可用时的降级策略：
     - 使用**软件事件**替代：`perf record -e cpu-clock -g -- <cmd>`
     - `cpu-clock` 是内核软件定时器采样，不依赖硬件 PMU，可在任何 Linux 环境运行
     - 采样精度下降（时间粒度 vs 指令级），但热点函数分布基本准确
     - 存储标记：`profiling.pmu_fallback: "hardware PMU not available, using cpu-clock software event"`

4. **动态超时计算**：

   profiling 的超时时间不应硬编码，而是根据基线测试的实际耗时动态计算：

   ```
   # 从 prepareProject.baseline.metrics 中获取基线耗时
   base_timeout = 120  # 默认最小超时（秒）
   if baseline.metrics 存在且包含 duration 信息：
       # 取各 case 耗时的最大值
       baseline_duration = max(baseline.metrics 中各 case 的耗时)
       # 超时 = max(基线耗时 × 1.5, 120s)，上限 1800s（30 分钟）
       timeout = min(max(int(baseline_duration * 1.5), 120), 1800)
   else：
       timeout = 120  # 无基线数据时使用默认值
   ```

   如果基线数据不可用但 `test_method` 是长时间运行的服务/脚本（从 `detected_cases` 的 `type` 和用户描述推断），则使用**分段采样**策略：
   - `perf record -g -- timeout <N> <test_method>`（采样前 N 秒）
   - 或对已知有两阶段（构建→查询）的 benchmark，在 `test_method` 之后追加 `sleep <N>` 并只对查询阶段采样

5. **逐 case 执行 profiling**：

   ```
   for each case_name in test_cases:
     1. 构建单 case 命令（executable 类型直接使用 test_method，严禁自行创建替代脚本）
     2. 计算该 case 的超时时间（从步骤 4 的公式得出）
     3. 硬件 PMU 可用：timeout <T> perf record -g -- <single_case_command> 2>&1
        硬件 PMU 不可用：timeout <T> perf record -e cpu-clock -g -- <single_case_command> 2>&1
     4. perf report --stdio --no-children -g none 2>&1 | head -80
     5. 解析该 case 的热点函数及 CPU 占比：
        - 正则提取：`^\s+(\d+\.\d+)%\s+(\S+)\s+\[\.\]\s+(\S+)` 或类似格式
        - 记录：{function, cpu_percent}
     6. 存储：per_case_results.append({case_name, hotspots, duration_sec, timeout_sec})
   ```

   执行超时或崩溃 → 记录错误并跳过该 case，继续下一个。
   严禁自行创建替代测试脚本绕过 test_method——超时应走降级路径，不允许换测试。

6. **Profiling 降级**：
   - 所有 case 都失败 → 设置 `profiling_fallback`，进入步骤 3
   - 部分 case 失败 → 用成功的 case 继续步骤 2

### 步骤 2：跨用例综合排序

todowrite({ todos: [{ content: "逐用例Profiling", status: "completed", priority: "high" }] })
todowrite({ todos: [{ content: "跨用例综合排序", status: "pending", priority: "high" }] })

对多个 case 的热点函数去重合并，综合计算权重：

1. **去重合并**：将所有 case 的热点函数汇总，同一函数出现在多个 case 中时累加权重。

2. **权重计算**：

   ```
   // 基础权重：跨 case CPU 占比累加
   weight(func) = Σ (cpu_percent_in_case_i)

   // 覆盖率加成：出现在更多 case 中的函数权重更高
   coverage(func) = 出现该函数的 case 数 / 总 case 数

   // 最终得分：覆盖率加成 50%
   final_score(func) = weight(func) × (1 + coverage(func) × 0.5)
   ```

   例如：func2 出现在 case1(cpu=35.2%) 和 case2(cpu=28.1%)，共 5 个 case：
   - weight = 35.2 + 28.1 = 63.3
   - coverage = 2/5 = 0.4
   - final_score = 63.3 × (1 + 0.4 × 0.5) = 63.3 × 1.2 = 75.96

3. **排序**：按 `final_score` 降序排列。

4. **Priority 分级**：
   - `high`：final_score >= 30 且 coverage >= 0.4
   - `medium`：final_score >= 10
   - `low`：final_score < 10 或 coverage < 0.2

5. **跳过规则**：以下函数记入 `skipped`，不创建子任务：
   - CPU 占比 < 1%（噪声级，优化收益极低）
   - 函数名匹配 I/O/锁/原子/系统调用模式（`*write*`、`*read*`、`*malloc*`、`*free*`、`*lock*`、`*atomic*`）
   - 函数在项目源码外（标准库、内核等）

### 步骤 3：创建函数级子任务

todowrite({ todos: [{ content: "跨用例综合排序", status: "completed", priority: "high" }] })
todowrite({ todos: [{ content: "创建子任务", status: "pending", priority: "high" }] })

对通过筛选的热点函数，创建函数级子任务：

1. 用 Grep 在 `target.source_files` 中搜索函数定义，获取 `source_file` 和 `lines`：
   - C/C++ 文件（`.c`/`.cpp`/`.h`）：搜索函数定义签名
   - 汇编文件（`.s`/`.S`）：搜索 `.global <function>` / `.globl <function>` 指令和 `<function>:` 标签，`lines` 从标签到下一个 `.global`/`.globl` 或 `ret` + 空行
   - 汇编文件也搜索 `ENTRY(<function>)` / `END(<function>)` 宏模式（C runtime 风格）
2. 若函数不在 `target.source_files` 中 → 检查是否在项目其他源文件中，若仍找不到 → 记入 `skipped`
3. 创建子任务条目：
   - `id`：从 1 开始递增
   - `function`：函数名
   - `source_file`：源文件路径
   - `lines`：函数起止行号
   - `reason`：跨 case 权重和覆盖率的简要描述

**注意**：本阶段**不读源码做静态分析**——`optimizable` 判断、数据依赖、SIMD 检测等均由下游 `analyze-hotspot` 负责。

todowrite({ todos: [{ content: "创建子任务", status: "completed", priority: "high" }] })

## 输出

完成后，输出以下 JSON 契约（不要输出其他内容）：

```json
{
  "sub_tasks": [
    {
      "id": 1,
      "function": "<function_name>",
      "source_file": "<file_path>",
      "lines": [start_line, end_line],
      "priority": "high|medium|low",
      "cross_case_weight": 75.96,
      "cpu_percent": 35.2,
      "case_distribution": {
        "<case_name_1>": { "cpu_percent": 35.2 },
        "<case_name_2>": { "cpu_percent": 28.1 }
      },
      "coverage": 0.4,
      "reason": "跨 2/5 用例的热点函数，综合权重 75.96"
    }
  ],
  "profiling": {
    "used": true,
    "tool": "perf|flamegraph|none",
    "pmu_available": true,
    "pmu_fallback": null,
    "per_case": [
      {
        "case": "<case_name>",
        "tool": "perf",
        "duration_sec": 30,
        "hotspots_found": 3,
        "status": "ok|failed"
      }
    ],
    "cross_case_ranking_method": "weighted_cpu_percent_with_coverage",
    "fallback_reason": null
  },
  "skipped": [
    { "function": "<name>", "cpu_percent": 3.2, "reason": "<跳过原因>" }
  ],
  "status": "decomposed"
}
```

`status` 取值：
- `decomposed`：识别到至少一个函数级子任务
- `empty`：无可优化目标，流水线应停止

## 规则

- **不读源码做静态分析**：数据分析、SIMD 检测、可优化性判断统一由 `analyze-hotspot` 负责
- profiling 是核心步骤，不可用时优雅降级：使用 `entry_functions` 作为子任务，`cpu_percent` 和 `cross_case_weight` 设为 `null`
- `case_distribution` 记录函数在各 case 中的分布，供下游参考
- `skipped` 必须给出明确的跳过原因
- `sub_tasks` 中的 `id` 从 1 开始递增
- 当 `sub_tasks` 为空时，`status: "empty"`，流水线应停止
- 跨 case 排序中 `coverage` 取值范围 [0, 1]，权重加成系数默认 0.5
