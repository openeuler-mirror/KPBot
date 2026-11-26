---
name: openviking-deploy
description: OpenViking 环境安装部署技能（Linux，aarch64/x86_64 通用）。当需要安装、部署、搭建、重装或升级 OpenViking（AI Agent 上下文数据库）服务端，从官方 Git 仓库源码构建、配置 Embedding/VLM 模型、生成 ov.conf、以 systemd 或前台方式启动并验证 /health、/ready 时使用。未指定版本时默认安装 master 分支（https://github.com/volcengine/OpenViking.git）并记录实际 commit；只负责环境部署，不负责性能调优、客户端接入与运维编排。
license: MulanPSL-2.0
compatibility: Linux（openEuler 24.03 已验证，兼容 RHEL/CentOS/Debian 系），aarch64 或 x86_64，root 权限，Python 3.10+、git、gcc/g++、CMake 3.15+、Rust 1.91.1+、maturin；模型侧需可访问 Embedding 与 VLM 服务（或本机 Ollama）。
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: agent_context_database
  activation_mode: active
  side_effect_free: "false"
  version: "1.0.0"
---

# OpenViking 环境安装部署

在 Linux 主机上从官方源码完成 OpenViking 服务端的依赖准备、源码构建、Python 虚拟环境安装、
模型配置生成、systemd 托管与健康验收，产出可复用的 `deploy.env`，并支持按 commit 保留旧版本用于回滚。

## 版本策略（强约束）

| 用户输入 | 行为 |
|----------|------|
| 未指定版本 | **安装 master 分支**（默认值，`https://github.com/volcengine/OpenViking.git`）；若上游默认分支已改名为 main，则按默认分支安装，并记录实际 commit |
| 指定 tag（如 v0.3.22） | 按 tag checkout 构建，记录 commit 与包版本 |
| 指定分支或 commit SHA | 按指定 ref checkout 构建，记录 commit |
| 使用 `--source-dir` | 使用目录自带版本，不得再指定非 master 的 `--version` |
| 只说要“最新版” | 说明 master/默认分支是移动分支；按默认分支执行并记录 commit，不猜测具体 tag |
| 要求 PyPI 预编译包 | 说明本技能默认源码构建；若用户坚持，使用 `--wheel` 安装预编译 wheel，不混用 master 版本声明 |

禁止把“未指定版本”解释为某个发行版包或旧 tag；禁止只写 `master` 而不记录实际 commit。

## 何时触发

- 需要安装、部署、搭建、重装或升级 OpenViking 服务端
- 需要从官方 Git 仓库源码构建 OpenViking（默认 master，支持 tag/分支/commit）
- 需要准备 Python 3.10+、Rust、CMake、C++ 编译工具链并创建独立 venv
- 需要生成或接管 `ov.conf`，配置 Embedding 与 VLM 模型
- 需要 systemd 托管、健康检查（`/health`、`/ready`）或升级回滚

不适用：客户端 `ov` CLI 接入与 agent 插件安装；性能调优；Docker/Kubernetes 编排；
企业私有化交付包（应使用官方企业部署文档）。

## 工作流总览

```text
前置检查（只读） → 依赖安装（可选 Rust 校验安装） → 获取源码（git master/tag 或离线目录）
  → 创建独立 venv → 构建并安装 OpenViking → 生成/接管 ov.conf
  → 安装 systemd 服务（或后台进程） → doctor 检查 → /health + /ready 验收 → deploy.env
```

原则：每一步失败都保留日志与现场；不删除旧 release、旧配置、旧数据；覆盖前先备份。

## 快速开始

在 skill 根目录执行：

```bash
# 0) 先把 API key 写入权限 600 的文件；read -s 不会进入 shell history
install -m 600 /dev/null /root/.openviking_api_key
read -rs -p 'API key: ' OV_KEY && printf '%s\n' "$OV_KEY" > /root/.openviking_api_key && unset OV_KEY

# 1) 只读前置检查（默认 master）
bash scripts/preflight_check.sh

# 2) 默认部署：master + 生成 ov.conf + systemd 托管
bash scripts/install_openviking.sh \
  --provider volcengine \
  --embedding-model doubao-embedding-vision-251215 \
  --vlm-model doubao-seed-2-0-lite-260428 \
  --api-key-file /root/.openviking_api_key

# 3) 指定 tag
bash scripts/install_openviking.sh --version v0.3.22 \
  --provider openai --embedding-model text-embedding-3-small \
  --vlm-model gpt-5.4 --api-key-file /root/.openviking_api_key

# 4) 接管已有 ov.conf（不重新生成）
bash scripts/install_openviking.sh --ov-conf /root/ov.conf

# 5) 离线源码目录
bash scripts/install_openviking.sh --source-dir /root/OpenViking \
  --provider volcengine --embedding-model doubao-embedding-vision-251215 \
  --vlm-model doubao-seed-2-0-lite-260428 --api-key-file /root/.openviking_api_key

# 6) 只打印计划
bash scripts/install_openviking.sh --dry-run
```

