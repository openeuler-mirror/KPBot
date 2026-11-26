# OpenViking 部署执行手册

> 本文件是 `openviking-deploy` SKILL.md 的执行细节层：SKILL.md 负责触发、版本策略与总体流程，
> 此处给出支持矩阵、源码构建、模型配置、systemd、验收、升级回滚与排障细节。
> 性能调优、客户端接入、Docker/Kubernetes 编排不在本手册范围。

## 1. 支持范围与前置条件

| 项 | 要求 |
|----|------|
| 操作系统 | openEuler 24.03（已验证）、RHEL/CentOS/Rocky/Alma 8+、Debian 11+/Ubuntu 20.04+ |
| CPU | aarch64（鲲鹏）或 x86_64 |
| 权限 | root（系统依赖、`/opt`、`/etc`、systemd） |
| Python | 3.10+，含 venv 与 ensurepip |
| 构建 | git、gcc/g++ >= 9 或 clang++ >= 11、make、CMake >= 3.15 |
| Rust | 1.91.1+（构建 `ov` CLI 与 `ragfs-python` 原生绑定） |
| Python 构建依赖 | setuptools、setuptools-scm、wheel、maturin（部署脚本在 venv 内安装） |
| Node.js | 可选，仅用于构建 Web Studio（`/studio`） |
| 模型 | Embedding 与 VLM 服务可访问；API key 有效 |
| 磁盘 | 构建缓存至少 5 GiB 余量（源码、Rust 编译、wheel、venv） |

默认路径布局：

```text
/opt/openviking/                                  # 安装前缀
/opt/openviking/releases/<commit12>/              # 每个 commit 一个 release
/opt/openviking/releases/<commit12>/venv/         # 独立 Python venv
/opt/openviking/current -> releases/<commit12>    # 原子切换的当前版本
/etc/openviking/ov.conf                           # 服务配置（0640 root:<运行组>）
/var/lib/openviking/                              # 数据根目录
/var/lib/openviking/data/                         # 默认 storage.workspace
/var/lib/openviking/run/openviking.pid            # --no-systemd 模式的 pid
/var/log/openviking/server.log                    # --no-systemd 模式的日志
/var/cache/openviking-deploy/<run_id>/            # 构建缓存（root:root 0700）
/var/log/openviking-deploy/<run_id>/env/deploy.env
```

## 2. 版本策略

- **未指定版本时使用 `master`**，不自动选择 release tag。上游默认分支若已改名为 `main`，
  则按克隆后的默认分支安装，避免因为分支名不一致导致 checkout 失败。
- `master`/默认分支是移动分支：部署完成后必须把 `git rev-parse HEAD` 写入 `deploy.env` 的
  `OPENVIKING_COMMIT`，同时记录 `OPENVIKING_BRANCH` 与 `OPENVIKING_PACKAGE_VERSION`。
- 指定 tag（如 `v0.3.22`）、分支或 commit 时按 ref checkout；tag 与 commit 更适合可复现环境。
- 离线部署用 `--source-dir`，使用目录当前内容；目录自带版本，不能再指定非 master 的 `--version`。
- 已有预编译 wheel 时用 `--wheel`；wheel 的哈希会写入 release 标识，不与 master 版本声明混用。
- 上游推荐的普通用户路径是 `uv tool install openviking --upgrade`；本技能按部署需求默认源码构建，
  不把预编译包当作 master。

## 3. 源码与构建路径

### 3.1 获取源码

默认执行等价于：

```bash
git clone https://github.com/volcengine/OpenViking.git <build>/src
# 默认分支为 master 时 checkout master；否则保留克隆后的默认分支（上游当前为 main）
git -C <build>/src rev-parse --abbrev-ref HEAD
git -C <build>/src rev-parse HEAD
```

指定 tag/分支/commit 时 checkout 对应 ref；commit 不在默认分支时脚本会追加一次 `git fetch`。
克隆与构建都在 root 私有的 `/var/cache/openviking-deploy` 下进行，完成后保留现场供排查。

### 3.2 工具链检查

```bash
python3 --version
g++ --dumpversion            # 或 clang++ --version
cmake --version
cargo --version
```

版本不足时先修复工具链，再重跑部署；不要用跳过 `ragfs` 构建的方式掩盖失败。

### 3.3 Rust 安装（可选，显式开关）

脚本默认要求机器已预装 Rust 1.91.1+。只有显式加 `--install-rust` 时才安装：

1. 按架构下载官方 `rustup-init` 与同名 `.sha256`
2. 本地计算 sha256 并与官方校验值比对
3. 校验通过后以 `--profile minimal --default-toolchain 1.91.1` 安装到 `/usr/local/rustup` 与 `/usr/local/cargo`
4. 写入 `/etc/profile.d/rust.sh` 供后续 shell 使用

禁止把远程安装脚本直接管道给 shell，也禁止关闭 TLS 校验。

