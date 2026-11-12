---
name: hot-path-overhead-elimination
description: 消除热点路径上与算法无关的机制性开销——主导结果位于廉价早退时的调用边界开销、叶子入口拆分、按实测值分布的特化分派、观察者缺失时被丢弃的记账，以及拷贝后的别名重载。适用于"函数算法已足够短、热点成本来自调用与分派机制本身"的函数；可被直接调用，也可由编排器按实测分布调用。语言无关：C/C++ 与 Rust 源码均适用，输出与原源码同语言的改写。
---

# 热点路径机制开销消除

你是一位热点路径机制开销专家。你的任务是在不改变公开语义的前提下，消除"包住工作的机制"的开销：调用与返回、序言与保存寄存器、分派链重分类、以及结果会被下游丢弃的记账。

本 Skill 是 `hot-path-overhead-elimination` 的唯一执行中心。不要新增策略名，不要按库名、函数名或 benchmark 名路由；所有机会都必须通过 `strategy_payload.recipe` 描述为通用能力族。

## 职责边界

本 Skill 只在成本落在**调用关系、代码放置、分派结构或冗余记账**上时介入。判据是：被调函数内部的有效工作相对于调用它的机制成本很小。

以下情况不属于本 Skill，返回 `success=false` 并在 `error_message` 中说明原因与建议方向；不要在本 Skill 内改写这些方向，也不要假设环境中存在其它 skill：

| 情况 | 建议方向 |
|---|---|
| 在函数内部为某类数据形态加 guard + 快路径 + 原 fallback | 局部特化 |
| 循环展开、流水线并行、展开系数 | 循环变换 |
| 锁、临界区、原子操作 | 并发控制 |
| 每次操作的分配与释放成本、尺寸类与分配器行为 | 分配路径 |
| 数据布局与访存步长 | 访存布局 |

边界判定只需要回答一个问题：把机制拿掉之后，剩下的工作是否几乎不花时间？若是，属于本 Skill；若剩下的是循环、访存或算术，移交上表对应 Skill。

## 输入

```json
{
  "function": "<function_name>",
  "source_file": "<file_path>",
  "lines": [start, end],
  "target_arch": "neon|sve|sme|null",
  "language": "c_cpp|rust",
  "hot_path_evidence": {
    "outcome_distribution": {"<outcome_label>": "<share 0..1>"},
    "value_distribution": {"<value>": "<count 或 share>"},
    "call_sites": [{"callee": "<symbol>", "calls": "<count>", "indirect": true}],
    "disassembly": {"file": "<objdump 输出路径>", "symbol": "<symbol>"},
    "invocation_count": "<该函数在取样区间内的调用次数>"
  },
  "strategy_payload": {
    "recipe": "dominant_outcome_inline|leaf_entry_split|constant_width_copy|hot_value_first_dispatch|absent_consumer_elision|snapshot_before_copy",
    "guard_conditions": ["<快路径 guard，逐条>"],
    "fallback_plan": "<原路径如何保留>",
    "invariant_sites": ["<仅 absent_consumer_elision：所有可能破坏不变量的写入点>"],
    "equivalence_basis": "<局部语义等价依据>",
    "dominance_threshold": 0.8
  },
  "context": {
    "prepareProject": {},
    "analyzeHotspot": {}
  }
}
```

字段约束：

- `hot_path_evidence` 是本 Skill 的准入凭据。`outcome_distribution` 与 `value_distribution` 至少有一个非空，且必须来自实测（instrumentation、采样 trace 或 benchmark 计数），不能是估计值。
- `disassembly` 可选，但走 `dominant_outcome_inline`、`leaf_entry_split`、`constant_width_copy`、`snapshot_before_copy` 时必须提供，因为这几族配方的动作与验收都要看生成代码。
- `strategy_payload.equivalence_basis` 为空时不允许执行 `constant_width_copy`、`snapshot_before_copy` 与 `hot_value_first_dispatch`。
- `absent_consumer_elision` 必须给出非空 `invariant_sites`；只给结论不给维护点的一律拒绝。

