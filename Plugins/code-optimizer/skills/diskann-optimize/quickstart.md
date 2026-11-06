# DiskANN 快速入门

在 KPBot 仓库根目录运行安装器，将项目目录指向待优化的 DiskANN 源码。Claude Code 使用 `claude` 平台参数。

```bash
bash install.sh project opencode /path/to/DiskANN
```

安装器提供 `diskann-optimize`、`diskann-source-analysis`、`diskann-benchmark-baseline` 和两个公共依赖 `vector-experiment-methodology`、`vector-ut-st-validation`，并注册五个专业子 Agent。使用当前主 Agent（例如 OpenCode 的 Build）发起任务。

在目标项目中提出任务，例如：

> 使用 vector-search-optimize 优化当前 DiskANN 检出。实验保存到指定 run_dir，使用已提供的测试工具、数据和资源限制，先建立基线再选候选，完成 UT/ST 和性能复验，交付本地结果。

初始化使用 workflows/scripts/init_run.py，支持非 Git 发布源码；恢复前运行 validate_state.py。v1 状态先预览 migrate_state_v1_to_v2.py --check，再在完整实验目录中执行 --apply。Wiki 归档按任务要求启用，不是前置条件。

性能用例位于 diskann-benchmark-baseline/cases/rust-memory-search/，详细命令和适用范围见该技能的 references/performance-case.md。