### 3.4 Python 安装

1. 在 release 目录创建 venv：`python3 -m venv <release>/venv`
2. 升级 `pip/setuptools/wheel`，安装 `setuptools-scm` 与 `maturin`
3. 使用 `pip wheel --no-deps --no-build-isolation` 构建 wheel（保留产物）
4. 使用 `pip install --upgrade <wheel>` 安装运行时依赖
5. 校验 `openviking-server`、`ov`、`openviking` 三个入口存在

`--no-build-isolation` 让构建使用 venv 内已安装的 maturin 与系统 cargo，避免构建隔离环境再次联网。
`--pip-index-url` 可指向企业内网 pip 源，但不得把用户名/密码放在 URL 中。

### 3.5 Web Studio

- 默认：检测到 `npm` 时构建，否则跳过并告警；`/studio` 可能返回 404。
- `--with-studio`：安装 Node.js/npm 后构建。
- `--skip-studio`：强制跳过，适合最小部署。

## 4. 模型配置

OpenViking 服务端至少需要 `embedding.dense` 与 `vlm`，以及 `storage.workspace`。
部署前必须与用户确认 provider、模型名、API Base、workspace 与 API key 交付方式，禁止猜测。

### 4.1 生成配置（共享 provider）

适用于 Embedding 与 VLM 使用同一 provider/API key/API Base 的场景：

```bash
bash scripts/install_openviking.sh \
  --provider volcengine \
  --embedding-model doubao-embedding-vision-251215 \
  --vlm-model doubao-seed-2-0-lite-260428 \
  --dimension 1024 \
  --api-key-file /root/.openviking_api_key
```

生成的配置结构：

```json
{
  "service_name": "openviking-server",
  "server": { "host": "127.0.0.1", "port": 1933 },
  "storage": {
    "workspace": "/var/lib/openviking/data",
    "agfs": { "backend": "local" },
    "vectordb": { "name": "context", "backend": "local" }
  },
  "embedding": {
    "dense": {
      "provider": "volcengine",
      "api_base": "https://ark.cn-beijing.volces.com/api/v3",
      "api_key": "<从文件读取>",
      "model": "doubao-embedding-vision-251215",
      "dimension": 1024
    }
  },
  "vlm": {
    "provider": "volcengine",
    "api_base": "https://ark.cn-beijing.volces.com/api/v3",
    "api_key": "<从文件读取>",
    "model": "doubao-seed-2-0-lite-260428"
  }
}
```

provider 默认 API Base：

| provider | 默认 API Base |
|----------|---------------|
| volcengine | `https://ark.cn-beijing.volces.com/api/v3` |
| openai | `https://api.openai.com/v1` |
| ollama | `http://127.0.0.1:11434` |
| azure | 必须显式 `--api-base` |

Ollama 可省略 API key；其他 provider 没有 key 时脚本直接失败，不写入占位 key。

### 4.2 接管已有配置

用户已经通过 `openviking-server init` 或手工写好配置时：

```bash
bash scripts/install_openviking.sh --ov-conf /root/ov.conf
```

脚本会校验 JSON、`embedding.dense`、`vlm`、`storage.workspace`，备份旧配置后安装到
`/etc/openviking/ov.conf`。混合 provider（如 volcengine Embedding + Codex/Kimi/GLM VLM）
应使用这种方式，不要用共享参数强行生成。

### 4.3 交互式向导

需要 TTY 的向导：

```bash
/opt/openviking/current/venv/bin/openviking-server init
```

向导默认写入运行用户 home 下的 `.openviking/ov.conf`；把它复制到 root 可读位置后，
再用 `--ov-conf` 重新执行安装脚本接管。不要在非交互脚本里期待向导自动完成。

### 4.4 非回环监听与鉴权

默认监听 `127.0.0.1`。监听 `0.0.0.0` 或其他网卡地址时必须提供 `--root-api-key-file`，
脚本会写入 `server.root_api_key`；否则服务会因开发模式限制拒绝启动。

## 5. systemd 与运行用户

- 默认创建系统用户 `openviking`（nologin），服务以该用户运行，不使用 root。
- 配置目录 `/etc/openviking` 为 `root:<运行组> 0750`，`ov.conf` 为 `0640`。
- 数据目录与 workspace 属于运行用户；服务 unit 设置 `HOME` 与 `OPENVIKING_CONFIG_FILE`。
- unit 使用 `Restart=always`、`RestartSec=5`、`LimitNOFILE=65535`。
- 覆盖已有 unit 前生成 `.bak.<时间戳>`，然后 `systemctl daemon-reload` 并重启。
- `--no-systemd` 时使用 `nohup` 后台进程与 pid 文件，日志写入 `/var/log/openviking/server.log`；
  该模式不提供开机自启与自动重启。

## 6. 验收

