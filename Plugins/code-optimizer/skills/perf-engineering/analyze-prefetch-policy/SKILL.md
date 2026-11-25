---
name: analyze-prefetch-policy
description: 当需要读取 C/C++、Rust 或汇编程序的 L1/L2 软件预取策略，或比较该预取策略的 x86-64 与 AArch64 实现时使用。交付带源码和产物证据的预取位置、目标地址、距离、条件分支及未验证项；用于策略调查，不负责插入预取或修改硬件开关。
---

# 读取程序的 L1/L2 预取策略

这是 `perf-engineering` 域的横切诊断能力，可独立调用，也可供 `decide-optimization` 在预取证据不足时调用；不承担优化流水线主入口职责。

只需回答“当前程序如何预取、不同架构是否为空实现”时，直接以 `analyze-prefetch-policy` 为入口，交付诊断状态、报告路径及缺项后结束，无需进入优化决策阶段或构造 `optimization_point`。需要新增、调整或验证预取优化时，转交 `prefetch-optimization`，并把本技能的地址、时序、距离和证据缺口作为输入；诊断结论本身不授权改写源码。

把“源码写了什么”“该构建生成什么”“该次运行执行什么”分别交回。预取指令是提示；三者都不能单独证明数据实际驻留某级缓存或程序加速。

## 输入与范围

从调用参数或上下文取得：

| 输入 | 要求 |
|---|---|
| `source_root` 或源码文件 | 源码调查必需；只有反汇编时允许转为产物调查 |
| `scope` | 程序入口、函数或阶段；未指定时先列候选，再明确本轮覆盖范围 |
| `architectures` | 待比较的目标架构，例如 `x86_64`、`aarch64`，不能用分析机架构代替 |
| `build_context` | 如已有：commit、未提交差异、target triple、编译器版本、优化参数、features/宏、构建日志 |
| `artifacts` | 可选：目标可执行文件、实际加载的动态库、带出处的反汇编或运行记录 |
| `output_dir` | 可选：指定报告目录；未指定时使用已有 `context.run_dir` 下的 `prefetch/`，独立调用则使用 `optimization_reports/run_<run_id>/prefetch/` |

`run_id` 沿用流水线上下文，独立调用时按 `YYYYMMDD_HHMMSS` 生成；`context.run_dir` 为父 RUN 的绝对路径。报告文件已存在时，在同一 RUN 内另建独立的预取报告子目录，返回实际路径，不覆盖原报告，也不更换流水线的 run_id。

源码可在任意平台只读分析；搜索优先 `rg`，缺失时用环境的文件搜索工具。二进制核对需要支持目标格式/架构的 `llvm-objdump` 或 GNU objdump；无工具时继续源码分析并标注缺项。普通文件读取无需 root。不要为只读调查安装内核模块、修改 MSR/BIOS、启动远程采样或运行目标程序。

本技能读取**显式软件预取及已有产物中的编译器生成预取**。硬件预取器状态需要对应 CPU 手册和配置证据，不能从软件指令或缺失率反推。若用户同时要硬件配置，单列问题与缺项。

## 工作流程

### 1. 固定证据身份

记录源码真实路径、commit 与工作区状态；无 Git 时记录被引用文件的 SHA-256。已有二进制也记录 SHA-256、格式和架构，将其关联到构建记录。只有版本号相同、路径名相同，不能认定源码与产物相同。

Python 包、插件、FFI 或 JIT 入口要追踪到真实后端。已有运行日志中的模块路径、build ID、进程映射可作证据；仅有新 Rust 可执行文件不能证明 Python 仍加载的旧 wheel 包含新策略。不要为了定位包而无条件 import 未知模块；先读安装元数据和现成记录。

### 2. 找定义，再追到调用点和消费点

在已确认的源码根执行以下候选搜索（PowerShell / Bash 均可使用该 `rg` 命令）。项目生成代码或依赖被忽略时，只对已定位的相关目录补查，不全盘扫描。

