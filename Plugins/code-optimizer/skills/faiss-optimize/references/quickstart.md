# faiss-optimize 使用入口

## 任务信息

提供或从上下文确认源码、算法、目标、测试工具、数据/索引及独立产物目录。已有补丁和实验记录可选；能力与构建以实际检出为准。
简单 API 问题使用算法技能；源码调查使用 analyzer；完整性能优化进入 CP0–CP7。

## 初始化示例

```bash
python3 <skill-dir>/workflows/scripts/init_run.py \
  --algorithm ivfpq --run-id ivfpq-example-01 \
  --repo /path/to/faiss --run-dir /path/to/experiment \
  --faiss-version <detected-version> --baseline-id <source-build-id>
python3 <skill-dir>/workflows/scripts/validate_state.py /path/to/experiment/state.json
```
路径、版本、运行编号与配置均是任务输入，示例不绑定具体机器、后端或时间。

## 编排

通过 KPBot 入口调用时继承模式；直接调用默认 collaboration，可按用户意图设 auto。阶段执行见[工作流](../workflows/optimize-workflow.md)，角色定义位于 agents/。
产物保存到 run_dir，只有 primary 写 state。已有记录缺失不阻断启动；参数冻结、Recall 与实际结果验证、交错性能对照和独立验收共同决定结论。提交按授权执行。