模型信息不全时先向用户确认 provider、Embedding/VLM 模型、API Base、workspace 与 API key 交付方式，
不要写入占位 key，也不要猜测模型名。

## 默认布局

| 项 | 默认值 |
|----|--------|
| 版本 | `master`（记录实际 commit） |
| 源码仓库 | `https://github.com/volcengine/OpenViking.git` |
| 安装前缀 | `/opt/openviking` |
| release 目录 | `/opt/openviking/releases/<commit12>` |
| 当前版本链接 | `/opt/openviking/current` |
| Python venv | `/opt/openviking/current/venv` |
| 配置 | `/etc/openviking/ov.conf`（0640 root:openviking） |
| 数据目录 | `/var/lib/openviking` |
| workspace | `/var/lib/openviking/data` |
| 运行用户 | `openviking`（系统用户，nologin） |
| 监听地址 / 端口 | `127.0.0.1` / `1933` |
| 服务 | `openviking.service`（Type=simple，Restart=always） |
| 构建缓存 | `/var/cache/openviking-deploy`（root:root 0700） |
| 产物 | `/var/log/openviking-deploy/<run_id>/env/deploy.env` |

## 参数

```text
--version REF            版本/分支/commit，默认 master
--git-url URL            源码仓库，仅支持 http(s)://，默认官方仓库；本地源码用 --source-dir
--source-dir DIR         离线/本地源码目录（要求 root 属主、组/其他不可写）
--wheel FILE             预编译 wheel；指定后跳过源码构建

--prefix DIR             安装前缀，默认 /opt/openviking
--build-root DIR         构建缓存，默认 /var/cache/openviking-deploy（要求 root:root 0700）
--result-dir DIR         日志与 deploy.env 输出目录，默认 /var/log/openviking-deploy/<run_id>
--run-as USER            运行用户，默认 openviking（不存在则创建系统用户）
--conf-dir DIR           配置目录，默认 /etc/openviking
--data-dir DIR           数据根目录，默认 /var/lib/openviking
--service-name NAME      服务名，默认 openviking

--ov-conf FILE           接管已有 ov.conf（校验 JSON 与必需字段后安装）
--provider NAME          生成配置时的 provider：volcengine/openai/azure/ollama
--api-base URL           Embedding 与 VLM 的 API Base（可按 provider 默认）
--embedding-model NAME   Embedding 模型名
--vlm-model NAME         VLM 模型名
--dimension N            Embedding 维度（可选）
--input-mode MODE        Embedding input：text/multimodal（可选，volcengine）
--api-version VER        Azure API 版本（可选）
--api-key-file FILE      API key 文件（要求 0600；key 不进入 argv/日志）
--root-api-key-file FILE 非回环监听时的 root API key 文件（要求 0600）
--workspace DIR          storage.workspace，默认 <data-dir>/data
--host ADDR              监听地址，默认 127.0.0.1
--port PORT              监听端口，默认 1933

--with-studio            安装 Node.js 并构建 Web Studio
--skip-studio            跳过 Web Studio 构建
--install-rust           允许脚本用 rustup-init（sha256 校验）安装 Rust
--skip-deps              跳过系统依赖安装
--no-systemd             不使用 systemd，以后台进程方式启动
--pip-index-url URL      私有 pip 源（禁止携带用户名/密码）
--ready-timeout SEC      等待 /ready 的超时，默认 180
--dry-run                打印计划，不落盘、不启动服务
```

## 安全基线

- API key 只从 `--api-key-file`（0600）或用户提供的 `ov.conf` 读取：不进入 argv、不写日志、
  不写 `deploy.env`；生成的 `ov.conf` 固定 `0640 root:<运行组>`，仅运行用户可读。
