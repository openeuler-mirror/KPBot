---
name: cubesandbox-lifecycle-bench
description: >
  CubeSandbox 核心生命周期性能的一键 Workflow 与路由入口：在已部署 Linux 主机上统一完成
  show-plan、只读预检、可选 NUMA/TAP 准备、冒烟和 Template create、density、Snapshot、
  dirty_page、Snapshot create、Rollback、Clone、Pause/Resume 全矩阵，归档报告并做差集清理。
  单项任务路由到十个聚焦 Skill；执行前仍须确认参数、独占窗口和全部副作用，不用于部署或未授权压测。
license: Apache-2.0
compatibility: Linux；CubeSandbox 官方 examples/cube-bench 与 snapshot-rollback-clone 脚本
metadata:
  skill_type: tool_assisted
  category: benchmark
  domain: cubesandbox
  version: "1.1.0"
---

# CubeSandbox 生命周期性能一键 Workflow / 路由

本入口保留原有一键全矩阵能力，也是十个聚焦 Skill 的**唯一共享执行器**。测量真源仍是官方
`cube-bench` 和六个 `bench_*.py`；计划校验、哈希、锁、冒烟、清理和结果汇总只在 `scripts/` 中实现一次。
测量纪律与 Create 判读继续复用 `cubesandbox-bench-protocol`，不要另造分析器。

## 单点任务路由（10 个）

| Skill | 固定能力 | 主要证据 |
|---|---|---|
| [cubesandbox-bench-preflight](../cubesandbox-bench-preflight/SKILL.md) | 只读 `check` | `preflight.json` |
| [cubesandbox-bench-prepare](../cubesandbox-bench-prepare/SKILL.md) | 独立 NUMA/TAP prepare 与 readiness | `prepare.json`、复验 `preflight.json` |
| [cubesandbox-template-create-bench](../cubesandbox-template-create-bench/SKILL.md) | `template_create` / `3.2` | Create 原始 JSON、`create-verdict.json` |
| [cubesandbox-density-bench](../cubesandbox-density-bench/SKILL.md) | `density` / `3.3` | density JSON、`density.csv`、排空证据 |
| [cubesandbox-snapshot-bench](../cubesandbox-snapshot-bench/SKILL.md) | `snapshot` / `4.1` | Snapshot 并发日志与汇总 |
| [cubesandbox-dirty-page-bench](../cubesandbox-dirty-page-bench/SKILL.md) | `dirty_page` / `4.2` | 各 dirty MiB 档日志与汇总 |
| [cubesandbox-snapshot-create-bench](../cubesandbox-snapshot-create-bench/SKILL.md) | `snapshot_create` / `4.3` | Snapshot 恢复并发日志与汇总 |
| [cubesandbox-rollback-bench](../cubesandbox-rollback-bench/SKILL.md) | `rollback` / `4.4` | Rollback 日志与汇总 |
| [cubesandbox-clone-bench](../cubesandbox-clone-bench/SKILL.md) | `clone` / `4.5` | Clone 日志与汇总 |
| [cubesandbox-pause-resume-bench](../cubesandbox-pause-resume-bench/SKILL.md) | `pause_resume` / `4.6` | Pause/Resume 日志与汇总 |

聚焦入口的 `scripts/run.sh` 解析到安装后的共享 `cubesandbox_skill.py`；场景入口固定 section，
可用 `--plan` 调整该场景已支持的参数，拒绝用 `--sections` 越界。`--show-plan` 是聚焦入口生成
对应批准哈希的选项，不是额外 Skill。八个单场景预览始终固定 `operation=run`，显式 `--operation run`
（含等号形式）会规范化为 run，`prepare` 值会在任何输出前拒绝；`--prepare` 仍表示 run 前准备。独立
prepare 入口继续固定 prepare-only。show-plan、授权哈希、冒烟和差集清理均不单独技能化。

## 不可跳过的安全门

1. 先只读检查，再向用户展示最终 `show-plan`，最后执行；测试章节、每档参数、模板、源码/API、
   NUMA/TAP、输出目录、独占窗口与清理/重启副作用均需明确确认。
2. 正式 run/prepare 必须显式模板和 NUMA，并携带 `--yes --exclusive-confirmed` 与匹配的
   `--approved-plan-hash`。哈希覆盖计划、实际 sections、run/prepare-only、prepare 开关和全部安全参数；
   任何变化都重新确认。prepare-only 哈希不能授权 run。