```sh
rg -n -i -g '*.{c,h,cc,hh,cpp,hpp,rs,s,S,asm,ll}' 'prefetch|_MM_HINT_|__pld|__prf|svprf[bdhw]|\b(prfm|prfum|rprfm|prf[bdhw])\b|pldl[123]|pstl[123]|llvm\.prefetch' .
rg -n 'target_arch|target_feature|cfg!|#if|#ifdef|PROBE_AHEAD|PREFETCH_DISTANCE|LOOKAHEAD' .
```

关键词只产生候选。继续读取完整函数、封装、调用者和相关配置，区分注释、测试、未使用实现、条件分支和真正入口。需要核实的路径为：

```text
程序入口 → 阶段/循环 → 地址计算 → 预取封装 → 实际指令/内联函数
                              ↘ 后续使用同一目标的 load/store
```

每条策略写清：目标数据结构和地址公式、读/写意图、层级/保留提示、触发条件、预取发生阶段、后续消费阶段、步长与提前距离及其单位。追踪 `base + (hash & mask)`、索引数组、指针链等间接访存，不能因其非连续就判定没有软件预取。跨阶段可能扩容、换表或重新获取基址/掩码；只有地址计算仍可对应时，才能把预取归给后续同一槽位。

区分稳态 look-ahead、循环开始的预热和尾部补齐。例如 `items[i+16]` 是提前 16 个 item；不是固定 16 字节，更不是 16 个周期。跨阶段 harvest→probe 的时间间隔通常可变，记 `kind=phase,value=null`，不把 batch 容量当成固定预取距离。

### 3. 分架构判定并解释指令

分别展开 `cfg(target_arch)`、feature、C 预处理宏和运行时分派。条件未知时保留各分支及所缺配置。名叫 `prefetch_l2` 的封装可能只在 x86 上有实现；其他架构执行空体应标 `noop`，距离填 `kind=unknown,value=null,unit=unknown`，封装调用的阶段只写入说明。仅未搜到关键词应标 `not_found_in_scope`，并给出范围，不能声称整个程序或 CPU 没有预取。

按需读 [指令判读](references/instruction-semantics.md)，核对 `PLD/PST`、`KEEP/STRM`、ACLE `__pldx` 参数、x86 hint 与 compiler locality 参数。`__builtin_prefetch(p,0,2)` 的 `2` 是 locality，不是 L2 编号；没有目标编译产物时，不填确定的 L2 映射。需要进一步查 ACLE intrinsic 时使用 `arm-instructions-query` 的预取指令参考。

读取 Fastokens/BPE 或分段流水线时，再读 [分阶段案例](references/fastokens-case.md)。只借用追踪方法，不复用历史地址、行号或测试收益作为当前证据。

### 4. 用已有产物核对生成的指令

以下为 Bash 示例；变量应指向已确认的目标文件和本轮报告目录。先创建独立报告目录，核对解析后的路径（包括符号链接或 Windows junction）位于获授权输出范围内，再重定向文本；若目标文件已有人工内容，在同一 RUN 内选择新子目录。Windows 可使用 PowerShell 变量、`Get-FileHash` 和 `llvm-objdump` 的对应命令。

```sh
sha256sum "$BINARY"
llvm-objdump --file-headers "$BINARY"
llvm-objdump --disassemble --demangle "$BINARY" > "$REPORT_DIR/disassembly.txt"
rg -n -i -C 8 '\b(prfm|prfum|rprfm|prf[bdhw]|prefetcht[012]|prefetchnta|prefetchw|prefetchwt1|prefetchit[01])\b' "$REPORT_DIR/disassembly.txt"
```

缺 LLVM 时可用支持该目标的 `objdump -f` / `objdump -d -C`；交叉架构反汇编失败应记工具不支持，不记“没有预取”。立即数形式的 Arm hint 需按 ISA 解码；SVE 的 `prfb/prfh/prfw/prfd` 要读取谓词、地址向量/偏移和 hint 操作数，不能遗漏或当成标量单地址预取。`rprfm` 等扩展需按目标手册解读。出现 `<unknown>`、`.inst` 或无法解码的字节时，先确认反汇编器及扩展支持；这些搜索表达式是起点，不是全指令集的穷举证明。

