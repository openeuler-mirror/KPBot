# CubeSandbox 在线单机部署参考

## 1. 范围与依据

本技能部署 **单机 all-in-one（control + compute）**，使用官方 one-click 发布包和包内
`install.sh`。脚本负责双架构资产选择、下载完整性校验、必要的主机准备和安全保护；组件
安装、Docker/DNS/systemd 配置及服务健康 quickcheck 仍由官方安装器完成。

参考来源（2026-11-11 核对）：

- [裸金属 / 物理机部署](https://github.com/TencentCloud/CubeSandbox/blob/master/docs/zh/guide/bare-metal-deploy.md)
- [快速开始 / PVM](https://github.com/TencentCloud/CubeSandbox/blob/master/docs/zh/guide/quickstart.md)
- [one-click 说明](https://github.com/TencentCloud/CubeSandbox/tree/master/deploy/one-click)
- [官方 Releases](https://github.com/TencentCloud/CubeSandbox/releases)

核对时最新稳定版为 v0.7.3，同时提供 `amd64` 与 `arm64` one-click 资产。脚本不硬编码
此版本；`CUBE_VERSION=latest` 优先从 GitHub API 解析最新稳定版，并读取 Release API 返回
的 SHA-256。`MIRROR=cn` 只把同一资产切换为 CNB 下载地址，校验值不变，下载失败时回退
GitHub。这解决了官方文档所述 `online-install.sh` 当前仅自动发现 x86_64 包的限制。

GitHub API 被限流或暂时不可达时，latest 会回退到官方
`download.cubesandbox.com/release/latest.json` 获取版本，指定版本则使用用户给出的标签；
两种降级都只构造官方 CNB/GitHub 资产地址，并明确警告本次没有 API 提供的 SHA-256。
需要严格摘要固定时，可提供 `GITHUB_TOKEN` 后重试，或由用户独立核验后设置
`CUBE_SANDBOX_DOWNLOAD_URL` 与 `CUBE_SANDBOX_SHA256`。API 已返回 Release 但缺少目标架构
或摘要时不会降级掩盖错误。

## 2. 不在本技能范围内

- 不创建模板，不安装 Python SDK，不创建真实沙箱。
- 不运行 cube-bench、Pause/Resume、E2B 示例或性能测试。
- 不制作离线包；目标机必须能下载发布包、系统依赖和容器镜像。
- 不自动分区/格式化磁盘，不自动安装 PVM 宿主内核或重启机器。
- 不部署独立计算节点或多机集群。
- 只支持全新安装，不升级、不恢复已有或部分安装；这些场景转官方手工运维流程。

官方 quickcheck 仅检查 systemd 单元、健康端点、socket、运行文件、节点注册和依赖容器
等部署健康状态，本技能始终保留它。它不是业务负载或性能测试。

## 3. 主机要求与副作用

### 最低要求

- Linux、root、正在运行的 systemd。
- x86_64 或 aarch64；glibc >= 2.31。
- >= 4 逻辑 CPU、>= 8 GiB 内存。
- `/dev/kvm` 可读写；bpffs/eBPF 和 cgroup CPU controller 可用。
- `/data/cubelet` 位于 XFS 上，至少 50 GiB 可用；建议生产环境 200 GiB 以上。
- 可访问 Release 下载站、操作系统软件源和容器镜像仓库；如使用代理或私有仓库，
  必须先向用户确认参数。
- 常见默认端口（3000、80、443、12088、3306、6379、9000、9001 等）无冲突。

### 安装会修改的内容

官方安装器可能安装/重启 Docker 与依赖容器，并修改 systemd、DNS/NetworkManager、
iptables、CoreDNS/dnsmasq、bpffs、`/data/cubelet`、
`/usr/local/services/cubetoolbox` 和相关证书。已有业务主机必须先取得维护窗口和备份确认。
包装脚本还会在需要时挂载 bpffs，并向 `/etc/fstab` 添加持久化条目。安装根目录固定为官方
`/usr/local/services/cubetoolbox`，不接受自定义路径。正式部署持有 flock 锁，并在主机准备
和调用官方安装器前分别重新检查；发现部分安装、非空未知目录或下载期间出现的新安装会停止，
要求人工备份/恢复，不会自动清理。

## 4. x86_64 与 ARM64 差异

| 项目 | x86_64 | ARM64 / aarch64 |
|---|---|---|
| Release 资产 | `*-amd64.tar.gz` | `*-arm64.tar.gz` |
| 原生 KVM | 支持，`CUBE_PVM_ENABLE=0` | 支持，`CUBE_PVM_ENABLE=0` |
| PVM 宿主内核 | 支持；准备并重启后使用 `auto` 或 `1` | **不支持**，不可设置 `1` |
| `/dev/kvm` 缺失 | 可经用户确认后另行按官方 PVM 指南处理 | 必须启用原生 KVM，无 PVM 兜底 |
| guest PMU | 按宿主机能力暴露 | 部分老内核/嵌套环境无 PMUv3，不阻塞 MicroVM 部署 |

`CUBE_PVM_ENABLE=auto` 在检测到 x86_64 PVM 内核或 `kvm_pvm` 时选择 PVM guest kernel，
其他情况选择普通 guest kernel。检测到 `kvm_pvm` 却强制设置 `0` 会被拒绝，因为普通
guest kernel 与 PVM 宿主不匹配。

官方文档说明 `sandbox-code:latest` 是 multi-arch 镜像，但其他托管镜像的 multi-arch
覆盖可能随版本变化。ARM 部署若报镜像 `exec format error` 或 manifest 无 arm64，应核对
该版本全部依赖镜像，向用户索取可用的 ARM64 私有镜像覆盖值；不要用 amd64 模拟运行冒充
原生 ARM 部署。模板镜像属于后续模板创建步骤，本技能不会自动拉取模板。

## 5. 需要 Agent 确认的环境变量

先运行只读预检，再只询问实际缺失或有风险的项，避免一次性向用户抛出整份配置。

| 变量 | 何时需要询问/确认 | 取值 |
|---|---|---|
| `CUBE_VERSION` | 用户有兼容性或版本冻结要求时 | 默认 `latest`，或 `vX.Y.Z` |
| `MIRROR` | 境内外网络或镜像站有要求时 | `cn`；空字符串表示 GitHub/global |
| `CUBE_SANDBOX_NODE_IP` | 多网卡、多 IPv4、NAT 或自动检测值不符合预期时 | 目标节点 IPv4 |
| `CUBE_PVM_ENABLE` | x86 PVM 主机，或自动检测结果与用户规划不一致时 | `auto` / `0` / `1` |
| `CUBE_SANDBOX_NETWORK_CIDR` | 官方默认 `192.168.0.0/18` 与主机、VPN、VPC 冲突时 | 不冲突的私网，掩码 `/16`～`/24` |
| `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` | 下载或镜像访问必须经过代理时 | 由用户提供；回环地址加入 `NO_PROXY` |
| `GITHUB_TOKEN` | GitHub API 限流且用户要求严格 API 摘要固定时 | 仅通过安全环境注入，不写入文件或日志 |
| `CUBE_SANDBOX_DOWNLOAD_URL` | 用户指定可信软件 mirror 时 | 文件名必须匹配当前架构 |
| `CUBE_SANDBOX_SHA256` | 使用自定义下载地址时强烈建议；官方 API 可用时自动获取 | 64 位十六进制摘要 |
| 官方镜像/数据库/端口/DNS 变量 | 私有 registry、外部依赖或端口冲突时 | 只写明确需要覆盖的官方键 |

自定义 `CUBE_API_BIND` / `CUBE_OPS_BIND` 时，脚本会从端口派生默认的
`CUBE_API_HEALTH_ADDR`、`CUBE_OPS_HEALTH_ADDR`、`CUBE_OPS_UPSTREAM` 和 `WEB_UI_UPSTREAM`，
避免健康检查与 WebUI 仍访问旧端口；用户显式设置的关联值优先，但必须自行保证一致。

不要把密码、Token、带凭据代理 URL 写入仓库、聊天记录或命令行历史。环境文件应放在
技能目录之外并限制权限。Shell 代理与 Docker daemon 代理是两套配置；如 Docker 拉取镜像
也需要代理，应按用户的 Docker 运维规范另行配置，不要把 shell 代理隐式当成 daemon 配置。

## 6. 执行方式

### 只读预检

```bash
bash scripts/deploy.sh --preflight-only
# 或
bash scripts/deploy.sh --env /secure/path/deployment.env --preflight-only
```

预检会检查主机、监听端口、沙箱 CIDR 与主机路由冲突，并在线解析与架构匹配的 Release，
但不下载大文件、不修改主机。出现 `AUTO` 表示正式部署会挂载 bpffs 或加载已就绪的 PVM
模块；出现多条 IPv4 时预检会失败，Agent 必须先让用户确认并显式设置节点 IP。

### 一键部署

```bash
bash scripts/deploy.sh --env /secure/path/deployment.env
# 无自定义配置时：
bash scripts/deploy.sh
```

流程：

```text
主机预检 → 选择 amd64/arm64 Release → （正式部署时）准备 bpffs/PVM 模块
→ 下载并校验 SHA-256 → 官方 install.sh → 官方服务健康 quickcheck → 输出访问信息
```

### 已有安装与恢复

本技能没有升级或恢复入口。发现完整、部分或非空未知安装目录时都会停止；如需升级、修复或
重装，必须在备份和用户明确授权后使用官方安装器及运维文档另行处理。不要通过自定义安装根
目录绕过保护，包装脚本只接受官方固定路径。

全新安装时，包装脚本会把用户环境文件中的官方覆盖项通过 `ONE_CLICK_ENV_FILE` 传给安装器，
过滤下载/代理/Token 等包装器变量，并把解析后的 PVM、节点 IP 和 quickcheck 值写入
root-only 临时配置。

## 7. 成功判据与故障处理

成功判据是官方安装器和 quickcheck 均返回成功，且运行时环境文件
`/usr/local/services/cubetoolbox/.one-click.env` 存在。脚本最后输出版本、架构、PVM 模式、
CubeAPI 和 WebUI 地址。不要把“压缩包下载完成”或“进程曾启动”当成部署成功。

常见故障：

| 现象 | 处理 |
|---|---|
| 非 XFS / 空间不足 | 停止并请用户准备挂载点；不得自行 `mkfs` |
| 无 `/dev/kvm` | x86 询问原生 KVM 或 PVM 路线；ARM 只能启用原生 KVM |
| Release 无目标架构 | 换用包含该架构的稳定版，或使用用户验证过的同架构 URL + SHA-256 |
| 下载失败 | 核对 DNS、代理、GitHub/CNB 连通性；CN 模式会回退同一 GitHub 资产 |
| SHA-256 不匹配 | 不执行安装；重新解析 Release 或让用户确认可信资产 |
| 已有/部分安装 | 默认停止；备份并转用户授权的官方手工升级/恢复流程 |
| cgroup `+cpu` 失败 | 按官方指引排查 multipathd 等实时线程服务，不跳过检查 |
| Docker/DNS/端口冲突 | 保留官方日志，确认归属后处理；不要直接 kill 业务进程 |
| ARM 镜像架构错误 | 核对所有组件镜像的 arm64 manifest，配置用户提供的镜像覆盖 |

排障日志优先查看官方输出、
`/var/log/cube-sandbox-one-click/`、相关 systemd 单元和
`/usr/local/services/cubetoolbox/scripts/cube-diag/collect-logs.sh`（以当前版本实际文件为准）。
不要使用 `ONE_CLICK_SKIP_PRECHECK=1`、`ONE_CLICK_SKIP_PVM_CHECK=1` 或关闭 quickcheck 来
伪造成功。
