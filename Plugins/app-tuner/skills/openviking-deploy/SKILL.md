---
name: openviking-deploy
description: OpenViking 服务端安装部署技能（Linux）。当需要安装、部署、升级 OpenViking，从官方仓库源码构建、配置 Embedding/VLM、生成 ov.conf、systemd 托管并验证 /health、/ready 时使用。默认安装上游默认分支（当前 main，master 为历史别名）并记录 commit；不负责调优与客户端接入。
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

在 Linux 主机上从官方源码完成依赖准备、源码构建、独立 release venv 安装、模型配置生成、
systemd 托管与健康验收，产出 `deploy.env`，并按 commit 保留旧 release 供回滚。

## 版本策略（强约束）

| 用户输入 | 行为 |
|----------|------|
| 未指定版本 | 安装上游默认分支（当前 `main`；`master` 是历史别名），记录实际 commit |
| 指定 tag/分支/commit | 按 ref checkout，记录 commit 与包版本；需要时加 `--verify-signature` 做 GPG 校验 |
| 使用 `--source-dir` | 使用目录自带版本，不得再指定非 main 的 `--version` |
| 只说要“最新版” | 说明默认分支是移动分支；按默认分支执行并记录 commit，不猜测 tag |
| 要求预编译包 | 使用 `--wheel` 安装，不混用源码版本声明 |

禁止把“未指定版本”解释为发行版包或旧 tag；禁止不记录实际 commit。

## 何时触发

- 安装、部署、搭建、重装或升级 OpenViking 服务端
- 从官方 Git 仓库源码构建（默认 main，支持 tag/分支/commit、离线目录、预编译 wheel）
- 准备 Python 3.10+、Rust、CMake、C++ 工具链并创建独立 venv
- 生成/接管 `ov.conf`，配置 Embedding 与 VLM
- systemd 托管、`/health`/`/ready` 验收或升级回滚

不适用：客户端 `ov` CLI 接入与 agent 插件安装；性能调优；Docker/Kubernetes 编排；
企业私有化交付包。

## 工作流

```text
前置检查（只读） → 依赖安装 → 获取源码/ wheel → release venv 构建安装
  → 生成/接管 ov.conf → systemd（或后台进程） → doctor → /health + /ready → deploy.env
```

每步失败保留日志与现场；不删除旧 release、旧配置、旧数据；覆盖前先备份。

## 快速开始

```bash
# API key 写入 0600 文件；read -s 不进入 shell history
install -m 600 /dev/null /root/.openviking_api_key
read -rs -p 'API key: ' OV_KEY && printf '%s\n' "$OV_KEY" > /root/.openviking_api_key && unset OV_KEY

bash scripts/preflight_check.sh
bash scripts/install_openviking.sh --provider volcengine \
  --embedding-model doubao-embedding-vision-251215 \
  --vlm-model doubao-seed-2-0-lite-260428 \
  --api-key-file /root/.openviking_api_key

# 指定 tag / 接管已有配置 / 离线源码 / 预编译 wheel / 只打印计划
bash scripts/install_openviking.sh --version v0.3.22 --provider openai \
  --embedding-model text-embedding-3-small --vlm-model gpt-5.4 \
  --api-key-file /root/.openviking_api_key
bash scripts/install_openviking.sh --ov-conf /root/ov.conf
bash scripts/install_openviking.sh --source-dir /root/OpenViking --provider ollama \
  --embedding-model nomic-embed-text --vlm-model llava
bash scripts/install_openviking.sh --wheel /root/openviking-*.whl
bash scripts/install_openviking.sh --dry-run
```

模型信息不全时先确认 provider、Embedding/VLM 模型、API Base、workspace 与 API key 交付方式，
不要写占位 key 或猜测模型名。

## 默认布局

