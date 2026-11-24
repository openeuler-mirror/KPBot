---
name: kpbot-ray-data-performance-analysis
description: 分析 Ray Data ETL、预处理和批推理流水线的吞吐、耗时、资源使用与执行瓶颈。用户提供 Dataset 代码、运行日志、stats 或 profile，或希望采集性能基线时使用；支持离线证据分析和用户指定命令的现场测量。
---

# Ray Data 性能分析

面向表格、文本、图像等 Ray Data 负载，交付可追溯的瓶颈判断、证据缺口及验证实验。无需固定数据集、存储服务、硬件架构或 benchmark。仅分析用户要求的范围，不安装框架补丁、不修改 UDF 或系统配置。

## 使用范围

- **离线**：读取已有代码、执行计划、Dataset stats、日志和采样结果；缺失项标为未知，不要求为了出报告重新部署。
- **现场**：在用户指定的环境运行采集命令。自动工具仅提供当前节点快照和带超时的命令计时；进程采样、GPU 和 Ray 指标使用目标环境已有工具按需采集。
- Linux ARM/x86 为现场资源采集目标；其他系统支持命令计时、结果比较及离线分析，Linux 专项字段标为不可用。
- 单节点是首版现场范围。多节点材料必须标明节点、rank/worker、时间基准；仅有 driver 快照时不能下集群级结论。Ray Serve、Train、Tune 不是本 skill 的专项诊断范围。

## 工作方法

1. **确定测量契约**：记录业务目标、输入规模/分布/身份、输出正确性约束、计时范围（启动/读取/计算/写出）、指标单位和方向。确认真实推理或模拟负载、冷/热缓存及资源范围；只追问影响结论的缺项。
2. **按需取证**：离线模式先列出现有证据。现场可执行 `python3 scripts/inspect_environment.py --out <新文件>`；它不导入 Ray、不连接集群。用当前版本的公开接口收集 `ds.explain()`、终结操作后的算子统计。Ray 2.52 中对尚未执行的 `ds` 调用 `materialize()` 后，原对象的 `ds.stats()` 可能仍为空串；应以 `measured = ds.materialize()` 后再读 `measured.stats()` 取得同一轮统计，保留原文和版本。不为采集再次执行惰性 Dataset；先明确哪次执行被测。详见 [采集指南](references/collection.md)。
3. **建立执行画像**：把源码的逻辑阶段与实际融合后的 operator 对应，记录 task/actor、输入输出、batch/block、并发和资源请求。不将累加的 task CPU/墙钟时间当成端到端耗时，也不把重叠阶段时间相加成占比。
4. **提出可证伪判断**：结合阶段统计、任务状态、系统或采样栈区分 I/O、计算、调度、内存、数据表示转换和写出成本。参照 [诊断方法](references/diagnosis.md)，每项附证据位置、替代解释和最小区分实验；低 CPU、线程数多、spill 或热点函数名单独都不足以证明主因。
5. **只做所需验证**：需要测量时，先短跑再选择合适重复次数；多轮配对或交错对照，并保存原始样本。已有用户 harness 优先使用；可选 `scripts/record_run.py` 包装一条命令，脚本只测量一次完整子进程运行，吞吐由核验后的工作量换算。输出行数可能因 filter/expand 改变，不能一律要求输入输出相等。比较契约见 [记录与比较](references/measurement.md)。
6. **交付分析**：报告目标与范围、环境、执行画像、按影响排序的判断（已确认/待验证/证据不足）、证据引用、测量波动、正确性缺口及下一步实验。没有可靠证据时保留结论，不输出承诺收益。用户仅要分析时不自动执行候选配置变更。

## 工具

脚本路径相对本 skill 安装目录，Python 3.10+，仅标准库。输出文件不得覆盖已有证据；使用新路径。

```bash
python3 scripts/inspect_environment.py --out results/environment.json
python3 scripts/record_run.py --out results/record-001 --timeout 300 -- python3 -c 'print("timing-demo")'
```

上面第二条是自包含的计时演示；正式测量时替换为用户授权的前台命令。仅当已有两份符合测量契约的实际记录时执行比较，将变量设为本次记录的路径：

```bash
: "${BASELINE_JSON:?set BASELINE_JSON to the actual baseline record}"
: "${CANDIDATE_JSON:?set CANDIDATE_JSON to the actual candidate record}"
python3 scripts/compare_runs.py "$BASELINE_JSON" "$CANDIDATE_JSON" --out results/comparison.json
```

`record_run.py` 不经 shell 解释参数；确需 shell 的用户命令可显式传 `bash <脚本>`。运行目录默认以属主专用 `0700` 创建，`stdout.log`、`stderr.log`、`run.json` 以 `0600` 创建；日志和命令仍可能含敏感信息，分享前检查脱敏；工具不采集环境变量全集、凭据或业务数据。

比较输入由用户 harness 或 Agent 按 [测量契约](references/measurement.md) 整理，不能假装工具能自动理解任意 benchmark 输出。未知条件、失败运行、未验证正确性或不同的对照条件都会阻止工具计算收益。

## 组合与依据

可复用已有 nmon/进程/网络采集结果；不要求安装其他 skill。后续需要系统调优或源码优化时，将本报告交给 `kpbot-app-tuner` 或 `kpbot-code-optimizer`，由用户任务范围决定是否继续。

- [公开来源与版本边界](references/sources.md)：Ray 公共文档；具体命令须匹配目标版本。
- `cases/analyze-records/case.yaml`：tier A 工具回归，包括表格、文本、图像三类合成记录。它不证明真实 Ray 性能或 Agent 的分析效果。

## 安装后的验证

纯逻辑用例：`bash cases/analyze-records/run.sh`，结果为 cases/analyze-records/results/result.json；它不依赖 Ray。真实 Ray 用例：在隔离的 Python 3.11 环境（独立 venv 或专用容器均可）安装 `ray[data]==2.52.0 numpy==2.1.3 pyarrow==19.0.1`，再执行 `bash cases/real-ray/run.sh`。需要 Linux、GNU coreutils timeout（支持 `--kill-after`）、至少 2 CPU 和足够共享内存；缺少这些前置时入口先做依赖探测，写出 `env_not_satisfied` 原因并以退出码 3 结束，表示环境不满足而非用例失败。入口会校验 `timeout --version` 确为 GNU coreutils，BusyBox 等非 GNU 实现同样按环境不满足处理。用例启动自己的本地 Ray，执行六轮 256 行 ETL，核对精确结果，保存计划、stats、环境和对照结果；退出时关闭本次 Ray。结果位于 results/real-ray/result.json，应 success=true、verified_rounds=6。两项用例均使用尚未存在的结果文件/目录，保留旧证据时换一个新的技能副本运行，不覆盖历史结果。

这验证真实执行与测量工具链，不证明 Agent 能自动识别任意业务主因。如何将这些产物交给分析流程，见[真实证据演练](references/worked-example.md)。
