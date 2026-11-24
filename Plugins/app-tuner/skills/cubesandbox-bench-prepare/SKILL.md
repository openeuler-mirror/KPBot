---
name: cubesandbox-bench-prepare
description: >
  当用户已明确授权 CubeSandbox 基准前的独立 NUMA/TAP 准备时使用。只执行 prepare、写 prepare.json/readiness 并复验，不运行冒烟或任何性能章节；无批准哈希、TAP 证据漂移或沙箱/shim 非空时受阻。
license: Apache-2.0
compatibility: Linux；已部署 CubeSandbox；共享 cubesandbox-lifecycle-bench 执行器
metadata:
  skill_type: tool_assisted
  category: benchmark
  domain: cubesandbox
  version: "1.0.0"
---

# CubeSandbox Bench 独立准备

## 唯一职责与输入

只调用共享执行器的独立 `prepare`：幂等绑定两个 NUMA 启动行、按精确证据处理超额 TAP、按需重启并等待
cubelet 就绪、必要时构建官方 cube-bench，然后完整复验；不运行基准。输入为 `CUBE_ROOT`、toolbox、API、
显式 READY Template、显式 NUMA、TAP 目标、check 给出的 `tap_evidence_sha256`、独占窗口与输出目录。

## 可执行入口

```bash
SKILL_DIR=/path/to/cubesandbox-bench-prepare
CUBE_ROOT=/path/to/CubeSandbox

# 无副作用地生成 prepare-only 批准范围；不能用 run 的哈希替代。
bash "$SKILL_DIR/scripts/run.sh" --show-plan \
  --repo "$CUBE_ROOT" --template-id tpl-xxx --numa-node 0 --tap-target 500 \
  --tap-evidence "$TAP_EVIDENCE_SHA256"

# 用户确认准备副作用后才执行。
bash "$SKILL_DIR/scripts/run.sh" \
  --repo "$CUBE_ROOT" --template-id tpl-xxx --numa-node 0 --tap-target 500 \
  --tap-evidence "$TAP_EVIDENCE_SHA256" --output-root /path/results \
  --yes --exclusive-confirmed --approved-plan-hash "$APPROVAL_SHA256"
```

相同参数变化（包括计划、章节、源码/API、NUMA/TAP 或证据）必须重新 `--show-plan` 和确认。
`--operation` 被固定为 prepare，不能借本入口改成 run。

## 输出与成功判据

写入 `<output-root>/prepare_<时间戳>_<pid>/prepare.json`、备份（如有）和复验 `preflight.json`。
只有 `prepare.json.status=completed`、`result.readiness` 记录 TAP/NUMA 就绪证据（需要重启时还包括
cubelet active 与 API health），并且复验 `preflight.json.ok=true`、退出 0 才成功。就绪等待不是性能测量。

## 受阻与边界

缺少 `--yes/--exclusive-confirmed`、批准哈希不匹配，以及参数/模板解析或宿主锁获取等进入准备状态机前
的失败，都在创建 `prepare_*` 目录前退出；此时没有 `prepare.json`，调用方必须按非零退出码和 stderr 判定，
不应等待或伪造 journal。只有本次运行目录已创建并进入准备状态机后，API 无法严格证明空沙箱、shim 非空、
CIDR/TAP 归属不明、证据集合漂移、NUMA 非法、构建或 readiness 超时等准备动作失败，才必须保留
`prepare.json.status=failed` 和 `actions` action journal，调用方再读取该失败 journal。不会在证据不足时清 TAP
或强杀进程，也不会继续冒烟/压测。完整安全约束见
[共享准备说明](../cubesandbox-lifecycle-bench/references/preflight-and-prepare.md)。

## 自检

```bash
bash "$SKILL_DIR/cases/skill-contract/run.sh"
```

自检通过 mock 驱动真实 prepare 状态机和 journal，不执行 systemd、ip、make 或真实基准命令。