| 项 | 默认值 |
|----|--------|
| 版本 / 仓库 | `main`（历史别名 `master`）/ `https://github.com/volcengine/OpenViking.git` |
| 安装前缀 | `/opt/openviking`，release 为 `releases/<commit12>`，`current` 为符号链接 |
| venv | `/opt/openviking/current/venv` |
| 配置 / 数据 | `/etc/openviking/ov.conf`（0640 root:openviking）/ `/var/lib/openviking` |
| 运行用户 | `openviking`（系统用户，nologin） |
| 监听 / 服务 | `127.0.0.1:1933` / `openviking.service`（Restart=always） |
| 构建缓存 / 产物 | `/var/cache/openviking-deploy`（root:root 0700）/ `/var/log/openviking-deploy/<run_id>/env/deploy.env` |

## 关键参数

完整参数以 `bash scripts/install_openviking.sh --help` 与 `preflight_check.sh --help` 为准。

| 参数 | 说明 |
|------|------|
| `--version REF` | 版本/分支/commit，默认 main |
| `--verify-signature` | 对 tag/commit 做 GPG 校验（需已导入公钥） |
| `--git-url URL` | 仅 http(s)://，不得含 userinfo；本地源码用 `--source-dir` |
| `--source-dir DIR` / `--wheel FILE` | 离线源码 / 预编译 wheel（root 属主，组/其他不可写） |
| `--prefix` / `--build-root` / `--result-dir` | 安装前缀 / 构建缓存（root:root 0700）/ 日志与产物 |
| `--ov-conf FILE` | 接管已有配置；监听地址与端口以配置内 `server.host/port` 为准 |
| `--provider` / `--api-base` / `--embedding-model` / `--vlm-model` | 生成配置所需的模型信息 |
| `--api-key-file` / `--root-api-key-file` | API key / 非回环监听的 root key（均要求 0600） |
| `--workspace` / `--host` / `--port` | workspace / 监听地址 / 端口（默认 127.0.0.1:1933） |
| `--with-studio` / `--skip-studio` | 构建或跳过 Web Studio |
| `--install-rust` / `--skip-deps` / `--no-systemd` | 允许安装 Rust / 跳过系统依赖 / 后台进程启动 |
| `--pip-index-url` / `--ready-timeout` / `--dry-run` | 私有 pip 源 / 就绪超时 / 只打印计划 |

## 安全基线

- API key 只从 0600 文件或已有 `ov.conf` 读取，不进入 argv/日志/`deploy.env`；
  生成的配置固定 `0640 root:<运行组>`。
- 非回环监听必须配置 root API key；`--ov-conf` 模式同样以配置内 `server.root_api_key` 校验。
- `--git-url` 拒绝 userinfo/查询串/片段，`--pip-index-url` 拒绝 userinfo；日志与 `deploy.env`
  不接收原始凭据 URL。
- 构建根 `root:root 0700`，源码目录要求 root 属主且组/其他不可写，并校验父目录链；
  路径拒绝 `.`、`..`、`//` 与系统目录。
- Rust 缺失时默认失败；仅 `--install-rust` 才下载 rustup-init 并做官方 `.sha256` 校验，
  禁止把下载内容直接管道给 shell，禁止关闭 TLS 校验。
- 服务以专用用户运行；不自动改防火墙、SELinux、内核参数；不删除旧 release、配置与数据。
- 结果目录 `logs/`、`env/` 为 0750，日志与 `deploy.env` 为 0640，避免凭据类残留被本地用户读取。

## 模型配置（先问再做）

服务端至少需要 Embedding 与 VLM。部署前确认：provider、Embedding 模型与维度、VLM 模型、
API Base、API key 交付方式、`storage.workspace`。用户不清楚模型或没有 key 时不要写配置；
可建议先运行 `/opt/openviking/current/venv/bin/openviking-server init`（需要 TTY），
再用 `--ov-conf` 接管。混合 provider 直接使用 `--ov-conf`，不要用共享参数强行生成。

Ollama 默认 API Base 为 `http://127.0.0.1:11434/v1`，可不提供 API key；
其他 provider 缺少 key 时脚本直接失败，不写占位 key。

## 产物与验收

`deploy.env` 关键字段：