把命中地址、符号/内联上下文、访问的基址和偏移关联回调查路径。全文件有一条 PRFM，不能证明目标函数含有它。单独生成的 `.s` 也不能替代最终链接产物。检查相关 `.so/.dll`、多版本分派和 JIT 代码；缺其中一部分就缩小结论范围。仅产物调查中，已识别产物/反汇编及目标符号中的指令可记 `binary_state=verified`，缺源码时 `source_state=unknown`，源码对应关系单独写入 `artifacts[].build_link`；不得暗示产物来自当前源码。

明确范围内的指令均已完整解码并检查，仍未见预取时，可记 `binary_state=not_found_in_scope`。按 [输出契约](references/output-contract.md) 保存已检查的符号、产物地址范围和反汇编行范围，省略不存在的预取指令地址；反汇编截断或解码不全不能支持该否定结论，只能记录已检查部分及缺项。

只读任务不自动触发重编译。若任务已有构建授权，可在隔离输出目录沿用项目的 target、features 和配置重建；记录命令和产物摘要。否则交回“源码已确认，产物待核对”。

### 5. 区分运行证据与收益证据

已有同构建、同输入的记录才能支持运行路径判断。调用者被采样命中不等于每个条件分支都执行；只有证据能关联到相应路径/指令时才填 `runtime_state=observed`，否则为 `unverified`。

MPKI 按同窗口 `事件数 / instructions × 1000` 计算，保留 PMU 名称、事件原义、计数范围和 multiplex 比例。L2 refill 不直接等于需求 load miss；不同 CPU 的同名通用事件也不保证语义相同。缺失率下降不等于加速。只有用户要求效果分析时才利用可比的正确性、同负载配对耗时/吞吐量证据，否则写“收益未验证”。

### 6. 输出与停止

按 [输出契约](references/output-contract.md) 交付 `PREFETCH-POLICY.md` 和 `prefetch-policy.json`，返回实际报告路径以供上游工作流引用。人读报告含架构对照表、调用时序、地址与距离、证据和未知项；JSON 使用同一批事实。保留源码/反汇编引用及文件摘要，遵循上述 RUN 与报告子目录约定。

- `ok`：请求范围内的结论均有相应层级的证据。例如仅要求源码调查，可以正常完成，仍将运行层标为未验证。
- `partial`：已取得部分结论，但用户要求的构建、架构或运行证据缺失；保留所有已证实结论及具体缺项。
- `blocked`：无法从现有输入取得核心结论，例如没有可读取证据，或仅有二进制而工具无法解码；交回原因和所需输入。已有部分可用结论时使用 `partial`。

目标源码、编译选项和硬件配置保持只读；默认仅写本轮报告和证据副本，另有构建授权时可写约定的隔离构建目录。发现可优化点时可记录为待验证假设，实施优化由另一个明确任务负责。

## 最小验收

[tier A 用例](cases/source-only/case.yaml) 只验证验收器的逻辑与夹具契约。从本技能目录执行 `python3 cases/source-only/run.py`；[入口脚本](cases/source-only/run.py) 会在用例目录的 `results` 子目录生成 `result.json`。这是运行时产物，干净检出中不存在，并按 [忽略规则](cases/source-only/.gitignore) 不提交到仓库。该用例不调用模型，也不代表 agent 端到端或硬件性能验收。

另用 [源码调查提示](cases/source-only/prompt.md) 完成一次独立分析，再执行 `python3 cases/source-only/verify.py /path/to/prefetch-policy.json`（Markdown 默认与 JSON 同目录，文件名为 `PREFETCH-POLICY.md`）。最后由复核者按 [语义检查清单](cases/source-only/review-checklist.md) 核对地址、触发条件、消费时序及两份报告的一致性；独立分析时不向分析者提供该清单中的预期答案。脚本成功只代表自动检查通过，不能替代语义审查。覆盖范围包括架构空实现、距离单位、locality、写预取及缺产物时的未验证状态。