3. 不自动停止其他业务，不默认全量删除沙箱，不强杀 shim。准备只在 API 严格证明空沙箱且 shim=0、
   TAP 归属和精确证据集合均未漂移时清理；任何 FAIL 都不能“先跑一把看看”。
4. `/run/lock/cubesandbox-lifecycle-bench.lock` 的非阻塞排他锁覆盖准备、测试和最终清理；锁冲突直接失败。
5. 正式 run 必须先通过真实 create/run_code/delete 冒烟。任何档位失败会停止后续测试并保留证据。

完整安全约束见 [预检与准备](references/preflight-and-prepare.md)。

## 兼容的一键全矩阵

以下 `SKILL_DIR` 指本 Skill 根目录，`CUBE_ROOT` 是与实际部署版本一致的 CubeSandbox 源码树。
原脚本路径、子命令和默认 29 档矩阵保持不变。

### 1. 预览与只读检查

```bash
python3 "$SKILL_DIR/scripts/cubesandbox_bench.py" show-plan   --repo "$CUBE_ROOT" --template-id tpl-xxx   --api-url http://127.0.0.1:3000 --numa-node 0 --tap-target 500

# 自定义时复制 assets/default-plan.json 并传 --plan；dry-run 不访问环境。
python3 "$SKILL_DIR/scripts/cubesandbox_bench.py" run   --repo "$CUBE_ROOT" --plan /path/plan.json --dry-run

python3 "$SKILL_DIR/scripts/cubesandbox_bench.py" check   --repo "$CUBE_ROOT" --template-id tpl-xxx   --api-url http://127.0.0.1:3000 --numa-node 0 --tap-target 500   --json --output /path/preflight.json
```

`check --output` 是新增的可选落盘方式；省略时保持原行为。准备前从检查证据读取
`context.tap_evidence_sha256`，再生成包含 prepare 副作用的**最终**批准范围：

```bash
python3 "$SKILL_DIR/scripts/cubesandbox_bench.py" show-plan   --repo "$CUBE_ROOT" --template-id tpl-xxx   --api-url http://127.0.0.1:3000 --numa-node 0 --tap-target 500   --prepare --tap-evidence "$TAP_EVIDENCE_SHA256"
```

不需要 prepare 时，用相同环境参数重新 show-plan（不带 prepare/tap-evidence）。

### 2. 用户确认后一次执行

```bash
python3 "$SKILL_DIR/scripts/cubesandbox_bench.py" run   --repo "$CUBE_ROOT" --template-id tpl-xxx   --api-url http://127.0.0.1:3000 --numa-node 0 --tap-target 500   --output-root /path/results --prepare --tap-evidence "$TAP_EVIDENCE_SHA256"   --yes --exclusive-confirmed --approved-plan-hash "$APPROVAL_SHA256"
```

不需要准备时去掉 `--prepare/--tap-evidence` 并使用对应哈希。仍可用
`--sections create,density,snapshot,dirty_page,snapshot_create,rollback,clone,pause_resume`
或 `3.2,3.3,4.1,...` 选择子集；不传时为全矩阵。独立准备仍使用原 `prepare` 子命令。

- `--prepare` 幂等修正 NUMA 启动行并备份，只清理批准证据中的 Cube TAP；不足时通过重启 cubelet
  重建，等待 active、TAP、运行 NUMA 和 API health 全部就绪。
- 普通档位按 API 沙箱 ID 差集清理；density 整章结束后排空再进入下一章。Snapshot/template 不在
  沙箱差集内，异常时必须提示用户审核，不得声称全部资源已清理。
- 输出 `report.md`、`results.json`、`preflight.json` 和原始日志；选择 Create 时还必须成功生成
  `create-verdict.json`。只有 `results.json.ok=true` 才是完整通过，dry-run 不是性能结果。

## 参数与结果契约

- [测试矩阵、29 档展开与参数语义](references/test-matrix.md)
- [预检、NUMA/TAP、资源独占与故障处理](references/preflight-and-prepare.md)
- [结果目录、成功判据与清理边界](references/result-contract.md)

## 自检

```bash
bash "$SKILL_DIR/cases/bench-contract/run.sh"
for focused in cubesandbox-bench-{preflight,prepare}   cubesandbox-{template-create,density,snapshot,dirty-page,snapshot-create,rollback,clone,pause-resume}-bench; do
  bash "$SKILL_DIR/../$focused/cases/skill-contract/run.sh"
done
```

全部 Tier-A 用例使用 mock/dry-run，不访问真实 CubeSandbox 服务、不压测、不改宿主机配置。
