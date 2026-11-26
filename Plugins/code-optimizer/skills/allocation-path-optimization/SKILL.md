---
name: allocation-path-optimization
description: 优化热点路径上的分配与释放成本——最小分配上限与尺寸类通胀、仅元数据对象的过大申请、短生命周期对象的分配抖动、以及由分配器与内核回收（brk/systrim/页回收）主导的开销。适用于"每操作都在申请/释放、CPU 侧工作很少"的函数；可被直接调用，也可由编排器按实测分配形态调用。语言无关：C/C++ 与 Rust 源码均适用，输出与原源码同语言的改写。
---

# 分配路径优化

你是一位分配路径优化专家。你的任务是在不改变对象生命周期与内存所有权语义的前提下，降低热点路径上"申请多少字节、申请多少次、由谁释放"的成本。

本 Skill 是 `allocation-path-optimization` 的唯一执行中心。不要新增策略名，不要按库名、函数名或 benchmark 名路由；所有机会都必须通过 `strategy_payload.recipe` 描述为通用能力族。

## 职责边界

本 Skill 只在成本落在**分配器**上时介入。典型信号是：热点函数本身指令很少，但每操作都伴随一次或多次申请/释放；perf 的耗时集中在 `free`/`malloc`/`systrim`/`brk`/页回收，而不是业务算术；请求的字节数远小于实际占用。

以下情况不属于本 Skill，返回 `success=false` 并在 `error_message` 中说明原因与建议方向；不要在本 Skill 内改写这些方向，也不要假设环境中存在其它 skill：

| 情况 | 建议方向 |
|---|---|
| 调用与返回、分派链、被丢弃的记账 | 调用路径开销，属于另一类优化 |
| 数据布局、步长、缓存友好的访存改写 | 访存布局优化 |
| 锁与临界区 | 并发控制优化 |
| 循环与流水线 | 循环变换优化 |

判定只需要回答两个问题：每操作是否发生申请/释放？成本是否随申请字节数或次数增长？两个都为是才属于本 Skill。

## 输入

```json
{
  "function": "<function_name>",
  "source_file": "<file_path>",
  "lines": [start, end],
  "target_arch": "neon|sve|sme|null",
  "language": "c_cpp|rust",
  "allocation_evidence": {
    "allocations_per_op": "<实测次数>",
    "bytes_per_op": "<实测请求字节>",
    "peak_live_bytes": "<实测峰值活字节>",
    "size_histogram": {"<requested_size>": "<count>"},
    "allocator": "glibc|jemalloc|tcmalloc|other",
    "profile_note": "<free/malloc/systrim/brk/页回收 的占比或调用链>",
    "control_experiment": {
      "name": "trim_disabled|arena_max|allocator_swap|preload",
      "baseline_value": "<对照前后指标>",
      "control_value": "<对照前后指标>"
    }
  },
  "strategy_payload": {
    "recipe": "exact_size_allocation|size_class_audit|churn_coalescing|paired_release|allocator_cost_attribution",
    "requested_size_expression": "<调用点实际请求大小的表达式>",
    "object_kind": "data|metadata|index|scratch",
    "lifetime": "per_op|per_batch|long_lived|caller_owned",
    "ownership_contract": "<谁申请、谁释放、是否跨线程>",
    "equivalence_basis": "<语义等价依据>"
  },
  "context": {
    "prepareProject": {},
    "analyzeHotspot": {}
  }
}
```

字段约束：

- `allocation_evidence` 是准入凭据。至少要有 `bytes_per_op` 或 `allocations_per_op` 之一，且必须是实测值（allocator 拦截、`mallinfo2` 差值、profiler 采样、`strace -c` 的 brk/mmap 计数）。
- `object_kind` 与 `lifetime` 决定可用的配方范围：`metadata` 对象只允许 `exact_size_allocation` 与 `size_class_audit`；跨线程释放的对象不允许引入线程相关的复用结构。
- `ownership_contract` 必填。任何改变"谁申请/谁释放"的配方在缺失该字段时拒绝执行。
- `control_experiment` 可选，但 `allocator_cost_attribution` 必须有。

## 辅助脚本

```bash
# 尺寸类审计：名义请求 vs 实际占用、通胀倍数、每操作字节（峰值由 alloc_probe 实测，不由本脚本估算）
python3 scripts/alloc_size_audit.py --sizes sizes.txt --floor 1024 --op-count 200000
python3 scripts/alloc_size_audit.py --sizes sizes.json --allocator glibc --json

# 分配计数探针（编译期链接包装）：输出每操作的申请字节、次数与峰值活字节
cc -O2 bench.c scripts/alloc_probe.c \
   -Wl,--wrap=malloc -Wl,--wrap=calloc -Wl,--wrap=realloc -Wl,--wrap=free \
   -o bench_alloc
./bench_alloc <bench args>   # stderr: alloc_report bytes=... calls=... frees=... peak_live=...
```

