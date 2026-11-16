---
name: cubesandbox-lifecycle-bench
description: >
  在已部署 CubeSandbox 的 Linux 主机上，一键预检、准备并执行核心生命周期性能测试：
  Template create、单机 density、Snapshot 并发、dirty_page、Snapshot create、Rollback、
  Clone、Pause/Resume。适用于复现 CubeSandbox PVM 性能指导、调整并发/请求数/轮数/脏页
  矩阵、归档原始报告。执行前必须向用户确认测试目标、参数、独占窗口和清理/重启副作用；
  不用于部署 CubeSandbox，也不得在有未授权沙箱或其他业务负载时直接开测。
license: Apache-2.0
compatibility: Linux；CubeSandbox 官方 examples/cube-bench 与 snapshot-rollback-clone 脚本
metadata:
  skill_type: tool_assisted
  category: benchmark
  domain: cubesandbox
  version: "1.0.0"
---

# CubeSandbox 生命周期性能一键测试

使用官方 `cube-bench` 和六个 `bench_*.py` 作为测量真源；本 skill 负责计划确认、预检、
NUMA/TAP/代理准备、长等待、逐档执行、差集清理和证据归档。测量纪律复用
`cubesandbox-bench-protocol`，不要另创与之冲突的口径。

## 不可跳过的安全门

1. **先只读检查，后请求确认，最后执行。** 不得因为用户说“一键测试”就省略参数确认。
2. 向用户展示 `show-plan` 输出，并明确确认：
   - 测试章节；每档并发数、请求数/Clone 数、轮数、warmup、dirty MiB；
   - density 累计存活数、`max_live`、内存安全水位；
   - CubeSandbox 源码路径、API、Template ID、结果目录；
   - NUMA 节点（多节点机器不能盲选）、TAP 目标（默认 500）及 check 给出的精确待删集合证据哈希；
   - 独占窗口、其他任务已停止、允许创建/删除本次测试资源；
   - 若使用 `--prepare`，还需明确允许改两个启动脚本、清理超额 TAP、重启 cubemaster/cubelet。
3. 正式执行必须显式指定模板和 NUMA，并同时携带
   `--yes --exclusive-confirmed --approved-plan-hash <APPROVAL_SHA256>`。批准哈希覆盖最终计划、
   实际章节、run/prepare-only、prepare 开关、源码/API/模板、NUMA/TAP 和安全参数；任一变化后
   必须用相同参数重新 `show-plan`、重新确认，禁止复用旧哈希。prepare-only 哈希不能授权 run。
4. 不自动停止/杀死其他业务，不默认全量删除既有沙箱，不强杀 shim。预检发现这些情况时
   停止并请用户处理。`--prepare` 只有在 API 证明沙箱为空且 shim 为 0 时才清 TAP/重启。
5. 不在当前异常环境中“先跑一把看看”。服务/API/模板/资源/NUMA/TAP 任一 FAIL 均不得开测。

## 一键工作流

以下 `SKILL_DIR` 指本 skill 根目录，`CUBE_ROOT` 是既有 CubeSandbox 源码树。优先使用与
实际部署版本一致的源码；不要把目录名当作版本证据，记录 Git commit。

### 1. 展示默认计划（无副作用）

```bash
# 先预览矩阵；需要 prepare 时，最终批准哈希还要等待第 2 步的 TAP 证据。
python3 "$SKILL_DIR/scripts/cubesandbox_bench.py" show-plan \
  --repo "$CUBE_ROOT" \
  --template-id tpl-xxx \
  --api-url http://127.0.0.1:3000 \
  --numa-node 0 --tap-target 500
```

自定义时复制 `assets/default-plan.json`，修改后用 `--plan /path/plan.json`。可先展开命令：

```bash
python3 "$SKILL_DIR/scripts/cubesandbox_bench.py" run \
  --repo "$CUBE_ROOT" --plan /path/plan.json --dry-run
```

dry-run 不访问 API、不修改系统，也不是性能结果。

### 2. 只读预检

```bash
python3 "$SKILL_DIR/scripts/cubesandbox_bench.py" check \
  --repo "$CUBE_ROOT" \
  --template-id tpl-xxx \
  --api-url http://127.0.0.1:3000 \
  --numa-node 0 --tap-target 500
```