```text
DEPLOY_STATUS=ok
OPENVIKING_VERSION=main
OPENVIKING_BRANCH=main
OPENVIKING_COMMIT=<实际 commit>
OPENVIKING_SIGNATURE_VERIFIED=0|1
OPENVIKING_PACKAGE_VERSION=<包版本>
OPENVIKING_RELEASE_DIR=/opt/openviking/releases/<commit12>
OPENVIKING_CURRENT_LINK=/opt/openviking/current
OPENVIKING_CONF=/etc/openviking/ov.conf
OPENVIKING_WORKSPACE=/var/lib/openviking/data
OPENVIKING_HOST=127.0.0.1
OPENVIKING_PORT=1933
OPENVIKING_SERVICE=openviking.service
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

通过标准：服务 active、端口 LISTEN、`/health` 为 `status: ok` 且 `healthy: true`、
`/ready` 为 `status: ready`、`doctor` 退出码 0、`deploy.env` 中 `DEPLOY_STATUS=ok`。
`/health` 正常不代表模型可用，不能替代 `/ready`。

## 升级与回滚

- 同一 commit 重跑复用 release；不同 commit/tag 创建新 release，原子切换 `current` 后重启。
- 配置/unit 覆盖前生成 `.bak.<时间戳>`；旧 `deploy.env` 重命名为 `.previous.<run_id>`。
- 回滚：停服 → `current` 指回旧 release → 恢复配置/unit 备份 → 启动并复验；
  旧 release 被删除时用 `deploy.env` 记录的 wheel 恢复，无法恢复则明确说明不可回滚。

## 常见问题速查

| 症状 | 处置 |
|------|------|
| Rust/CMake/编译器缺失 | `--install-rust` 或手工安装 Rust 1.91.1+；确认 CMake 3.15+、g++ 9+/clang++ 11+ |
| ragfs/原生扩展构建失败 | 看 `logs/build.log`；确认 cargo、maturin 可用，不要跳过 ragfs 后声称完整 |
| `/health` 正常但 `/ready` 非 ready | 检查 embedding/api_base/api_key、模型与网络；`/ready` 才代表模型可用 |
| doctor 报配置缺字段 | 补齐 `embedding.dense`、`vlm`、`storage.workspace`；不要写占位 key |
| 非回环启动失败 | 配置 root API key；`--ov-conf` 模式检查 `server.root_api_key` |
| 端口占用 | `ss -lntp` 定位；停旧实例或改 `--port` |
| systemd 启动失败 | `journalctl -u openviking -n 100`；查配置、目录权限、运行用户、端口 |
| 源码/构建目录被拒绝 | 使用默认 `/var/cache/openviking-deploy`；源码目录改为 root 属主且父目录可信 |
| 升级后异常 | `current` 指回旧 release，恢复配置备份，重启并复验 |

## 脚本与资料

| 项 | 用途 |
|----|------|
| `scripts/preflight_check.sh` | 只读前置检查（权限、工具链、源码/wheel 与父目录、构建目录、端口、配置） |
| `scripts/install_deps.sh` | 安装系统依赖；可选 Rust（sha256 校验）与 Node.js |
| `scripts/gen_ov_conf.py` | 生成/校验 `ov.conf`；API key 只从文件读取 |
| `scripts/install_openviking.sh` | 主流程：源码、release venv、构建安装、配置、systemd、doctor、验收 |
| `references/openviking-install-guide.md` | 支持矩阵、构建、模型配置、离线、升级回滚、排障 |
| `assets/ov.conf.template` / `assets/openviking.service.template` | 配置模板 / systemd 单元模板 |

上游：https://github.com/volcengine/OpenViking ，文档：https://docs.openviking.ai/

## 输出契约

- 成功：`deploy.env` 中 `DEPLOY_STATUS=ok`，给出 release、commit、配置路径、服务名、监听地址与验收结果。
- 失败：返回失败阶段、日志路径、已生成产物与缺失项；等待人工处理，不自动删除现场。
- 任何变更：说明改前/改后、备份路径与回滚方式；不得输出 API key 或 root API key。
