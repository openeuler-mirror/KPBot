---
name: remote-runner
description: 受限 SSH 远端构建/测试执行。当 code-optimizer 检测到项目根存在 .kpbot/remote/config.json，或需要把源码同步到只能 SSH 直连、不能部署 Agent 的远端执行 build/test/bench/collect 时使用；不用于本地可直接完成的编译测试。
---

# KPBot 远端执行（remote-runner）

本 skill 是 `kpbot-code-optimizer` 的子 skill。code-optimizer 及其子 skill 不直接调用脚本，而是：

```text
Skill({ skill: "remote-runner", args: "<action> [--project <path>]" })
```

本 skill 负责**流程与判定**；`scripts/remote_runner.py` 只提供安全原语（配置校验、probe、原子同步、执行、pull）。
不要在 skill 之外手工拼远端 ssh 命令，也不要把脚本路径复制到 code-optimizer 文档中。

## 0. 角色边界

- **Skill 决定**：何时配置、是否继续、重试还是停止、问用户还是 blocked、证据如何映射。
- **脚本负责**：字符/路径校验、凭证排除、SSH/SCP/rsync、原子发布、同步锁、超时、日志与 meta、realpath/大小校验。
- 脚本输出 JSON；本 skill 读取 JSON 后做判断。脚本失败时按错误码路由，不凭猜测继续。

## 1. 何时使用

- 目标机只能 SSH 直连，不能安装 Agent 或访问 API。
- 开发机与目标机架构不同（通常本地 x86、远端 aarch64），本地结果不能作为性能证据。
- 本地能直接编译测试且不需要目标机数据时，**不要用**。

## 2. 前置条件

- 本地：Python >= 3.8、`ssh`、`scp`；`rsync` 可选。
- 远端：`bash`、可写的专用 workspace（建议 ≥100GB）；无 rsync 时需要 `tar`；`pull` 需要 `realpath`；脚本超时清理建议有 `timeout`。
- SSH 免密：`ssh -o BatchMode=yes <user>@<host> exit 0` 必须成功。
- 主机指纹：使用 `StrictHostKeyChecking=accept-new`。`probe` 返回已知指纹；强制 pin 需预置 `known_hosts`。

## 3. 状态机

```text
无配置
  └─ 询问用户 host/user/port/workspace
       └─ config
            └─ probe ──失败/关键缺失──> 停止或询问用户
                 └─ sync ──凭证/失败──> 按策略处理
                      └─ build/test/bench/collect
                           └─ 读取 meta/stdout/stderr
                                └─ pull（可选）
```

任何一步失败都不要跳步或改用本地结果。

命令与判定表随实现提交补齐。