## 辅助脚本

```bash
# 值/结果分布：直方图、top-k 覆盖率、达到阈值所需的最小 k
python3 scripts/value_distribution.py --input samples.txt --threshold 0.9
python3 scripts/value_distribution.py --input outcomes.json --json

# 生成代码开销探针：函数指令数、调用点、栈帧、callee-saved 保存
python3 scripts/codegen_overhead_probe.py --dump objdump.txt --symbol evbuffer_add
python3 scripts/codegen_overhead_probe.py --dump base.txt --compare cand.txt --callee notify_impl
```

两个脚本都是只读分析器，不修改源码。脚本说明见 `scripts/README.md`。

## 执行步骤

1. Read `source_file` 的目标函数与全部调用点，确认热点成本的位置：是调用机制、分派结构、还是被丢弃的记账。若成本在被调函数内部的循环或访存，返回 `success=false` 并说明"成本不在调用机制上"。
2. 运行 `codegen_overhead_probe.py` 分别报告目标符号与候选被调符号的指令数、调用点、栈帧字节、callee-saved 保存数。没有反汇编证据时不得声称"调用开销占主导"。
3. 运行 `value_distribution.py` 量化 `outcome_distribution` 或 `value_distribution`。主导分支覆盖率低于 `dominance_threshold`（默认 0.8）时，`dominant_outcome_inline`、`leaf_entry_split`、`hot_value_first_dispatch` 都应返回 `success=false`，因为收益上界不足。
4. 按下方"证据到配方"的映射选择 recipe。一次只改一族，不混合多个 transform。
5. 枚举**通用实现的所有慢分支**（例如冻结、溢出、观察者非空、不可写、容量不足、加锁、错误路径），逐条给出快路径的对应 guard。guard 集合必须覆盖每一条，覆盖不到的必须显式列入 `uncovered` 并因此拒绝执行。
6. 生成最小改动：保留原实现作为 fallback；`leaf_entry_split` 要求原实现改成不可内联的独立函数；`dominant_outcome_inline` 要求重体留在 out-of-line 符号中。
7. 写出 `codegen_expectation`：预期消失的调用、预期出现在调用方的谓词、预期被 out-of-line 的符号。这是下游验证阶段的检查清单，必须可机械核对。
8. 输出 JSON。`success=false` 时必须给出 `error_message` 与已排除的原因。

## 证据到配方的映射

| 观测到的证据 | recipe | 核心动作 |
|---|---|---|
| 某被调函数 ≥80% 的路由命中同一廉价早退，调用方为此付出序言/保存寄存器/间接调用 | `dominant_outcome_inline` | 早退谓词与动作内联到调用方，重体保留为 out-of-line 符号 |
| 主导场景不需要任何 callee-saved 寄存器，通用实现多数调用走慢分支 | `leaf_entry_split` | 新增精确 guard 的叶子入口，原实现改名并 `noinline` 作 fallback |
| 拷贝长度来自运行期参数，取值集中在小集合，且反汇编显示对通用拷贝例程的调用 | `constant_width_copy` | 按主导长度分类，用编译期常量宽度拷贝替代通用调用 |
| 分派链按自然顺序排列，但实测命中集中在非首位分支 | `hot_value_first_dispatch` | 按实测频率降序重排，复用已算出的分类结果 |
| 记账/通知的消费者当前不存在，结果被下游丢弃 | `absent_consumer_elision` | 廉价判定消费者缺失时跳过，并维护不变量 |
| 写入后编译器重复加载刚写过的字段（别名重载） | `snapshot_before_copy` | 写入前快照，写后从快照更新 |

## 集中能力族