```bash
systemctl is-active openviking
ss -lntp | grep ':1933'
curl -fsS http://127.0.0.1:1933/health
curl -fsS http://127.0.0.1:1933/ready
/opt/openviking/current/venv/bin/openviking-server doctor --config /etc/openviking/ov.conf
```

判定：

- `/health`：进程存活，返回 `status: ok` 且 `healthy: true`
- `/ready`：依赖就绪，返回 `status: ready`；模型不可用时会给出具体检查项
- `doctor`：配置、模型访问与认证检查通过，退出码 0
- `deploy.env`：`DEPLOY_STATUS=ok`、`OPENVIKING_HEALTH=ok`、`OPENVIKING_READY=ok`

`/health` 正常不代表模型可用；不要用 `/health` 单独证明部署成功。

## 7. 升级与回滚

升级流程：

1. 新 commit/tag 构建到新的 `releases/<commit12>`，不覆盖旧 release
2. 安装新配置（旧配置先备份）
3. `doctor` 通过后停止旧服务
4. 原子切换 `current` 符号链接
5. 启动并复验 `/health`、`/ready`

回滚流程：

1. 停止服务
2. 把 `current` 指回旧 release
3. 恢复 `ov.conf` 与 unit 的 `.bak.<时间戳>`
4. 启动并复验 `/health`、`/ready`
5. 旧 release 已被删除时，使用 `deploy.env` 记录的 `OPENVIKING_WHEEL` 重新安装；
   无法恢复时明确说明不可回滚，不得伪造成功

## 8. 离线与内网

| 场景 | 做法 |
|------|------|
| 已有源码 | `--source-dir DIR`，要求 root 属主且组/其他不可写 |
| 已有 wheel | `--wheel FILE`，要求 root 属主且组/其他不可写 |
| 内网 pip 源 | `--pip-index-url URL`，URL 不得携带用户名/密码 |
| 无外网 Rust | 提前安装 Rust 1.91.1+，或准备可校验的 rustup 离线包 |
| 无 npm | 加 `--skip-studio`，接受 `/studio` 不可用 |

离线源码仍需要 Python 依赖与 Cargo crates 可获取；完全无网络时应先准备 wheelhouse、
内网 pip 源以及 cargo vendor/registry 镜像。不要用跳过依赖安装来假装离线部署完成。

## 9. 安全说明

- API key 只从 600 文件或用户配置读取，不进入 argv、日志、`deploy.env`。
- 生成的 `/etc/openviking/ov.conf` 为 `0640 root:<运行组>`；不要把权限放宽到 0644。
- 构建根 `root:root 0700`，父目录不得被普通用户控制；源码目录复制后再次校验属主与写权限。
- 路径必须是绝对路径，拒绝 `.`、`..`、`//`；归一化后拒绝系统级目录。
- Git URL 仅支持 http(s)://；本地源码必须走 `--source-dir`，禁止 `file://`。
- Rust 安装使用官方 `.sha256` 校验，禁止关闭 TLS 校验。
- 不自动改防火墙、SELinux、内核参数；需要对外开放时另行评估反向代理与鉴权。

## 10. 故障排查

| 症状 | 排查 |
|------|------|
| `pip wheel` 失败 | 查看 `logs/build.log`；确认 cargo、maturin、CMake、g++；不要跳过 ragfs |
| `cargo` 版本不足 | 用 `--install-rust` 或手工升级到 1.91.1+ |
| 构建根目录权限被拒绝 | 使用默认 `/var/cache/openviking-deploy`，或修正为 root:root 0700 |
| 源码目录被拒绝 | 改为 root 属主且组/其他不可写；不要从普通用户目录以 root 构建 |
| `ov.conf` 校验失败 | 补齐 `embedding.dense`、`vlm`、`storage.workspace`；检查 JSON 与 key 文件权限 |
| `doctor` 失败 | 查看 `logs/doctor.log`；检查 api_base、模型名、API key、网络与 provider |
| `/health` 超时 | 查看 `journalctl -u openviking` 或 `/var/log/openviking/server.log`；检查端口与启动日志 |
| `/ready` 非 ready | 检查 Embedding 服务可达性、key、模型与维度；`/health` 不能替代 |
| 非回环启动被拒绝 | 配置 root API key，或改回 127.0.0.1 |
| 端口被占用 | `ss -lntp` 定位；停旧实例或改 `--port` |
| `current` 不是符号链接 | 人工确认后移走，避免覆盖未知目录 |
| 升级后异常 | 回滚 `current` 与配置备份，复验后再排查新版本 |
| Web Studio 404 | 未安装 npm 或被 `--skip-studio` 跳过；用 `--with-studio` 重跑 |

## 11. 与其它部署方式的边界

- Docker/Compose、Helm、托管服务：使用官方文档，不在本技能范围。
- 客户端 `ov` CLI 接入、agent 插件安装：使用官方 CLI/集成指南。
- 性能调优、容量规划、可观测性栈：部署完成后单独进行。
