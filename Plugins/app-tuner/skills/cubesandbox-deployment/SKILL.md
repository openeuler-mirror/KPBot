---
name: cubesandbox-deployment
description: >
  在有网络、有 Agent 的 x86_64 或 aarch64 Linux 主机上，一键在线部署单机
  CubeSandbox（Cube Sandbox）all-in-one 环境。适用于“安装/部署 CubeSandbox”、
  “配置 CubeSandbox 单机环境”、“选择 amd64/arm64 安装包”、“CubeSandbox 部署预检和
  安装故障处理”。自动选择官方架构匹配资产、优先校验 Release SHA-256、执行官方
  安装器并保留服务健康 quickcheck；涉及节点 IP、PVM、网络 CIDR、代理或私有镜像变量时先询问
  用户。不适用于离线部署、多机集群、PVM 内核无人值守安装、模板创建、沙箱业务测试、
  cube-bench 或 Pause/Resume 性能测试（基准测试使用 cubesandbox-bench-protocol）。
license: Proprietary
compatibility: Linux x86_64/aarch64; root; systemd; native KVM or x86_64 PVM; online access
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: cubesandbox
  version: "1.0.0"
---

# CubeSandbox 单机在线部署

目标是在用户确认的 Linux 目标机上完成 **单机 all-in-one 部署**，而不是生成一份泛化的
安装说明。优先使用 `scripts/deploy.sh` 自动完成，不重复手写官方组件安装流程。

## 不可违反的边界

1. **只部署，不做业务/性能测试**：不创建模板或真实沙箱，不运行 cube-bench、
   Pause/Resume、E2B Hello World。官方服务健康 quickcheck 必须保留。
2. **不越过硬前置条件**：不使用 `ONE_CLICK_SKIP_PRECHECK=1`、
   `ONE_CLICK_SKIP_PVM_CHECK=1`，不关闭 quickcheck，不把服务曾启动当作成功。
3. **不破坏已有环境**：本技能只支持全新安装。检测到完整、部分或无法识别的已有安装均
   停止；升级/恢复只能在用户明确授权后按官方手册另行处理。不得擅自停止业务进程、替换
   Docker、删除容器或数据。
4. **危险主机操作必须单独授权**：不得自动格式化磁盘、安装 PVM 宿主内核、修改 GRUB
   或重启。需要这些操作时说明原因、影响及官方步骤，取得确认后单独处理，再恢复本流程。
5. **秘密信息不入库**：密码、Token、私钥和带凭据代理 URL 不写入 skill、仓库、日志或
   shell 历史。用户环境文件放在技能目录外并限制权限，报告中不打印秘密。

## 执行工作流

### 1. 确定目标与授权

确认部署对象是 Agent 所在机器还是 SSH 目标机；后续命令必须在**目标机**执行。确认：

- 单机 all-in-one、root、有网络，用户允许官方安装器修改 Docker、systemd、DNS、iptables、
  `/data/cubelet` 和 `/usr/local/services/cubetoolbox`；
- 是否已有 CubeSandbox、重要容器或业务端口，是否有维护窗口与备份；
- 默认仅部署。用户若同时要求模板/测试，将其拆成部署后的独立任务，不在本技能中顺带执行。

### 2. 先运行只读预检

定位本技能目录后执行：

```bash
bash scripts/deploy.sh --preflight-only
```

如用户已经提供环境文件：

```bash
bash scripts/deploy.sh --env /secure/path/deployment.env --preflight-only
```

预检解析当前主机的 `amd64/arm64` 官方 Release，不下载大包、不修改配置。阅读完整输出，
不能仅看退出码。

### 3. 只询问必要变量

以 `deployment.env.example` 为模板，在用户工作目录或安全临时目录生成环境文件。变量细节见
[references/deployment.md](references/deployment.md)。按实际预检结果询问：

- **多网卡/多 IP**：让用户确认 `CUBE_SANDBOX_NODE_IP`，不得盲选；
- **x86 无 KVM**：询问原生 KVM 还是 PVM 路线。PVM 安装与重启是独立授权步骤；
- **ARM 无 KVM**：说明 PVM 不支持 ARM，必须先提供原生 KVM；
- **网络 CIDR 冲突**：向用户索取不冲突的 `CUBE_SANDBOX_NETWORK_CIDR`；
- **代理/私有 registry/外部数据库/端口冲突**：索取对应官方变量，不自行猜值；
- **自定义包地址**：要求同架构文件名及可信 `CUBE_SANDBOX_SHA256`。

常规单网卡、原生 KVM、官方源可直接使用默认值，不为可选变量反复打扰用户。

### 4. 正式执行前给出变更摘要

至少说明：选定版本/架构、节点 IP、原生 KVM 或 PVM、CN 或全球镜像路线、安装根目录，以及
将安装依赖并调整系统网络。多 IP、端口/CIDR 冲突、已有/部分安装或任何破坏性前置操作
未确认时不得继续。脚本会强制要求多 IP 场景显式设置节点地址。

### 5. 一键部署并等待完成

```bash
bash scripts/deploy.sh --env /secure/path/deployment.env
# 无环境文件时
bash scripts/deploy.sh
```

检测到已有或部分安装时不要加参数强行继续；本技能没有升级/恢复入口，应转入用户授权的
官方手工运维流程。不要在同一条远程命令中串联重启、磁盘格式化、模板创建或测试。大文件下载和镜像拉取可能
耗时，保持命令会话并观察退出状态；中断后先检查官方安装状态，不盲目重装。

### 6. 核验并报告部署结果

只有官方 `install.sh`、quickcheck 均成功且 `.one-click.env` 存在时才报告成功。报告：

- 版本、主机架构、PVM 模式、安装根目录；
- CubeAPI（默认 `127.0.0.1:3000`）和 WebUI（默认 `<node-ip>:12088`）；
- 未创建模板、未运行沙箱业务或性能测试；
- 后续运维/排障路径，以及必要的安全提醒（API/WebUI 不直接暴露到不可信网络）。

失败时保留原始错误，给出已完成的阶段和下一步排查，不声称“一键部署成功”。常见故障及
架构注意事项见 [references/deployment.md](references/deployment.md)。

## 脚本契约

- `scripts/deploy.sh`：只读预检、双架构 Release 解析、SHA-256 校验、bpffs/PVM 模块准备和
  官方全新安装。完整/部分已有安装均拒绝继续；使用部署锁并在安装前二次检查。环境文件经
  包装器变量过滤后通过 `ONE_CLICK_ENV_FILE` 传给官方安装器。
- `scripts/discover_release.py`：仅使用 Python 标准库，从 GitHub Release API 精确选择
  `cube-sandbox-one-click-<version>-<amd64|arm64>.tar.gz`；CN 模式使用同资产 CNB 地址并
  保留 GitHub 回退。API 限流时回退官方 latest 元数据/版本标签，明确警告无 API 摘要；
  严格摘要场景使用用户安全提供的 `GITHUB_TOKEN` 或自定义 URL + SHA-256。
- `deployment.env.example`：全新安装的最小覆盖项，只写明确需要调整的官方键。
- `cases/deployment-contract/run.sh`：无副作用的双架构资产选择、镜像映射和脚本静态契约测试。
