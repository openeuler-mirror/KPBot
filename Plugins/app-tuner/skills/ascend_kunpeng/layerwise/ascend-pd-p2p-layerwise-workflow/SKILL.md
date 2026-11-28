---
name: ascend-pd-p2p-layerwise-workflow
description: 编排昇腾 vLLM-Ascend PD 分离 P2P Layerwise 的一键适配流程，把准入、配置、验证和排障串成有停止条件的阶段。用于用户明确要求端到端适配、跨多个阶段协调，或需要一份统一的 verdict/evidence/blockers 报告；只做单个阶段时使用对应的 admission/config/verification/troubleshooting skill。作为 ascend_kunpeng/layerwise 子域使用。
compatibility: Requires the four sibling ascend-pd-p2p-layerwise-* capability skills and their runtime prerequisites.
---

# P2P Layerwise 一键适配编排

## 何时触发

- 用户要求“从零开始适配 P2P layerwise”。
- 需要把准入、配置、验证、排障串成一个流程。
- 需要统一输出 verdict、evidence、blockers、risks 和 next_action。
- 需要在 no-go 或验证失败时明确停止，而不是继续套配置。

## 不进入本 skill

- 只问“能不能开” → `ascend-pd-p2p-layerwise-admission`。
- 只问“配置怎么写” → `ascend-pd-p2p-layerwise-config`。
- 只问“怎么验证” → `ascend-pd-p2p-layerwise-verification`。
- 只贴报错 → `ascend-pd-p2p-layerwise-troubleshooting`。

## 下游 Skill

本 workflow 只负责编排，不复制四个能力 skill 的细节。执行前必须由宿主原生 Skill 工具加载对应入口；加载失败则阻塞，不凭目录推断。

- `ascend-pd-p2p-layerwise-admission`
- `ascend-pd-p2p-layerwise-config`
- `ascend-pd-p2p-layerwise-verification`
- `ascend-pd-p2p-layerwise-troubleshooting`

## 输入

- 目标：公开版 P2P Layerwise Mode A；Store layerwise / host offload 超出本 skill 范围。
- 模型/KV cache 事实：`kv_caches.keys()`、group 数、draft layer 数、attention 后端。
- checkout/PR 信息（版本门禁由 admission 执行）。
- 现场配置路径、服务健康地址、日志路径、允许的失败注入方式。
- 是否授权修改配置和重启服务。

## Workflow

### Step 0：输入与授权确认

确认目标、现场事实、配置路径、验证入口和修改/重启授权；不在本阶段重复执行版本门禁。

### Step 1：Admission

加载 admission skill，执行版本门禁和 G1-G6。

- 输出 `no-go`：停止，交 `code-optimizer` 或回退非 layerwise。
- 输出 `mode-a`：进入 Step 2。

### Step 2：Config

加载 config skill，生成或校验 `--kv-transfer-config`。

- 静态检查失败：回到 config 修复，不启动。
- 配置超出公开 Mode A 范围：停止或交 `code-optimizer`。

### Step 3：Apply

仅在用户明确授权时修改配置并重启服务。

- 先备份原配置、启动脚本、proxy 配置。
- 记录版本、端口、engine_id 和回退命令。
- 未授权时只输出 diff/计划，不执行。

### Step 4：Verification

加载 verification skill。

- 健康检查。
- 唯一 prompt 触发。
- block 映射一致性。
- 精度对比。
- 失败注入和 recompute 检查。

### Step 5：Troubleshooting Loop

验证失败时加载 troubleshooting skill。

- 先判断是配置、ownership、block mapping、版本门禁还是源码限制。
- 能配置修复的回 Step 2；需要源码修改的停止并交 `code-optimizer`。
- 不允许静默跳过缺块或把失败请求当成功。

### Step 6：Closeout

输出一份统一报告：

```json
{
  "verdict": "no-go|mode-a",
  "config_status": "valid|invalid|not-run",
  "layerwise_triggered": "yes|no|unknown",
  "block_mapping": "consistent|mismatch|unknown",
  "precision": "pass|fail|not-run",
  "failure_injection": "pass|fail|not-run",
  "evidence": [],
  "blockers": [],
  "risks": [],
  "next_action": ""
}
```

## 停止条件

- admission 给出 no-go。
- config 无法通过静态检查。
- 用户未授权修改配置或重启服务。
- block 映射不一致且无法在不改源码的情况下收敛。
- 精度验证失败且找不到配置级原因。
- 需要多 draft layer、多 entry connector、planner 变更等源码改动。

## 输出

最终只给一份阶段化报告，明确区分：

- 已完成；
- 已跳过；
- 阻塞；
- 需要 `code-optimizer`；
- 回退命令；
- 版本与证据路径。

## 边界

本 workflow 不实现技术细节、不修改源码、不写死机型/拓扑/测试数据。单阶段请求直接路由到对应 skill。