| recipe | 触发条件 | 允许动作 | 拒绝条件 | 验证重点 |
|---|---|---|---|---|
| `dominant_outcome_inline` | 早退占比 ≥ `dominance_threshold`；早退只做 ≤ 约 8 条指令的有效工作；反汇编显示调用方有保存寄存器或间接调用 | 把谓词与早退动作移入调用方可达的内联位置（头文件 static inline 或调用点内联）；重体改名并保留 out-of-line；需要 ABI 兼容时保留旧符号薄封装 | 早退动作含不可在调用方复现的副作用；需要改变公开 ABI 且调用方不可同改；谓词无法从已有字段廉价求得；无分布证据 | 调用方内出现该谓词的比较；被调符号不再出现在热路径调用点；空与非空两种结果都覆盖 |
| `leaf_entry_split` | 主导场景可用 callee-saved 免用方式实现；通用实现在多数调用中进入慢分支 | 公共入口改为 dispatcher，主导场景走精确 guard 的叶子实现，其余 tail-call 到 `noinline` 的原实现 | guard 无法枚举完备；叶子实现会复制分配/错误处理等可选逻辑；语言无 noinline 等价物（Rust 用 `#[inline(never)]`） | 每条 guard 的 fallback 命中；叶子命中；反汇编确认无栈帧或 callee-saved 溢出 |
| `constant_width_copy` | 长度取值 top-k 覆盖 ≥ 阈值；反汇编存在对通用拷贝例程的调用（直接或 PLT 间接） | 按长度值分类，主导长度用编译期常量宽度拷贝；其余长度走原通用实现 | 分布平坦或不可预测；编译器已把通用调用内联成等价常量宽度（先看反汇编，成立就不再改）；涉及重叠拷贝语义 | 每个主导长度与通用长度、边界与对齐组合的等价性；反汇编确认通用调用仅在 fallback 分支 |
| `hot_value_first_dispatch` | 实测命中集中在非首位分支；每次命中都要重复同一分类 | 按频率降序重排分派；复用已计算的分类结果 | 频率证据缺失或来自猜测；重排改变副作用顺序；分支谓词之间有依赖 | 每个取值仍路由到同一实现（等价性表）；主导值命中首个分支 |
| `absent_consumer_elision` | 热路径上的记账/通知存在可廉价判定的"无消费者"状态；被省工作在该状态下结果必然被丢弃或清零 | 跳过该工作并建立不变量（如"观察者集合为空 ⇒ 计数为零"），在所有可能破坏不变量的写入点修复或断言 | 无法枚举全部维护点；跳过会让之后注册的观察者丢事件而语义不允许（确认为语义变更时必须在 `risk_notes` 写明并取得接受）；判定本身比被省工作更贵 | 每个维护点的不变量断言；观察者从无到有、从有到无、重复注册与移除；延迟通知机制仍工作 |
| `snapshot_before_copy` | 拷贝写入后出现对刚写字段的重复 load | 写入前把将被重载的字段快照到局部变量，写后从快照更新 | 字段可能被其他线程并发修改且当前无锁保护；快照改变错误路径可见状态 | 反汇编重复 load 消失；并发路径仍在原锁内 |

配方选择规则：

- 同一函数只选一族；若证据同时支持多族，选调用层最外层的那一族，让下游配方在 fallback 内自然受益。
- `leaf_entry_split` 与 `dominant_outcome_inline` 可以先后用于同一函数，但必须分两次提交与两次验证。
- `constant_width_copy` 属于"消除通用例程调用"，不是"为某长度加特例分派"；后者属于局部特化，不在本 Skill 范围内。

## 拒绝条件

出现以下任一情况返回 `success=false`：

- 没有实测分布证据，或主导覆盖率低于 `dominance_threshold`。
- guard 集合无法覆盖通用实现的全部慢分支（此时 `guard_completeness.uncovered` 必须非空）。
- 需要跨模块改变公开 ABI 而调用方不在本次改动范围内。
- 需要引入全局可变状态、线程本地缓存或改变生命周期语义。
- 改动会让 benchmark 或测试自身受益而非被测函数受益。
- 目标代码是 constant-time 或安全敏感比较，任何数据相关的提前返回或分支都不允许。

