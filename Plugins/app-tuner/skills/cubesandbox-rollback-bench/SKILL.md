---
name: cubesandbox-rollback-bench
description: >
  当需要只测量各沙箱回滚到自己 checkpoint 的并发 Rollback 性能时使用。固定且仅执行 rollback（4.4 / rollback）章节，复用统一预检、批准哈希、冒烟、锁和差集清理；不用于全矩阵、部署或未经授权的宿主机准备。
license: Apache-2.0
compatibility: Linux；已部署 CubeSandbox；共享 cubesandbox-lifecycle-bench 执行器
metadata:
  skill_type: tool_assisted
  category: benchmark
  domain: cubesandbox
  version: "1.0.0"
---

# Rollback 单项基准

## 唯一职责与输入

本 Skill 只运行 `rollback`，测量真源为官方 `bench_rollback_concurrency.py`。公共代码位于安装后的
`../cubesandbox-lifecycle-bench/scripts/`，本入口没有第二份执行器。

输入：既有 `CUBE_ROOT`、API URL、用户选定的 READY Template、NUMA 节点、TAP 目标、输出目录，
以及最终确认的测试窗口。默认使用共享 `default-plan.json`；`--plan` 可传完整计划或仅含
`rollback` 的计划，支持参数为 `cases[].concurrency/rounds`、章节级 `dirty_mb`、`settle_secs`。未知字段和非安全参数仍会失败。

## 可执行入口

```bash
SKILL_DIR=/path/to/cubesandbox-rollback-bench
CUBE_ROOT=/path/to/CubeSandbox

# 成功路径的安全预览：只展开本场景，不访问 API、不修改系统。
bash "$SKILL_DIR/scripts/run.sh" --repo "$CUBE_ROOT" --dry-run

# 展示本场景最终计划和批准哈希；自定义计划时在两条命令中都加同一个 --plan。
bash "$SKILL_DIR/scripts/run.sh" --show-plan \
  --repo "$CUBE_ROOT" --template-id tpl-xxx --numa-node 0 --tap-target 500

# 用户确认后正式执行；APPROVAL_SHA256 取自上一命令。
bash "$SKILL_DIR/scripts/run.sh" \
  --repo "$CUBE_ROOT" --template-id tpl-xxx --numa-node 0 --tap-target 500 \
  --output-root /path/results --yes --exclusive-confirmed \
  --approved-plan-hash "$APPROVAL_SHA256"
```

正式参数必须一致；需要同时准备时可显式添加 `--prepare --tap-evidence <证据>`并重新生成哈希。
入口固定追加 `--sections rollback`，用户传 `--sections` 会被拒绝，避免越界跑其他场景。

## 输出与成功判据

正式执行写入 `<output-root>/<时间戳>_<pid>/`：`preflight.json`、`metadata.json`、`results.json`、
`report.md`、冒烟及 `rollback/` 原始日志（Template Create 为 `template-create/`）。只有
`metadata.sections == ["rollback"]`、预检和真实冒烟通过、全部所选档位记录成功、差集清理完成且
`results.json.ok=true` 才是成功。所有并发档均退出 0 且汇总无问题；每个沙箱只能回滚自身 checkpoint，跨沙箱 checkpoint 误用不属于本入口支持范围。

dry-run 只证明计划可展开，不是性能结果，也不生成上述正式结果目录。

## 受阻与边界

缺模板/NUMA/批准哈希、预检 FAIL、锁冲突、冒烟或任一档失败均非零退出并停止后续工作；保留证据，
不得改跑其他章节或降低安全参数“试试看”。`--prepare` 的脚本改写、TAP 清理和服务重启仍需单独明确授权。
共同安全语义见 [预检与准备](../cubesandbox-lifecycle-bench/references/preflight-and-prepare.md)、
[结果契约](../cubesandbox-lifecycle-bench/references/result-contract.md)；测量纪律继续复用
`cubesandbox-bench-protocol`，本 Skill 不实现新的分析器。

## 自检

```bash
bash "$SKILL_DIR/cases/skill-contract/run.sh"
```

该 Tier-A 用例使用 dry-run 和契约反例，不访问 CubeSandbox 服务、不压测、不改宿主机。
