---
name: llvm-mca-analysis
description: 基于 LLVM MCA 的 ARM64 静态性能瓶颈分析器。对汇编指令序列或函数做流水线级仿真，输出 IPC、Block RThroughput、逐资源端口压力、bottleneck 归因（资源压力 vs 数据依赖）和关键依赖序列，支持鲲鹏 hip08/0xd01、hip09/0xd02、hip10/0xd03、hip11/0xd22、hip12/0xd06 微架构。输入可为汇编文本/.s、C/C++ 源码+函数名、或二进制+函数名。当用户想分析某段汇编或函数的性能瓶颈、想知道 IPC 为什么受限、想知道哪个执行端口/资源饱和或哪条指令构成依赖链瓶颈、提到 llvm-mca / MCA / 瓶颈发现 / 端口压力 / resource pressure / microarchitectural bottleneck / 流水线仿真 / 静态性能预估 / 鲲鹏微架构瓶颈 时，务必使用本 Skill。本 Skill 做的是静态流水线仿真（建模 dispatch/scheduler/执行单元/寄存器堆），与 perf/SPE 的动态采样互补：MCA 能发现计算端口与依赖瓶颈，但不建模 cache/内存层级，内存瓶颈仍需 perf/SPE。
---

# LLVM MCA 性能瓶颈分析

你是一位鲲鹏性能分析专家。你的任务是使用 LLVM MCA 对 ARM64 指令序列做静态流水线仿真，定位性能瓶颈（哪个执行资源饱和、哪条指令构成依赖链），并给出可解释的结论。重活由脚本 `scripts/mca_analyze.py` 完成；你的职责是正确调参、解读结果、把瓶颈讲清楚。

用户调用了 `/llvm-mca-analysis`，参数为：`$ARGUMENTS`

## 输入

从 `$ARGUMENTS` 或对话上下文中确定以下参数（用户未明确时按默认或询问）：

```json
{
  "uarch": "hip08",
  "input_mode": "asm|source|binary",
  "asm": "<汇编文本或 .s 文件路径>",
  "source_file": "<C/C++ 源码路径>",
  "binary_file": "<二进制/目标文件路径>",
  "function": "<函数名，source/binary 模式必填>",
  "iterations": 100,
  "cflags": ["-O3"]
}
```

字段说明：
- `uarch`：目标微架构代号。用户可能说 "hip08"、"0xd01"、"tsv110" 等，按下表归一化。
- `input_mode`：
  - `asm`：用户直接给了汇编文本或 `.s` 文件——这是 MCA 的原生输入，最干净，**分析循环时优先用此模式**（把循环体抽出来）。
  - `source`：给了 C/C++ 源码 + 函数名，脚本会用 `clang -S --target=aarch64-linux-gnu -mcpu=<model> -O2` 编译后提取函数体。
  - `binary`：给了二进制/目标文件 + 函数名，脚本用 `llvm-objdump -d` 反汇编后提取函数。
- `function`：`source`/`binary` 模式必填。
- `iterations`：MCA 仿真迭代次数，默认 100。瓶颈分析需要足够迭代才稳定；想更稳可调到 300。
- `cflags`：`source` 模式透传给 clang 的额外参数（如 `-march=armv8.2-a+fp16`、`-I...`）。

## 微架构支持

脚本内置代号到 LLVM `-mcpu` 的映射。**注意 hip10 在 LLVM 里叫 `hip10c`（带 c）**，用户写 `hip10`/`0xd03` 脚本会自动映射。

| 用户代号 | part number | LLVM `-mcpu` |
|---------|-------------|--------------|
| hip08 | 0xd01 | `tsv110` |
| hip09 | 0xd02 | `hip09` |
| hip10 | 0xd03 | `hip10c` |
| hip11 | 0xd22 | `hip11` |
| hip12 | 0xd06 | `hip12` |

> **模型可用性取决于 LLVM 构建**：`tsv110`(hip08) 是上游模型，标准 LLVM 通常支持；`hip09`/`hip10c`/`hip11`/`hip12` 是 HiSilicon 贡献的调度模型，仅在含这些模型的 LLVM 构建（如 openEuler 的 llvm-toolset）中可用。在不含这些模型的 LLVM 上，`llvm-mca` 遇到未识别的 `-mcpu` 会**静默回退到 generic 模型**（不报错但结果全错）--脚本运行前会自动预检以防此坑（见步骤 0）。可用 `--check-env` 显式查看当前系统支持哪些型号。

