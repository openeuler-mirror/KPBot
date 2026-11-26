---
name: remote-runner
description: 受限 SSH 远端构建/测试执行。当 code-optimizer 检测到项目根存在 .kpbot/remote/config.json，或需要把源码同步到只能 SSH 直连、不能部署 Agent 的远端执行 build/test/bench/collect 时使用；不用于本地可直接完成的编译测试。
---

# KPBot 远端执行（remote-runner）

本 skill 是 `kpbot-code-optimizer` 的子 skill。code-optimizer 及其子 skill 不直接调用脚本，而是：

```text
Skill({ skill: "remote-runner", args: "<action> [--project <path>]" })
```

本 skill 负责**流程与判定**；当前脚本提供配置校验与 probe 原语，同步/执行原语随后续变更补齐。
不要在 skill 之外手工拼远端 ssh 命令，也不要把脚本路径复制到 code-optimizer 文档中。

## 0. 角色边界

- **Skill 决定**：何时配置、是否继续、重试还是停止、问用户还是 blocked、证据如何映射。
- **脚本负责**：字符/路径校验、SSH 连接、probe 与超时。
- 脚本输出 JSON；本 skill 读取 JSON 后做判断。脚本失败时按错误码路由，不凭猜测继续。
- `--help` / `--version` 是文本输出；参数错误和业务错误都输出 JSON。

## 0.1 退出码

| code | 含义 | 动作 |
| --- | --- | --- |
| 0 | 成功 | 继续下一步 |
| 2 | 配置已存在 | 用户确认后加 `--force` 覆盖 |
| 3 | 本地 I/O 或内部错误 | 停止并检查本机环境 |
| 4 | SSH 或远端操作失败 | 停止，原样反馈错误 |
| 5 | 配置不存在或配置无效 | 先运行 `config` |
| 64 | CLI 参数或输入非法 | 修正参数后重试 |
| 130 | 用户中断 | 停止 |

## 1. 何时使用

- 目标机只能 SSH 直连，不能安装 Agent 或访问 API。
- 开发机与目标机架构不同（通常本地 x86、远端 aarch64），本地结果不能作为性能证据。
- 本地能直接编译测试且不需要目标机数据时，**不要用**。

## 2. 前置条件

- 本地：Python >= 3.8、`ssh`、`scp`；`rsync` 可选。
- 远端：`bash`、可写的专用 workspace（建议 ≥100GB）；无 rsync 时需要 `tar`；脚本超时清理建议有 `timeout`。
- SSH 免密：`ssh -o BatchMode=yes <user>@<host> exit 0` 必须成功。
- 主机指纹：使用 `StrictHostKeyChecking=accept-new`。`probe` 返回已知指纹；强制 pin 需预置 `known_hosts`。

## 3. 状态机

```text
无配置
  └─ 询问用户 host/user/port/workspace
       └─ config
            └─ probe ──失败/关键缺失──> 停止或询问用户
```

当前提交实现 `config` / `probe`；sync、build/test/bench/collect 随后续变更补齐。任何一步失败都不要跳步或改用本地结果。

## 4. 首次配置（config）

先问用户，不得默认：

1. 远端 host、user、port。
2. KBot-workspace 绝对路径；建议可用空间 ≥100GB（不是硬门槛，按项目规模判断）。
3. 普通用户是否需要 sudo 免密；`root` 不需要。
4. workspace 不存在时，是否允许创建（`--create-workspace`）。

然后调用：

```text
Skill({ skill: "remote-runner", args: "config --project <path> --host ... --user ... --port ... --workspace ... [--create-workspace]" })
```

`config` 会校验字符/路径并写 `.kpbot/remote/config.json`，同时确保 git 工作区的 `.gitignore` 包含 `.kpbot/`。配置不包含密码。`--create-workspace` 只对新建目录执行 `chmod 700`，不会修改已存在目录的权限；workspace 不能等于远端 `$HOME`。修改配置后会清除旧同步标记，后续执行前必须重新 sync。workspace 会拒绝 `/etc`、`/usr`、`/var`、`/bin`、`/root` 等系统目录；`/mnt`、`/opt`、`/srv` 等数据盘只允许使用专用子目录。

## 5. probe 与判定表

```text
Skill({ skill: "remote-runner", args: "probe --project <path>" })
```

`probe` 返回事实，不返回“通过/失败”结论：

```json
{
  "ok": true,
  "ssh": true,
  "arch": "aarch64",
  "hostname": "node-01",
  "home": "/home/runner",
  "bash": true,
  "rsync": false,
  "tar": true,
  "timeout": true,
  "workspace": {"path": "/ssd/KBot-workspace", "exists": true, "writable": true, "free_gb": 220},
  "fingerprints": ["..."]
}
```

| 事实 | 判定 | 动作 |
| --- | --- | --- |
| `ssh=false` | 免密/网络失败 | 停止，原样给用户 ssh 错误 |
| `bash=false` | 无法执行脚本 | 停止，要求安装 bash |
| `workspace.exists=false` | 专用目录未创建 | 询问用户后 `config --create-workspace` |
| `workspace.writable=false` | 不可写 | 停止，换目录或修权限 |
| `workspace.path == home` | 使用主目录 | 停止，要求专用目录 |
| `workspace.free_gb < 10` | 空间不足 | 停止；`<100` 仅警告 |
| `rsync=false && tar=false` | 无法同步 | 停止，要求安装 rsync 或 tar |
| `timeout=false` | 超时后进程可能残留 | 警告；建议安装 coreutils |
| `fingerprints` 为空 | 无法确认主机 | 警告，要求人工核对 |
| `arch` 与预期不符 | 可能不是目标机 | 停止并让用户确认 |

`probe` 返回 `ok=false` 时，不得继续后续步骤；先按 `error` 修环境。
