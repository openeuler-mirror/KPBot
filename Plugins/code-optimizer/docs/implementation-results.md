# code-optimizer 密集反馈实施记录

实施日期：2026-09-20。开发分支：`self_evolve`。起始版本：`ac60a29`。

## 交付内容

- 两个新环节：假设与实验设计、实验验证与反馈。
- 实验记录与证据/运行条件校验。
- 现有分析、决策、验证及失败恢复回路接入；同步 Claude/OpenCode。
- 两平台安装产物测试与真实案例回放指引。

未扩展 dotproduct、evolve-skill、app-tuner 或 TF 专项；优化方法库留待后续。

## 最终检查

实现代码版本：`7c20e27`。五项任务独立审查及一次整体审查均已完成，最终修复复审通过，无剩余审查问题。

| 检查 | 结果 |
| --- | --- |
| 本机记录、反馈与安装套件 | 30 项通过 |
| Claude / OpenCode 安装后脚本 | 每个平台 28 项通过 |
| 原有安装回归 | 8 项通过 |
| Python 优化模式检查 | 通过 |
| 带未提交修改的准备流程 | 临时仓库手工步骤验证通过；不是 Agent 真机验收 |
| 真实历史案例回放 / 鲲鹏实测 | pending |

## 验证边界

本机逻辑与安装检查已执行，最终测试结果见 [回放与验收说明](feedback-replay.md)。真实历史案例回放与鲲鹏实测均待提供环境/数据。当前实现是阶段模板与辅助脚本，测试未证明模型会在任意真实任务中遵守全部流程，也未证明性能提升。

## 实施裁决（完整记录）

- Ruling: Use user-designated separate self_evolve checkout in place, no additional worktree — user specifically prepared this feature repository — cost if wrong: changes are isolated by commits but share this checkout.
- Ruling: Persist pre-execution designs separately; write_record creates immutable final attempt records — avoid contradictory write-once and pre/post updates — cost if wrong: additional draft artifact.
- Ruling: Full feedback CLI validates explanation/path and independent current_context.json (run_id, point_id, parent_state, execution_context); pure choose_next validates only its routing subset — plan fixture omits full fields and record cannot validate its own target — cost if wrong: adapters must provide current context.
- Ruling: Add strict input consistency checks around route_verification; snippets are illustrative minimums, not permission to accept contradictory statuses — spec requires reliable acceptance — cost if wrong: legacy results remain unverified until normalized.
- Ruling: Task 2 also updates existing prepare/analyze stage output templates where they enumerate fields — otherwise new producer fields are discarded by the orchestrator prompt — cost if wrong: a few additional related template edits.
- Ruling: Evidence-free records may validate as designs but cannot be finalized; failed executed attempts retain error-log evidence and target-unavailable states remain drafts — enforce lifecycle at write boundary — cost if wrong: callers must provide error logs to archive failed attempts.

## 审查修复

1. Task 1：拒绝将空证据草稿写为正式记录，明确写入失败残留目录。
2. Task 2：补齐 OpenCode 调用者覆盖、用户验证命令传递及目标环境门控。
3. Task 3：等待目标状态仅用于未执行草稿，禁止已执行记录改标。
4. Task 4：修正最后一项预算预留、后台完整命令列表和自定义正确性命令分支。
5. Task 5：安装测试实际调用安装后的模块与 CLI，避免回退测试源码。

预检曾误判调用者分析没有 OpenCode 覆盖文件，Task 2 审查发现后已纠正并同步该文件。

## 最终整体审查修复

- 正式记录写入前校验输出目录范围，拒绝通过符号链接越界写入。
- 准备阶段允许可识别且可安全隔离的未提交源码状态，保留用户修改。
- 安装测试使用显式异常，Python 优化模式不会跳过来源检查。

原始开发记录的设计选择已在上文完整归档；运行与回放入口见 [检查说明](feedback-replay.md)。所有变更保留在本地 self_evolve 分支，未合并到 master 或推送。
