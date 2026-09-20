# 密集反馈：检查、历史回放与目标验收

## 三种独立状态

| 层次 | 当前状态 | 能说明什么 |
| --- | --- | --- |
| 本机逻辑与安装 | passed：28 个顶层测试，两个安装目录各重跑 27 个记录/反馈测试；原安装回归 8 项通过 | 记录契约、路由和安装产物可用 |
| 用户真实历史回放 | pending：尚未提供逐轮原始案例 | 完成后仅说明诊断能随证据修正，不证明新性能收益 |
| 鲲鹏目标实测 | pending：尚未提供目标机器及真实局部实验 | 必须另行完成正确性、局部实验和项目最终验收 |

测试中即使字段写作 `target` 或 `historical`，数据仍是逻辑 fixture，不是实测或真实历史案例。当前不能宣称整体验收通过。

## 可复现的本机命令

从 KPBot 仓库根运行安装回归，然后进入插件目录运行完整反馈测试：

```bash
bash tests/test_install_platform.sh
cd Plugins/code-optimizer
PYTHONPATH=skills/kpbot-code-optimizer/scripts python3 -m unittest discover -s tests/feedback -v
```

`test_platform.py` 为 Claude Code 和 OpenCode 分别创建临时项目，执行 `bash install.sh project claude TMP` / `bash install.sh project opencode TMP`，检查新增 prompts、contract 和 scripts。子进程只将安装后的 scripts 加入 `PYTHONPATH`，验证两个模块的 `__file__` 和测试 CLI 的 `SCRIPT` 一致，再显式运行 `test_record`、`test_feedback`，不递归运行平台测试。输出保留实际导入路径；临时目录退出时删除。运行时不依赖本篇 docs 被安装，契约位于已安装技能的 `references/experiment-contract.md`。

## 人工逐轮历史回放

1. 案例保管人准备真实原始文件、源码状态（含未提交内容）、构建参数、机器和输入条件。将后续轮次及最终答案保存在参与诊断的 Agent 无法读取的位置，不能只要求它忽略已可见文件。
2. 第一轮只开放初始条件和第一轮观测。Agent 先写可反驳假设、替代解释、支持/反驳判据及下一步最小实验；保存这份决定后才开放下一轮结果。
3. 每轮使用独立 experiment_id，按 [实验契约](../skills/kpbot-code-optimizer/references/experiment-contract.md) 保存原始证据与记录。证据 source 必须是 `historical`，保留原命令和真实 exit_code，不执行 record 中的 command。声明 provenance 的 parent_state、execution_context、scope，并用 reuse_explanation 解释可比性；不可比或缺失证据不能提升为 supported。模型解释与原始观测分开保存。
4. 控制器独立维护 `RUN/current_context.json`，包含 run_id、point_id、parent_state、execution_context，不能为通过校验机械复制不符当前条件的旧值。预先写好的记录输入、feedback 和原始文件放在 RUN 下；feedback 包含四个 ID、execution_status、hypothesis_verdict、两个布尔检查占位、explanation、最终 record_path。
5. 在插件根执行下面的辅助命令（先把 `/absolute/path/to/replay-run` 替换为该轮真实目录；`e1` 为示例 ID）。这些工具只保存/校验 JSON，不执行历史命令。历史复用不消耗新命令额度，remaining 应取控制器的实际预算，例中为 3。

```bash
SCRIPTS="$PWD/skills/kpbot-code-optimizer/scripts"
RUN=/absolute/path/to/replay-run
python3 "$SCRIPTS/experiment_record.py" --record "$RUN/input-e1.json" --run-dir "$RUN"
python3 "$SCRIPTS/experiment_feedback.py" --record "$RUN/experiments/e1/record.json" --feedback "$RUN/feedback-e1.json" --run-dir "$RUN" --remaining 3
```

记录 writer 输出为后续命令使用的最终路径，不重复写入同一 experiment_id。下一轮重新设计并分配新 ID，核对 inconclusive→DesignExperiment、refuted→AnalyzeHotspot、supported 且证据/目标检查通过→DecideOptimization；校验失败应阻塞而不是手动强制 checked=true。逐轮保留“揭示前决定 / 新证据 / 判据对照 / 实际转向”。没有真实案例时保持 pending；单元测试不能替代该步骤。

## 鲲鹏真实验收清单

- 用户提供目标机器、真实局部案例及项目验收命令；固定源码/父状态、输入、编译器与参数、线程和运行条件，记录未提交修改。Agent 在目标环境直接执行实验并读取原始结果。
- 先记录判据和预算，预留额度后执行一次诊断，再隔离候选修改；记录命令、退出码、耗时与原始证据，不重复启动已运行或状态不明的 reservation。
- 分别给出 correctness_verdict、local_performance_verdict、system_acceptance。局部通过不能替代完整正确性和用户约定工作负载的全部验收命令；仅 correctness=passed 且 system_acceptance=passed 可交付。
- 必须检查“假设被反驳”及“局部结果不足以判定系统收益”。真实案例若没有其中分支，用另行标明的逻辑回放补覆盖，不能伪造实测分支。
- 实际 Verify JSON 可用以下命令校验路由（不执行其中命令）：

```bash
python3 "$SCRIPTS/experiment_feedback.py" --verify-result "$RUN/verify-result.json"
```

目标不可用时返回 `awaiting_target_evidence`：保留无 evidence、无 exit_code 的设计草稿，放在 RUN 内且在 experiments 外；两个检查标志保持 false，不消耗目标命令额度。最终报告分别更新上表三项并附证据路径，保留未完成项。
