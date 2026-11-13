# 脚本说明

两个脚本都是只读分析器，不修改源码、不写仓库文件。

## value_distribution.py

把观测样本变成"是否值得特化"的判据：直方图、top-1 占比、达到阈值所需的最小 k，并给出与 `SKILL.md` 配方一一对应的推荐。

输入支持三种：每行一个样本的文本、CSV（`--column` 选列）、JSON（样本列表、`{"samples": [...]}`、或 `值 -> 计数/占比` 映射）。数值样本会归一化分箱，`5`、`5.0`、`"5"` 落在同一箱。

```bash
python3 value_distribution.py --input lengths.txt --threshold 0.9
python3 value_distribution.py --input outcomes.json --json
python3 value_distribution.py --input run.csv --column 2 --max-k 4
```

输出字段：`total`、`distinct`、`histogram[]`、`top1_share`、`min_k_for_threshold`、`top_k_share`、`recommendation`、`reason`。

推荐规则：

| 条件 | recommendation |
|---|---|
| `top1_share >= threshold` | `dominant_outcome_inline` |
| 前 `max_k` 个取值覆盖达到阈值 | `constant_width_copy` |
| 前 `max_k` 个取值覆盖不足 | `reject` |

`reject` 是正常结果：分布不足以支撑特化时应当放弃候选，而不是先改再看。

## codegen_overhead_probe.py

解析 AArch64（鲲鹏）的 `objdump -d`，逐符号报告指令数、直接/间接调用点、栈帧字节、callee-saved 使用、是否叶子、入口附近是否存在跳向 `ret` 的条件早退。

单个符号：

```bash
objdump -d --no-show-raw-insn build/lib/libfoo.a > dump.txt
python3 codegen_overhead_probe.py --dump dump.txt --symbol append --offset 0x78
```

对照两个构建（这是 `codegen_expectation` 的机械核对）：

```bash
python3 codegen_overhead_probe.py --dump base.txt --compare cand.txt \
    --callee notify_impl --symbol append --offset 0x78 --outlined notify_slow
```

对照模式输出 `checks[]`：

| check | 含义 |
|---|---|
| `callee_call_sites` | 被调符号的调用点是否减少（期望 `after < before`） |
| `outlined_body_present` | 预期 out-of-line 的符号是否只出现在候选构建里 |

任一 check 不通过时退出码为 1，可直接作为门禁。`--offset` 用来确认"谓词是否移入调用方"：改动前该偏移的操作数应为 0，改动后应为正数。

早退判定是启发式（默认前 24 条指令），只用于定位候选，不用于最终判定；最终判定看 `callee_call_sites` 与 `--offset`。

## 局限

- 依赖 `objdump` 的输出格式；不同 binutils 版本的缩进与符号修饰可能不同，脚本按"函数头 + 制表符分隔的指令行"解析，遇到不识别格式会返回空结果而不是猜。
- 不分析内联展开后的行号信息，也不替代 profiler。它证明"机制变了"，不证明"变快了"；后者由 A/B 实测负责。
