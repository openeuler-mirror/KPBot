---
name: rocksdb-workflow-baseline
description: 总工作流：先调用 rocksdb-deploy-baseline 构建基线产物，再调用 rocksdb-test-baseline 完成 RocksDB 单实例基线性能测试。Use this skill whenever the user asks for the full RocksDB baseline workflow, a one-command baseline run, or wants deploy and test skills orchestrated together, even if they do not mention "workflow" explicitly.
license: MulanPSL-2.0
compatibility: Requires the sibling skills rocksdb-deploy-baseline and rocksdb-test-baseline, plus their runtime prerequisites.
metadata:
  skill_type: benchmark
  category: workflow
  domain: rocksdb
  activation_mode: active
  side_effect_free: "false"
  version: "1.0.0"
---

# RocksDB Baseline Workflow

一个入口完成基线测试：

```bash
bash scripts/run_workflow.sh --work-root /data/rocksdb-baseline --data-dir /data/workload_data
```

执行顺序：

1. `rocksdb-deploy-baseline`：安装依赖、构建 RocksDB、构建 YCSB distribution、写 `deploy.env`。
2. `rocksdb-test-baseline`：读取 `deploy.env`，执行 preflight、load 100W、drop pagecache、warmup 100W、workload a-f、report、数据备份。

## 起点

必需输入:

- `rocksdb-deploy-baseline` 和 `rocksdb-test-baseline` 可访问
- `--work-root` 可写
- `--data-dir` 可写；自定义目录需要 `--force-wipe`
- 可选 `--rocksdb-src`、`--ycsb-src` 用于离线构建

预期产物:

- `env/deploy.env`
- `env/workflow.env`
- `summary.json`、`summary.csv`、`REPORT.md`
- 数据备份路径写入 `env/cleanup.env`

## 节点输出

| 节点 | 消费 | 产出 | 下游读取 |
|---|---|---|---|
| deploy | `work_root`、源码、依赖 | `deploy.env`、`YCSB_DIST`、RocksDB jar | test |
| test | `deploy.env`、`data_dir` | `summary.json`、`REPORT.md`、`cleanup.env` | 父任务/用户 |
| workflow | 两个子节点状态 | `workflow.env` | 父任务/用户 |

## 入口预检

启动任何节点前，按所选模式检查入口:

- 完整流程:deploy 和 test 入口都必须可读
- `--deploy-only`:只检查 deploy 入口
- `--test-only`:检查 test 入口，并要求共享 `result-dir` 下已有 `env/deploy.env`

入口缺失时写 `workflow=failed`、`FAILED_PHASE=precheck`、`EXIT_CODE=2`，不安装依赖、不构建。

## 分支条件

- `--deploy-only`:只执行 deploy，写 `workflow=deploy_only` 后结束
- `--test-only`:跳过 deploy，要求共享 `result-dir` 下已有 `env/deploy.env`
- 默认:deploy → test
- `--skip-deps`:跳过依赖安装，要求工具链已具备
- `--keep-data`:test 阶段保留原数据目录，不生成备份

## 异常处理

- deploy 失败:写 `workflow=failed`，保留 deploy 日志和已有产物，不启动 test
- test 失败:保留 test 日志，默认备份数据目录并写 `cleanup.env`，`workflow.env` 记录 `FAILED_PHASE=test` 和原始 `EXIT_CODE`
- 缺少 `deploy.env` 或子 skill:预检阶段写 `workflow=failed` 后退出 2，不静默跳过
- `workflow_command.txt` 保存解析前的完整参数，便于复现
- 无法备份数据:保留原数据目录并提示人工检查

## 终止条件

- 默认流程:deploy 和 test 均成功，`workflow=ok`
- 仅部署:deploy 成功，`workflow=deploy_only`
- 任一节点失败:停止后续节点，`workflow=failed`，保留证据和备份路径

## 交回

向调用方返回:

- `workflow.env`:流程状态
- `deploy.env`:部署产物路径和版本
- `summary.json` / `REPORT.md`:测试结论
- `cleanup.env`:数据备份路径和人工清理提示

两个子 skill 默认位于当前 skill 的兄弟目录，也可用 `DEPLOY_SKILL` / `TEST_SKILL` 指定。

## 常用选项

```bash
# 只部署
bash scripts/run_workflow.sh --deploy-only

# 只用已有部署产物跑测试
bash scripts/run_workflow.sh --test-only \
  --result-dir /data/rocksdb-baseline/workflow/<timestamp>

# 复用已有构建
bash scripts/run_workflow.sh --skip-deps --rocksdb-src ... --ycsb-src ...

# dry-run
bash scripts/run_workflow.sh --dry-run
```

完整参数：

```text
--deploy-only
--test-only
--work-root DIR
--data-dir DIR
--result-dir DIR
--skip-deps
--rocksdb-src DIR
--ycsb-src DIR
--cpu-list LIST
--numa-node N
--keep-data
--force-wipe
--dry-run
```

最终结果在共享的 `--result-dir` 下：

```text
env/deploy.env
env/workflow.env
summary.json
summary.csv
REPORT.md
```

`rocksdb-test-baseline` 负责性能报告和数据备份；workflow 只负责调度和汇总状态。
