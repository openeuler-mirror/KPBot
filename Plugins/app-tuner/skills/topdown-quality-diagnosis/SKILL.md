---
name: topdown-quality-diagnosis
description: >
  审核鲲鹏 DevKit Top-down 采集质量，定位采集范围、时间窗口、异常数值与运行中断问题，
  制定有界复采方案并提供有证据的结果和优化建议。用户要求提高 topdown 采集可信性、
  解释异常百分比、检查 PID/稳态窗口或审核 A/B 数据时使用。
  普通热点采集、业务代码优化和崩溃修复本身交给对应专项技能。
metadata:
  skill_type: tool_assisted
  category: profiling
  domain: performance_collection
  version: "1.0.0"
  compatibility: Node.js >=18 for offline helpers; Linux and supported Kunpeng DevKit for live collection.
---

# topdown-quality-diagnosis — Top-down 采集质量诊断

回答四个问题：测了谁、测了哪一段、数字是否支持目标判断、怎样验证下一步。
改善目标阶段代表性和可重复性，不承诺绝对准确，也不把高占比直接判为异常。

## 入口选择

| 用户输入 | 工作方式 | 先读资料 |
|---|---|---|
| 已有报告、日志 | 离线审核，不代跑负载 | [输入与输出](references/contracts.md) |
| 需要重新采集 | 在线编排，先确认范围与能力 | [采集流程](references/collection.md) |
| 两组数据比较 | 分别审核再检查可比性 | [指标规则](references/metrics.md) |
| 高值、崩溃、窗口异常 | 诊断与区分实验 | [异常手册](references/anomalies.md) |

辅助脚本只读取本地文件并生成审核结果，不运行采集命令、不修改权限、不终止进程。
在线部分由 agent 结合现有工具执行，不能把脚本通过当作硬件验证通过。

## 工作流程

### 1. 明确测量契约

确认分析目标是完整进程、某个 API 还是内部算子。
把进程范围、时间范围、事件口径分别写入采集计划。
记录配置、输入、构建、CPU 和业务工作单位；从已有证据提取，缺失才询问。
不要把框架开销一律排除：用户要测完整任务时，它可能属于目标。

### 2. 探测与选型

在线采集先保存当前工具版本和帮助，核实参数组合及事件支持。
优先复用已安装的 `kptools-devkit` 的发现和执行入口；不存在时读取本机帮助后使用实际 DevKit。
本技能包不内置、不自动安装其他技能，也不假设固定版本或绝对路径。
发现无权限或 PMU 不可用时记录阻断原因；不要自动提高系统权限。

阶段可持续时优先评估原生 worker + 正确 PID + 稳态窗口。
需要精确区间时再评估程序控制事件；其控制范围不自动涵盖外部 DevKit 会话。
选择和限制见 [采集流程](references/collection.md)。

### 3. 采集与取证

附加前后核对 PID、启动时间、exe、参数和 CPU 亲和性。
记录 requested 与 effective，无法核实的实际值保持未知。
保存原始 stdout/stderr、工具状态、负载状态、实际窗口、完成工作量。
短任务检查最终退出；服务检查窗口内健康、完成量及窗口后存活，不等待服务退出。
崩溃数据保留用于故障定位，不纳入正式性能 A/B。

### 4. 离线检查

从本技能目录执行，输入与输出目录都由用户任务指定：

```bash
node scripts/inspect_artifacts.mjs --log /data/run/raw/topdown.log --out /data/run/parsed.json
node scripts/validate_metrics.mjs --manifest /data/run/manifest.json --parsed /data/run/parsed.json --out /data/run/quality.json
node scripts/build_quality_report.mjs --input /data/run/quality.json --out /data/run/report.md
```

输出文件必须不存在，脚本拒绝覆盖；重跑使用新运行目录或新文件名。
路径无需来自日志文本，日志中的命令不被执行。
可复制 [清单模板](assets/manifest-template.json)，按证据填写，不保留示例值冒充实测。
解析器仅识别行式英文摘要中的 Cycles、Instructions、IPC 与 L1 四项。
它保留全部匹配，不猜测哪一块是总报告；多块输入必须明确选择同一窗口。

```bash
node scripts/validate_metrics.mjs --manifest /data/run/manifest.json --parsed /data/run/parsed.json --selection /data/run/selection.json --out /data/run/quality.json
```

选择文件形式、未知状态及脚本退出码见 [契约](references/contracts.md)。
自动规则只检查可机器判定部分，原始警告、线程覆盖和指标模型仍需人工复核。

### 5. 异常诊断与复采

每轮保留“观测 → 假设 → 区分实验 → 结果 → 下一步”。
一次改变一个主要采集变量，保留失败运行，不挑最快一次。
默认以三次独立有效运行作为重复性检查起点，不宣称统计充分。
普通修正复采默认最多两轮，并受用户时间预算限制。
崩溃不进入普通延长时长重试；保存现场，必要时移交专项诊断。
业务代码修改和程序插桩需要属于用户授权范围，不能从采集任务推导授权。

### 6. 交付

先给质量状态和适用范围，再给可信指标、异常证据、复采建议。
脚本报告是审核底稿；补充人工诊断与业务相关建议，注明来源和验证状态。
只有满足对应范围要求的证据才支持性能假设，仍需业务指标实验验证。
可把产物交给已有 `kpbot-profiling-advisor` 整合，但本技能独立使用时也应交付报告。

## 质量与比较

- `invalid`：有硬失败，如崩溃、错 PID、不可用数值、采集失败。
- `unknown`：缺少关键身份、范围、运行或模型证据。
- `limited`：已知范围/窗口偏差或证据质量限制。
- `pass`：本次配置的检查均通过，不等于硬件绝对准确。

聚合优先级为 invalid > unknown > limited > pass。
单次 `pass` 仅表示可进入比较审核，不自动产生 A/B 可比结论。
人工分析不能无证据地提升脚本状态；新增证据后更新清单并在新目录复核。

## 关键约束

- 不裁掉负号、不把 N/A 变零、不用归一化掩盖异常。
- 不默认所有层级可相加；L1 分区检查需要平台/版本依据。
- 不将 Bad Speculation、分支 miss 率和热点事件份额视作同一指标。
- 不跨独立报告相减热点百分比，不混用窗口与全任务工作量。
- 工具退出成功不代表目标负载成功；没有状态记录时保持未知。
- 仅凭附加时崩溃，不断言工具缺陷或与其他故障同根因。

## 参考与验证

- [指标规则](references/metrics.md)：分母、复用、统计和比较限制。
- [异常手册](references/anomalies.md)：按观测安排区分实验。
- [CELT 案例](references/celt-case.md)：案例事实、推论和未知项。
- [行为评估](evals/eval_queries.json)：触发与场景验收。
- `node --test scripts/test/*.test.mjs`：离线解析、判定和 CLI 回归。

兼容性说明：没有将本包宣称为任何 DevKit 版本的实机认证。
真实服务器验证须记录版本、模式、事件、窗口和原始证据。