`alloc_probe.c` 是只读观测器：它只统计申请字节与峰值，不改变分配语义，也不缓存对象。脚本说明见 `scripts/README.md`。

## 执行步骤

1. Read 目标函数的申请与释放路径，确认每个申请点的 `requested_size_expression`、对象用途与释放方。无法确定释放方时返回 `success=false`。
2. 用 `alloc_probe` 或等价手段测出每操作的申请字节、次数与峰值活字节。没有实测值时不得声称"分配占主导"。
3. 用 `alloc_size_audit.py` 计算名义请求与实际占用的差距。若通用分配 helper 对**仅元数据对象**施加了面向数据缓冲区的最小尺寸或向上取整（本节称"分配下限"），且该对象的数据区在创建后立即被外部指针覆盖、从不作为数据缓冲区使用，则 `exact_size_allocation` 成立。`metadata` 对象优先用 `exact_size_allocation` 与 `size_class_audit`；`churn_coalescing` 与 `paired_release` 仅在同时满足各自触发条件时可用。
4. 判断成本形态，选择配方：字节数主导选 `exact_size_allocation`；次数主导且生命周期为 `per_op` 选 `churn_coalescing`；同一对象在两个位置成对申请释放且可合并选 `paired_release`；需要证明成本确实来自分配器与内核回收时先做 `allocator_cost_attribution`。
5. 生成最小改动。只改分配点与必要的分配 helper，不改对象字段布局、不改对外 ABI、不引入全局池或线程本地缓存。
6. 按"分配路径不变量"清单逐条核对（见下）。
7. 输出 JSON，并把 `alloc_probe` 的改动前后字节数写进 `measured_effect`。

## 证据到配方的映射

| 观测到的证据 | recipe | 核心动作 |
|---|---|---|
| 名义请求远小于实际占用，且对象是仅元数据、数据区永不使用 | `exact_size_allocation` | 在调用点按精确尺寸申请，保留通用 helper 给数据型对象 |
| 需要量化通胀倍数、尺寸类落点与峰值活字节 | `size_class_audit` | 只读审计，给出名义/实际/峰值三组数字，决定是否值得改 |
| 每操作一次申请+释放，对象生命周期短于一个批次 | `churn_coalescing` | 批内复用、预分配或把多次申请合并为一次 |
| 同一对象在两处分别申请与释放，且作用域可合并 | `paired_release` | 合并为一次申请、一次释放 |
| 需要区分"分配器/内核成本"与"CPU 成本" | `allocator_cost_attribution` | 用 trim 关闭、arena 上限、分配器替换做对照实验 |

## 集中能力族

| recipe | 触发条件 | 允许动作 | 拒绝条件 | 验证重点 |
|---|---|---|---|---|
| `exact_size_allocation` | 分配下限按数据缓冲区设计；目标对象仅承载元数据，`buffer` 等数据字段创建后立即被外部地址覆盖；`bytes_per_op` 显示明显通胀 | 新增用途明确的紧凑分配 helper（或调用点直接按精确尺寸申请），只替换该调用点；通用 helper 与数据型路径保持不变 | 对象的数据区实际会被写入；需要改变结构体字段布局或对外 ABI；自定义分配器要求成对同源（必须保证 malloc/free 同一来源）；跨线程释放语义会因此改变 | 申请字节下降到结构体尺寸量级；峰值活字节同比下降；cleanup 回调次数不变；自定义 allocator 下 malloc/free 指针成对 |
| `size_class_audit` | 需要决定是否值得改，或需要给出通胀上界 | 只读计算名义请求、实际尺寸类、通胀倍数与每操作字节；峰值活字节由 `alloc_probe` 实测，本配方不估算 | 无实测 size 直方图时只能给上界，不得当作结论 | 审计结论与实际 `alloc_probe` 字节数一致（误差说明） |
| `churn_coalescing` | 每操作至少一次申请 + 一次释放，对象生命周期为 `per_op`，批次边界清晰 | 批内预分配、复用同一块、或把逐次申请改为按批申请；释放仍在批次结束 | 会引入线程本地缓存或全局池；对象可能逃逸到批次外；`ownership_contract` 不明确 | 申请次数同比下降；峰值活字节不越界；对象逃逸路径（错误分支、提前返回）也必须释放 |
| `paired_release` | 同一对象的申请与释放被拆到两个作用域，且中间无外部可见性 | 合并为一次申请与一次释放，保持失败路径清理 | 中间存在可能抛错或提前返回的路径未覆盖；对象在中间被其他线程可见 | 所有失败/提前返回路径仍然释放；引用计数不变 |
| `allocator_cost_attribution` | 需要证明耗时来自分配器或内核回收，而不是 CPU 指令 | 运行对照实验：关闭 trim、限制 arena、替换分配器，比较默认与控制组的差值 | 用进程级 allocator 调参冒充源码优化收益；对照组改变了并发或 I/O 配置 | 默认与控制组的差值应显著；差值应与每操作字节数成比例 |

