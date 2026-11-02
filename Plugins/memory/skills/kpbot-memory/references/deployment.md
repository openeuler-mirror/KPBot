# 部署后端与复现边界

如果已有可用服务，只需[接入指南](<getting-started.md>)，不要再部署一套。客户端仅需 Python 与 `httpx`；本页的服务、数据库、模型、镜像和持久目录由部署方另行管理。安装 skill 或运行 configure/status 不会准备这些资源。

本页依据以下固定基线的官方材料与接口整理；链接供部署时查阅，本页不是已验证的全栈一键安装器：

| 后端 | 基线 | 部署资源与本地已验证范围 |
|---|---|---|
| OpenViking | Python 包 0.4.22 | 服务、持久目录、账号/用户 key、embedding。已验证开启鉴权、本地 BGE、vectors_only；语义模型能力不能据此推定通过。 |
| mem0 | 源码 v2.2.1 + 容器内 mem0ai==2.2.1 | Docker Compose、PostgreSQL/pgvector、embedding；可选 LLM。已验证开启鉴权的原文链路和双用户隔离，未验证真实 LLM 提炼成功。 |
| TencentDB Agent Memory | v2.0.3 | Node.js、SQLite FTS5、本地持久目录与网关 key。已验证独立 MemoryCore 的 BM25 + L0 原文链路，无 embedding/LLM 提炼。 |

凭证使用隐藏交互输入、受保护配置文件或部署密钥管理，不写入命令行参数、仓库或共享日志。远程服务传输凭证需 HTTPS；OpenViking 当前 KPBot 适配器仅接受回环地址。不要把本地测试的绝对路径、模型缓存或数据库卷当作其他机器已具备的资源。

## 1. OpenViking 0.4.22