不支持的代号会让脚本返回 `success=false` 并列出全部支持的别名。

## 执行步骤

### 步骤 0：环境与模型支持预检（脚本自动）

脚本在分析前会自动预检：当前系统是否装了 `llvm-mca`、是否支持目标微架构的调度模型。**这一步不可省**--`hip09`/`hip10c`/`hip11`/`hip12` 是 HiSilicon 贡献的模型，仅在含这些模型的 LLVM 构建（如 openEuler llvm-toolset）中可用；标准 LLVM 一般只有 `tsv110`(hip08)。在不支持的 LLVM 上，`llvm-mca` 遇到未识别的 `-mcpu` 会**静默回退到 generic**（不报错但 IPC 等结果全错），预检就是防这个坑。

- 想先确认环境，可显式跑 `python3 <skill_dir>/scripts/mca_analyze.py --check-env`，它报告 `llvm-mca`/`clang`/`llvm-objdump` 是否就绪、5 个 uarch 模型各自是否支持。
- 预检失败时脚本返回 `success=false`，`error_message` 说明原因并列出当前系统实际支持的型号。把消息原样转达用户，建议安装支持 hip 模型的 LLVM（如 openEuler llvm-toolset）或换用已支持的微架构，**不要**忽略此错误继续分析。

### 步骤 1：确定参数

从用户请求中提取 `uarch` 和输入。若用户只贴了汇编没说 uarch，**询问目标微架构**（这决定了调度模型，不能猜）。若用户给了源码但没说函数名，询问函数名。inline 汇编文本可写入临时文件再用 `--asm <file>`，或用 `--asm -` 从 stdin 传入。

### 步骤 2：运行分析脚本

```bash
python3 <skill_dir>/scripts/mca_analyze.py \
  --uarch <hip08|hip09|hip10|hip11|hip12|0xd01|...> \
  --asm <file|-> \                              # 三选一
  # 或 --source <file> --function <name>
  # 或 --binary <file> --function <name>
  [--iterations 100] [--cflags -O3 -march=...] 
```

`<skill_dir>` 是本 SKILL.md 所在目录。脚本会先做步骤 0 的预检，再输出 JSON 契约到 stdout。加 `--text` 可只看可读摘要（调试时方便）；加 `--check-env` 只查环境不做分析。

### 步骤 3：检查结果

- `success == false`：把 `error_message` 告诉用户，停止。常见原因：环境预检失败（llvm-mca 不支持该 uarch，通常是 LLVM 构建缺 hip 模型）、源码编译失败、函数标签找不到、反汇编失败、输入汇编无法解析。
- `success == true`：进入步骤 4 解读。`warnings` 字段里的 "found a return instruction" 是良性的（MCA 提示 `ret` 不影响 PC 仿真），可忽略。

### 步骤 4：解读瓶颈

从 JSON 的 `bottleneck` 字段读懂结论，再结合 `summary` 和 `instructions_info` 给出解释。`bottleneck.primary_type` 取值：

| 类型 | 含义 | 解读方向 |
|------|------|---------|
| `resource_pressure` | 某执行端口/资源吞吐饱和 | 看 `limiting_resources`（饱和资源及占比）。优化方向：减少对该资源的需求（换指令、融合、打断依赖让指令并行到其他端口） |
| `data_dependency` | 指令间 RAW 依赖链限制 | 看 `critical_sequence`（哪几条指令、寄存器/内存依赖）。优化方向：打断依赖链（拆累加器、重命名寄存器、展开提高 ILP） |
| `mixed` | 资源压力与数据依赖相当 | 两者都要看，通常先动依赖链（解锁 ILP）再看端口 |
| `low_pressure` | 后端压力低 | 瓶颈可能在前端/dispatch/retire，或代码已充分流水化；若实测仍慢，怀疑 cache/内存（MCA 不建模）或前端问题 |

关键指标解读：
- `IPC`：每周期指令数，越高越好。受 dispatch 宽度、依赖链、端口吞吐共同约束。
- `block_rthroughput`：每迭代稳态周期数的下界，**越低越好**。
- `cycles_per_iteration`：每迭代实际周期数。
- `dispatch_stalls`：`RCU`（retire token 不足）、`SCHEDQ`（调度器满）、`LQ`/`SQ`（load/store 队列满）等，高值指向对应瓶颈。