- 只有非回环监听时才允许通过 `--root-api-key-file` 配置 `server.root_api_key`；非回环且无鉴权直接失败。
- 源码/构建根必须为 root 私有：`--build-root` 要求 `root:root 0700`，父目录不得被非 root 用户控制；
  工作目录用 `mktemp -d` 创建。`--source-dir` 要求 root 属主且组/其他不可写，复制后再次校验。
- 路径参数必须是绝对路径，拒绝 `.`、`..`、`//` 段；归一化后命中系统目录黑名单（`/`、`/etc`、`/var` 等）直接失败。
- `--git-url` 仅接受 `http(s)://`；本地源码必须走 `--source-dir`，禁止 `file://`。
- Rust 缺失时默认失败并给出安装指引；只有显式 `--install-rust` 才下载 rustup-init，并用官方
  `.sha256` 校验后再执行，禁止把下载内容直接管道给 shell，禁止关闭 TLS 校验。
- 服务以专用系统用户运行，不使用 root；不自动修改防火墙、SELinux、内核参数。
- 不删除旧 release、旧配置、旧数据；配置/unit 覆盖前生成 `.bak.<时间戳>`；升级保留旧 release 供回滚。
- `--pip-index-url` 不接受携带用户名/密码的 URL，避免凭据进入日志与进程参数。

## 模型配置（先问再做）

OpenViking 服务端至少需要 Embedding 与 VLM 两类模型配置。部署前必须与用户确认：

1. provider：`volcengine`、`openai`、`azure`、`ollama`，或其他受支持的 provider
2. Embedding 模型名与维度（如 volcengine `doubao-embedding-vision-251215`，dimension 1024）
3. VLM 模型名（如 volcengine `doubao-seed-2-0-lite-260428`）
4. API Base（provider 默认地址是否可用）
5. API key 的交付方式（本地文件或已有 `ov.conf`；不要粘贴到对话里反复回显）
6. `storage.workspace` 目录

以下情况不要继续写配置：用户不清楚模型、没有 key、只愿意用未确认的私有地址。
可建议用户先运行交互式向导（需要 TTY）：

```bash
/opt/openviking/current/venv/bin/openviking-server init
```

向导生成的 `ov.conf` 放到 `~/.openviking/ov.conf` 后，再用 `--ov-conf` 重新执行安装脚本接管配置。
混合 provider（如 volcengine Embedding + Codex/Kimi/GLM VLM）请直接使用 `--ov-conf`，不要用共享参数强行生成。

## 产物与验收

`deploy.env` 关键字段：

```text
DEPLOY_STATUS=ok
OPENVIKING_VERSION=master
OPENVIKING_BRANCH=main
OPENVIKING_COMMIT=<实际 commit>
OPENVIKING_PACKAGE_VERSION=<包版本>
OPENVIKING_SOURCE=https://github.com/volcengine/OpenViking.git
OPENVIKING_RELEASE_DIR=/opt/openviking/releases/<commit12>
OPENVIKING_CURRENT_LINK=/opt/openviking/current
OPENVIKING_VENV=/opt/openviking/current/venv
OPENVIKING_CONF=/etc/openviking/ov.conf
OPENVIKING_WORKSPACE=/var/lib/openviking/data
OPENVIKING_HOST=127.0.0.1
OPENVIKING_PORT=1933
OPENVIKING_SERVICE=openviking.service
OPENVIKING_RUN_USER=openviking
OPENVIKING_PROVIDER=volcengine
OPENVIKING_EMBEDDING_MODEL=doubao-embedding-vision-251215
OPENVIKING_VLM_MODEL=doubao-seed-2-0-lite-260428
OPENVIKING_CONFIG_SOURCE=generated|file
OPENVIKING_WHEEL=<构建产物 wheel 路径>
OPENVIKING_HEALTH=ok
OPENVIKING_READY=ok
OPENVIKING_API_KEY_SET=0|1
```

验收命令：

```bash
systemctl is-active openviking
ss -lntp | grep ':1933'
curl -fsS http://127.0.0.1:1933/health
curl -fsS http://127.0.0.1:1933/ready
/opt/openviking/current/venv/bin/openviking-server doctor --config /etc/openviking/ov.conf
```

通过标准：服务 active；端口 LISTEN；`/health` 返回 `status: ok` 且 `healthy: true`；
`/ready` 返回 `status: ready`；`doctor` 退出码 0；`deploy.env` 中 `DEPLOY_STATUS=ok`。

## 重复执行、升级与回滚