参考：[官方部署文档](https://docs.openviking.ai/en/guides/03-deployment)、[服务端配置](https://docs.openviking.ai/en/getting-started/03-quickstart-server)、[认证说明](https://docs.openviking.ai/en/guides/04-authentication)。本地安装的 0.4.22 官方包 README 提供 `init`、`doctor`、启动命令；使用固定版本，不以无版本的 upgrade 命令作为复现依据。

在已有 Python 3.10+ 的机器上单独安装服务，不与轻量客户端环境混用：

```bash
OV_VENV="$HOME/.local/share/kpbot-memory/openviking-server"
uv venv --no-python-downloads --python python3 "$OV_VENV"
uv pip install --python "$OV_VENV/bin/python" 'openviking==0.4.22'
"$OV_VENV/bin/openviking-server" init
```

交互配置会准备默认用户目录下的服务配置。按官方说明选择实际可用的 embedding/VLM provider、模型与持久目录，保护配置文件，并按认证文档启用 API key、创建账号和普通用户。不要把 root key 交给业务客户端。配置好后核验并显式绑定回环地址：

```bash
"$OV_VENV/bin/openviking-server" doctor
"$OV_VENV/bin/openviking-server" --host 127.0.0.1 --port 1933
```

若使用自选配置位置，可通过该版本的 `--config` 指定路径。doctor 是供应商工具，不是 KPBot status；模型连接检查与真实任务写入仍要分别验证。

本地 CPU embedding 不是只装 OpenViking 就完成：本次开发验证还单独准备了 BGE small zh v1.5 GGUF、相应本地推理依赖及模型配置。模型文件、下载权限、磁盘与 CPU/内存都需另行准备；本机缓存不会随 skill 分发。`vectors_only` 不调用语义摘要来代替原文，但仍需要向量生成服务可用。

服务启动后，用普通用户身份完成[客户端配置及合成往返](<getting-started.md>)。CLI 返回 ready 不等于真实 LLM 推理已验证。

## 2. mem0 2.2.1 self-hosted REST

参考：[v2.2.1 官方服务 README](https://github.com/mem0ai/mem0/blob/v2.2.1/server/README.md)。这是独立 REST 服务，不是只安装 Python SDK。官方服务使用 Docker；API 入口通常是 8888，数据库是 PostgreSQL/pgvector，默认模型配置需有效 embedding 与模型 provider 配置。

### 选择明确的源码与凭证

在部署工作目录获取官方 v2.2.1（该版本对应 commit `94c3fe9f238f3dbf29c9ce98643bd71eb13077cd`）：

```bash
git clone --branch v2.2.1 --depth 1 https://github.com/mem0ai/mem0.git mem0-service
cd mem0-service/server
umask 077
cp .env.example .env
chmod 600 .env
```

在本机私密编辑器中填写 `.env` 的 `POSTGRES_PASSWORD`、`JWT_SECRET`、服务支持的 `ADMIN_API_KEY` 和模型 provider 所需凭证；保持 `AUTH_DISABLED=false`。不要把值放进 make 参数或 shell 历史。若采用官方 dashboard 创建普通 API key 的流程，由操作员在私密界面保存 key，本文不要求把凭证打印到共享终端。

首次 KPBot 连接设置 `derive_memories=false`，但仍需 embedding 和向量存储。即使不提炼，服务端 provider 初始化也可能要求模型配置存在。默认可使用服务 README 支持的 provider；如果改为本地 embedding，还要部署模型服务，并保证实际向量维度、pgvector collection 配置一致。我们的 BGE 512 维、额外模型容器是专用开发配置，不是官方默认 Compose 自带资源。

### 固定实际 SDK，而不只固定源码标签

此标签的服务依赖允许 `mem0ai>=0.1.48`，默认 Compose 启动命令还会无版本重装 `mem0ai`。因此源码 v2.2.1 不保证运行时 SDK 就是 2.2.1。部署方需固定依赖与启动命令，并核验容器内版本。

例如在 `server` 目录创建本机专用覆盖文件 `kpbot-local.yaml`，使用支持 `!override` 的 Docker Compose，固定 SDK、只发布回环端口，不关闭鉴权：

```yaml
services:
  mem0:
    command: >-
      sh -ec 'pip install --quiet --force-reinstall --no-deps mem0ai==2.2.1 &&
      python -c "import importlib.metadata; assert importlib.metadata.version(\"mem0ai\") == \"2.2.1\"" &&
      alembic upgrade head &&
      exec uvicorn main:app --host 0.0.0.0 --port 8000'
    ports: !override
      - "127.0.0.1:8888:8000"
    environment:
      AUTH_DISABLED: "false"
      MEM0_TELEMETRY: "false"
    restart: "no"
  postgres:
    ports: !override
      - "127.0.0.1:8432:5432"
    restart: "no"
```

这是一份供部署方审阅的版本/端口覆盖示例，不包含模型部署或完整镜像锁定，也未替代从零部署验收。然后启动、核验；不要输出完整容器环境：

```bash
docker compose --project-name kpbot-mem0 \
  -f docker-compose.yaml -f kpbot-local.yaml up -d --build postgres mem0
docker compose --project-name kpbot-mem0 \
  -f docker-compose.yaml -f kpbot-local.yaml exec -T mem0 \
  python -c 'import importlib.metadata; print(importlib.metadata.version("mem0ai"))'
```

应看到 SDK 2.2.1，再使用[接入指南](<getting-started.md>)创建含 `api_key` 的客户端配置并运行带进度的 status、合成往返。仅能读取公开 OpenAPI 不算认证接入成功；KPBot 还会访问受保护的 scoped 只读接口。

已有 PostgreSQL 15 数据升级到官方 PostgreSQL 17 镜像时，先按官方迁移说明备份和恢复；不能直接复用旧数据目录。不要使用 `docker compose down -v` 当作普通停止命令。启动和停止现有 Docker daemon、是否开机自启，均是部署方决定，不由 KPBot 操作。

## 3. TencentDB Agent Memory v2.0.3

参考：[v2.0.3 MemoryCore README](https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/v2.0.3/MemoryCore/README.md)。本适配器连接独立 MemoryCore HTTP 网关，不要求安装 OpenClaw 或运行 Agent，也不要求腾讯云 VectorDB 才能测试原文链路。

### 版本与安装

需要 Node.js >=22.16.0、npm，以及带 SQLite FTS5 的 Node 构建；仅满足 Node 版本号不保证 FTS5 存在。本地基线使用 Node 24.18.0；换用其他构建时，以健康响应 `stores.ftsAvailable` 为准。

```bash
git clone --branch v2.0.3 --depth 1 \
  https://github.com/TencentCloud/TencentDB-Agent-Memory.git tencentdb-memory-service
cd tencentdb-memory-service/MemoryCore
npm install --ignore-scripts
```

v2.0.3 对应 commit `084bed45cad6bb7f877455082cee82835d65671a`。该标签内部版本元数据不一致，不能仅凭服务 `/health` 的 `version:"0.1.0"` 或 package 版本识别源码版本。

官方 `npm run build` 仍引用未分发的 seed-v2 编译配置，可能以 TS5058 失败；[上游 issue #716](https://github.com/TencentCloud/TencentDB-Agent-Memory/issues/716)、[#759](https://github.com/TencentCloud/TencentDB-Agent-Memory/issues/759)记录了同类问题。它不是用户漏填服务配置。不要补造文件来假装完整构建通过；官方 README 提供的 tsx 源码入口可以单独启动网关。安装时跳过附加脚本不等于修复了完整构建。

### 先验证无 LLM 的原文 + BM25 模式

部署方可参考官方 standalone 模板；下面是明确关闭提炼的本机开发配置，保存为受保护的 `kpbot-gateway.json`。数据目录用你的用户目录，勿指向现有生产数据：

```json
{
  "deployMode": "standalone",
  "stateBackend": "local",
  "instanceId": "kpbot-local",
  "server": {"host": "127.0.0.1", "port": 8420},
  "data": {"baseDir": "~/.local/share/kpbot-memory/tencentdb-data"},
  "llm": {
    "baseUrl": "http://127.0.0.1:9/v1",
    "apiKey": "",
    "model": "disabled-for-original-storage-test"
  },
  "memory": {
    "capture": {"enabled": true},
    "extraction": {"enabled": false},
    "llm": {"enabled": false},
    "pipeline": {"enableWarmup": false},
    "storeBackend": "sqlite",
    "embedding": {"provider": "none"},
    "bm25": {"enabled": true, "language": "zh"}
  },
  "skill": {"enabled": false}
}
```

设置网关 key 时使用隐藏输入，不放入 argv，然后启动官方源码入口：

```bash
chmod 600 kpbot-gateway.json
read -rsp 'Gateway API key: ' TDAI_GATEWAY_API_KEY; printf '\n'
export TDAI_GATEWAY_API_KEY
export TDAI_GATEWAY_CONFIG="$PWD/kpbot-gateway.json"
node --import tsx src/gateway/server.ts
```

key 必须是部署方生成并安全保存的非空值，后续通过客户端 configure 的隐藏输入填入同一个值；不是每次启动随机换 key。另一个终端可读取不含凭证的健康端点：

```bash
curl --fail --silent --show-error http://127.0.0.1:8420/health
```

预期 `status:"ok"`，BM25 模式 `stores.ftsAvailable:true`。客户端配置的 `service_id` 应为 `kpbot-local`，其 Bearer key 必须匹配服务。健康端点不验证该凭证，仍须完成客户端 status 和合成读写。v2.0.0–v2.0.2 健康响应缺少适配器依赖的字段，不是本指南基线。

需要 LLM 提炼时，另行配置真实 provider、启用相关处理并验证；上面故意使用的回环占位 endpoint 不可用来提炼。L0 接口成功不能宣称完整的 L1–L3 记忆能力已验证。

## 4. 源码 checkout 与已安装 skill 的区别

已安装 skill 包含客户端入口和本页，但不包含上述官方后端源码、数据库卷或模型。KPBot 仓库测试及 OpenViking 开发 fixture helper 只用于完整源码 checkout；helper 位于源码根目录的 `tools/memory/setup-openviking.py`，不随 npm 包或 skill 安装分发。不要从已安装 skill 向上寻找仓库根目录后调用源码工具。

服务验收至少记录：源码/SDK 版本、实际模型与维度、认证模式、配置路径、持久目录、成功原文来源标识，以及哪些测试明确跳过。关闭客户端、停止服务、删除数据是不同操作；不要用清空数据库代替结束测试。