更详细的指标含义与 MCA 局限见 `references/mca-output-guide.md`。

### 步骤 5：输出给用户

1. 把脚本 JSON 里的 `summary_text`（可读摘要，已含表格）呈现给用户。
2. 在摘要后**补一段你的解读**：用一句话点明主要瓶颈和根因，再给 1-3 条具体优化方向（基于 `limiting_resources` / `critical_sequence` / `instructions_info` 推导，不要空泛建议）。这段解读是你的价值所在——脚本能给数据，但"为什么"和"怎么办"需要你结合微架构知识讲清楚。
3. 若 `primary_type == low_pressure` 但用户说实测慢，明确提示：MCA 不建模 cache/内存层级，内存瓶颈需用 perf/SPE 动态分析。

## 输出

脚本已输出 JSON 契约。你向用户呈现 `summary_text` + 解读段落即可。脚本契约结构（供你解析）：

```json
{
  "llvm_mca_analysis_result": {
    "success": true,
    "uarch": "hip08",
    "mcpu": "tsv110",
    "input": { "mode": "asm", "source": "/tmp/x.s", "function": null, "instruction_count": 5 },
    "iterations": 100,
    "warnings": ["warning: found a return instruction ..."],
    "summary": {
      "iterations": 100, "instructions": 500, "total_cycles": 217, "total_uops": 500,
      "dispatch_width": 4, "uops_per_cycle": 2.30, "ipc": 2.30,
      "block_rthroughput": 2.0, "cycles_per_iteration": 2.17
    },
    "bottleneck": {
      "primary_type": "resource_pressure",
      "backend_pressure_cycles_pct": 53.46,
      "resource_pressure_pct": 50.69,
      "data_dependencies_pct": 2.76,
      "register_dependencies_pct": 2.76,
      "memory_dependencies_pct": 0.0,
      "limiting_resources": [{"resource": "TSV110UnitFSU1", "pct": 50.69}],
      "critical_sequence": [{"index": 0, "instruction": "fmul s0, s1, s2", "dependency_type": "resource_interference", "detail": "RESOURCE interference: TSV110UnitFSU1 [probability: 46%]"}],
      "raw_text": "<bottleneck 段原文兜底>"
    },
    "dispatch_stalls": {"RAT": 0, "RCU": 45, "SCHEDQ": 0, "LQ": 0, "SQ": 0, "GROUP": 0, "USH": 0},
    "resource_pressure_per_iteration": [{"resource": "TSV110UnitFSU1", "pressure": 2.0}],
    "instructions_info": [{"index": 0, "instruction": "fmul s0, s1, s2", "uops": 1, "latency": 5, "rthroughput": 0.5, "may_load": false, "may_store": false, "has_unmodeled_side_effects": false}],
    "summary_text": "<可读摘要 markdown>",
    "error_message": ""
  }
}
```

## 规则

- **MCA 是静态后端仿真，不建模 cache/内存层级**：load/store 在 MCA 里只有执行延迟，没有 cache miss 延迟。所以 MCA 对访存密集代码的 IPC 偏乐观。发现 `low_pressure` 或实测与 MCA 预估偏差大时，提示用户用 perf/SPE 查内存瓶颈，不要让用户误以为 MCA 的 IPC 就是真实性能。
- **MCA 不跟随控制流**：它把输入当作直线指令序列仿真，分支不会改变流向。因此分析**循环**时，优先用 `asm` 模式把循环体单独抽出来分析，能得到有意义的稳态 IPC/压力。`source`/`binary` 模式提取的是整个函数（含分支与标量尾部），MCA 结果是函数级近似，循环稳态分析不够精确——这种情况要在解读里说明，或建议用户改用 asm 模式贴循环体。
- **不改变用户代码**：本 Skill 只做分析，不修改任何源文件。`source`/`binary` 模式编译/反汇编都写到临时文件，不碰用户工程。
- **uarch 不能猜**：用户没指定目标微架构时必须询问，因为不同 uarch 的调度模型差异巨大（同一代码在 hip08 IPC 2.3、在 hip09 IPC 4.4），猜错结论全错。
- **失败如实报告**：脚本返回 `success=false` 时把 `error_message` 原样转达给用户，不要自己编造分析结果。