- 同一 commit 重跑：复用 `/opt/openviking/releases/<commit12>` 的 venv，重新构建安装并重启服务。
- 不同 commit/tag：创建新 release 目录，安装完成后原子切换 `current` 符号链接并重启；旧 release 保留。
- 配置/unit 覆盖前生成 `.bak.<时间戳>`，旧 `deploy.env` 重命名为 `.previous.<run_id>`。
- 回滚：停服 → 把 `current` 指向旧 release → 恢复旧配置/unit 备份 → 启动并复验 `/health`、`/ready`；
  旧 release 被人工删除时，使用 `deploy.env` 记录的 wheel 重新安装，不得声称可回滚。
- 不自动删除 release、数据目录与旧配置；清理必须人工确认。

## 常见问题速查

| 症状 | 处置 |
|------|------|
| 未指定版本 | 直接用默认 master，并在 deploy.env 记录实际 commit |
| Rust 版本不足 | 用 `--install-rust` 由脚本校验安装，或手工安装 Rust 1.91.1+ 后重跑 |
| CMake/编译器缺失 | 运行 `scripts/install_deps.sh`，确认 gcc/g++ >= 9 或 clang >= 11、CMake >= 3.15 |
| pip 安装失败在 ragfs/原生扩展 | 确认 cargo、maturin 可用；查看 `logs/build.log`，不要跳过 ragfs 后声称完整 |
| /health 正常但 /ready 非 ready | 检查 embedding/api_base/api_key、模型名、网络；`/ready` 才代表模型可用 |
| doctor 报配置缺字段 | 补齐 `embedding.dense`、`vlm`、`storage.workspace`；不要写占位 key |
| 非回环监听启动失败 | 配置 `--root-api-key-file`，或改回 `127.0.0.1` |
| 端口占用 | `ss -lntp` 定位；停旧实例或改 `--port` |
| systemd 启动失败 | `journalctl -u openviking -n 100`；查配置路径、目录权限、运行用户、端口 |
| 构建根/源码目录被拒绝 | 使用默认 `/var/cache/openviking-deploy`，或修正为 root:root 0700；源码目录改为 root 属主 |
| Web Studio 404 | 构建时缺少 npm；重新用 `--with-studio` 安装 Node.js 并重跑 |
| 升级后异常 | 将 `current` 指回旧 release，恢复配置备份，重启并复验 |

## 脚本索引

| 脚本 | 用途 |
|------|------|
| `scripts/preflight_check.sh` | 部署前只读检查：权限、OS/架构、资源、工具链、源码/网络、构建目录、端口、既有安装、配置与模型参数 |
| `scripts/install_deps.sh` | 安装 git/gcc/g++/make/cmake/python3/pip/venv 与基础库；可选安装 Rust（sha256 校验）与 Node.js |
| `scripts/gen_ov_conf.py` | 从已确认参数生成 `ov.conf`，API key 只从文件读取；支持校验已有配置 |
| `scripts/install_openviking.sh` | 主部署流程：源码获取、release venv、构建安装、配置接管、systemd、doctor、/health+/ready、deploy.env |
| `scripts/lib/common.sh` | 日志、root 检查、路径校验等公共函数 |

## 参考资料

- `references/openviking-install-guide.md` — 支持矩阵、源码构建、版本策略、模型配置、systemd、
  离线/私有源、升级回滚、验收与排障细节。
- `assets/ov.conf.template` — 最小配置模板（占位符，不可直接使用）。
- `assets/openviking.service.template` — systemd 单元模板。
- 上游项目与文档：https://github.com/volcengine/OpenViking 、https://docs.openviking.ai/

## 输出契约

- 成功：`deploy.env` 中 `DEPLOY_STATUS=ok`，并给出 release 路径、commit、配置路径、服务名、监听地址与验收结果。
- 失败：返回失败阶段、日志路径、已生成产物与缺失项；等待人工处理，不自动删除现场。
- 任何变更类操作：说明改前/改后、备份路径与回滚方式；不得输出 API key 或 root API key。

## 错误处理

依赖安装失败、源码获取失败、构建失败、配置缺字段、doctor 失败、端口冲突或服务启动失败时立即停止后续阶段，
保留 `logs/`、`deploy.env`（`DEPLOY_STATUS=failed`）、release 目录与已有配置，修复后重跑即可继续。
无法满足安全基线（路径非法、构建目录不安全、源码目录非 root、非回环无鉴权、key 文件权限过宽）时
返回原因并等待用户决策。