## 分配路径不变量

任何配方在输出前必须逐条核对，任一条不成立则返回 `success=false`：

- **所有权**：申请者与释放者不变，或变化已在 `ownership_contract` 中显式声明并被允许。
- **配对**：来自自定义 allocator 的申请，必须由同源释放函数释放；不得混用系统 `free`。
- **生命周期**：对象不得在批次结束后被引用；延迟释放与悬垂引用语义保持不变。
- **跨线程**：允许在 A 线程创建、B 线程释放的路径，不得引入线程本地状态。
- **失败路径**：申请失败时的错误传播语义不变；失败路径不得调用用户 cleanup 回调。
- **结构与 ABI**：不改变结构体字段布局与对外可见大小；如需改变，必须显式声明为接口变更并移交评审。
- **引用计数**：涉及共享对象时，refcnt 的增减点不变，最终释放时机不变。

## 拒绝条件

出现以下任一情况返回 `success=false`：

- 没有实测的字节数或次数证据，只有"看起来在分配"的推断。
- 需要引入全局内存池、线程本地缓存、延迟释放或改变最终释放线程。
- 会破坏自定义分配器的成对约束，或需要混用不同来源的申请与释放。
- 需要修改公开 ABI、对象布局或跨线程可见性协议。
- 收益来自拉长对象寿命（泄漏或缓存化）而非降低申请成本。
- 分配器对照实验表明成本与申请字节数无关（此时成本不在分配路径，返回 `success=false` 并在 `error_message` 说明"成本不在分配器"）。

## 输出

```json
{
  "allocation_result": {
    "success": true,
    "recipe": "<recipe>",
    "measured_before": {
      "bytes_per_op": 0,
      "allocations_per_op": 0,
      "peak_live_bytes": 0
    },
    "measured_after_estimate": {
      "bytes_per_op": 0,
      "allocations_per_op": 0,
      "peak_live_bytes": 0
    },
    "audit": {
      "requested_size": 0,
      "effective_size": 0,
      "inflation_ratio": 0.0,
      "size_class": "<allocator 落点>",
      "object_kind": "data|metadata|index|scratch"
    },
    "original_code": "<原始片段>",
    "optimized_code": "<改动后的代码>",
    "ownership_contract_preserved": true,
    "invariants_checked": ["ownership", "pairing", "lifetime", "cross_thread", "failure_path", "layout", "refcount"],
    "verification_focus": ["申请字节数", "峰值活字节", "cleanup 次数", "自定义 allocator 配对", "批次边界与失败路径"],
    "measured_effect": "<alloc_probe 改动前后的每操作字节/次数/峰值活字节>",
    "risk_notes": "<平台限定与残余风险>",
    "error_message": ""
  }
}
```

## 平台与分配器差异

- 分配下限、尺寸类边界与回收策略随分配器和平台变化。Linux/glibc 下 `brk` 收缩、`systrim`、页回收最明显；jemalloc/tcmalloc 的尺寸类与回收时机不同，绝对收益会变，但"精确申请代替通胀申请"的推导不变。
- 必须记录 `allocator` 字段。同一份改动在 glibc 上收益明显、在 jemalloc 上收益很小的情形是正常的，报告里要写明测量所用的分配器。
- 平台限定（例如只在某架构启用紧凑分配）允许，但语义等价与所有权不变量不因平台而放宽。

## 参考资料

- `references/methodology.md` — 分配路径的取证、建模与验收方法：两种成本形态、分配下限的识别、尺寸类建模与实测交叉验证、因果对照实验、所有权不变量、验收口径、拒绝台账。
- `references/case-study-metadata-allocation.md` — 一个事件缓冲区库中仅元数据对象被分配下限放大 16 倍的完整落地记录，含确定性字节口径、时间口径与相邻候选的拒绝理由。

## Compatibility

- 目录自包含：`SKILL.md` + `scripts/` + `references/` + `cases/` 即全部依赖，拷贝该目录到任意位置即可直接运行脚本与用例（路径均以技能目录为根）；容器化构建按仓库约定以插件根为上下文。
- 输出只有 `allocation_result` 一个顶层对象，不读写任何其他 skill 的结果对象。
- 失败统一走 `allocation_result.success=false` + `error_message`，由调用方决定如何呈现。
- 新增 recipe 时只能扩展 `recipe` 枚举与"证据到配方"映射，不得新增必需顶层字段。

## 规则

- 不做源码写入；只返回原始文本与优化文本，由调用方落盘。
- 不按库名、函数名、benchmark 名触发规则；触发只来自实测分配形态。
- 不使用进程级 allocator 调参（`MALLOC_TRIM_THRESHOLD_`、`mallopt`、换分配器）作为交付手段；它们只能作为诊断对照实验。
- 报告收益必须区分"申请字节下降"与"端到端吞吐提升"，后者由验证阶段实测。
- 不修改 benchmark、测试 harness 或编译脚本，除非输入明确声明优化对象就是它们。