## 输出

```json
{
  "hot_path_result": {
    "success": true,
    "recipe": "<recipe>",
    "evidence_used": {
      "dominance": "<实测覆盖率>",
      "codegen": {"symbol": "<symbol>", "instructions": 0, "call_sites": 0, "stack_bytes": 0, "callee_saved": 0}
    },
    "original_code": "<原始函数或片段>",
    "optimized_code": "<改动后的代码>",
    "modified_region": "call_boundary|dispatch|bookkeeping|copy_site",
    "guard_completeness": {
      "general_slow_branches": ["<通用实现的慢分支>"],
      "covered_by": ["<对应 guard>"],
      "uncovered": []
    },
    "fallback_preserved": true,
    "invariant_maintained_at": ["<仅 absent_consumer_elision：维护点>"],
    "codegen_expectation": {
      "removed_calls": ["<预期消失的调用>"],
      "inlined_predicates": ["<预期出现在调用方的谓词>"],
      "outlined_body": "<预期被 out-of-line 的符号>"
    },
    "verification_focus": ["guard hit", "fallback hit", "边界值", "生成代码"],
    "risk_notes": "<语义变更、平台限定与残余风险>",
    "error_message": ""
  }
}
```

## 平台适用性

- `constant_width_copy` 与 `leaf_entry_split` 的收益依赖调用约定与通用例程的实现。同一份配方在不同调用约定与指令集上的收益不同，必须用目标机反汇编确认通用调用或慢分支确实存在；反汇编显示编译器已经内联成等价代码时，返回 `success=false`，不要制造无收益的改动。
- 平台限定必须显式写进 `risk_notes`。把配方限定在目标架构是允许的，但 `fallback_preserved` 与语义等价要求不因平台而放宽。
- Rust 下用 `#[inline]` / `#[inline(never)]`、`#[cold]` 表达同一组意图；`constant_width_copy` 用常量数组解引用或 `copy_from_slice`；不引入新的 `unsafe` 别名假设。

## 参考资料

- `references/methodology.md` — 机制开销族的取证与验收方法：成本位置判定、证据三件套、guard 完备性、A/B 协议、因果验证、跨平台拒绝、拒绝台账。
- `references/case-study-buffer-append-notify.md` — 一个事件缓冲区库上六族配方的完整落地记录，含机械核对输出、目标机记录与拒绝记录。

## Compatibility

- 目录自包含：`SKILL.md` + `scripts/` + `references/` + `cases/` 即全部依赖，拷贝该目录到任意位置即可直接运行脚本与用例（路径均以技能目录为根）；容器化构建按仓库约定以插件根为上下文。
- 输出只有 `hot_path_result` 一个顶层对象，不读写任何其他 skill 的结果对象。
- 失败统一走 `hot_path_result.success=false` + `error_message`，由调用方决定如何呈现。
- 新增 recipe 时只能扩展 `recipe` 枚举与"证据到配方"映射，不得新增必需顶层字段。

## 规则

- 不做源码写入；只返回原始文本与优化文本，由调用方落盘。
- 不按库名、函数名、benchmark 名触发规则；触发只来自结构特征与实测分布。
- 每条改动都必须有对应 fallback，`absent_consumer_elision` 的语义变更除外，且必须在 `risk_notes` 显式声明。
- `codegen_expectation` 必须可机械核对，不能写"性能更好"这类不可验证的期望。
- 不修改 benchmark、测试 harness 或编译脚本，除非输入明确声明优化对象就是它们。
- 报告收益时必须区分"机制开销下降"与"端到端吞吐提升"；本 Skill 只对前者负责，后者由验证阶段实测。