需要准备时，从检查输出读取 `TAP_EVIDENCE_SHA256`（对应精确超额 `(ifindex,name)` 集合），
再生成**最终**批准范围并向用户确认：

```bash
python3 "$SKILL_DIR/scripts/cubesandbox_bench.py" show-plan \
  --repo "$CUBE_ROOT" --template-id tpl-xxx \
  --api-url http://127.0.0.1:3000 \
  --numa-node 0 --tap-target 500 \
  --prepare --tap-evidence <TAP_EVIDENCE_SHA256>
```

不准备时，用相同环境参数重新 `show-plan`（不带 prepare/tap-evidence）即可。多 READY
模板时必须由用户选定，不得自动猜。检查项包括服务、API、模板全局/节点副本
READY、沙箱/shim 残留、非 Cube 高 CPU 进程、load、内存/磁盘、NUMA 脚本与运行进程亲和性、
TAP 数量和 SDK。代理变量由执行器自动 unset，不要求用户修改其交互 shell。

### 3. 用户确认后一次执行

```bash
python3 "$SKILL_DIR/scripts/cubesandbox_bench.py" run \
  --repo "$CUBE_ROOT" \
  --template-id tpl-xxx \
  --api-url http://127.0.0.1:3000 \
  --numa-node 0 --tap-target 500 \
  --output-root /path/results \
  --prepare --tap-evidence <TAP_EVIDENCE_SHA256> \
  --yes --exclusive-confirmed \
  --approved-plan-hash <本次show-plan输出的APPROVAL_SHA256>
```

- 不需要准备时同时去掉 `--prepare` 和 `--tap-evidence`，并使用对应的最终批准哈希；所有预检仍必须通过。
- 只测部分章节：加 `--sections create,snapshot,dirty_page,snapshot_create,rollback,clone,pause_resume`
  的所需子集；也支持 `3.2,4.1` 编号。density 用 `density`/`3.3`。
- `--prepare` 会幂等修正 NUMA 启动行并备份；TAP 超额时只匹配接口名 `192.168.x.x`，
  清理后重启 cubelet；不足时也通过重启重建。cubelet 默认最长等待 1800s，确认
  `active + TAP=500 + NUMA 正确 + API health` 后才继续。
- 正式运行先做真实 create/run_code/delete 冒烟，再逐档串行执行。`/run/lock/cubesandbox-lifecycle-bench.lock`
  的排他 `flock` 覆盖 prepare、测试和清理；锁被占用时直接退出，不得并行启动多个实例。
- API 未知结构、缺 ID、分页未完整或计数不一致均按“无法证明为空”处理，不能继续。

### 4. 判读与交付

读取结果目录的 `report.md`、`results.json`、`preflight.json` 和原始日志。Create 场景还会
调用 `cubesandbox-bench-protocol` 的分析器生成 `create-verdict.json`。报告必须包含：

- 主机、内核、源码 commit、模板、最终计划哈希、批准范围哈希、实际章节和参数；
- 成功率/错误数、create avg/P95/P99、各生命周期 wall/per 操作；
- density 每批 MemAvailable 与均摊变化；
- 准备动作、cubelet 等待、清理失败、Snapshot/template 残留和任何部分失败；
- 结果目录。不得把 dry-run 或其他机器/其他纪元的博客数值当成本次结果。

任何档位失败即停止后续测试并保留证据。清理只针对相对基线新增的 API 沙箱 ID；
Snapshot/template 不在该差集内，异常时必须提醒用户审核，不得谎报已全部清理。

## 参数与细节

- [测试矩阵与指标口径](references/test-matrix.md)
- [预检、NUMA/TAP、资源独占与故障处理](references/preflight-and-prepare.md)
- [结果目录与验证](references/result-contract.md)

## 自检

```bash
bash "$SKILL_DIR/cases/bench-contract/run.sh"
```

该 Tier-A 用例不需要 CubeSandbox 服务，覆盖计划校验、29 档命令展开、批准门控、代理清理、
NUMA 幂等改写、TAP 锚定匹配和 dry-run。它不能替代真实环境预检与性能测试。
