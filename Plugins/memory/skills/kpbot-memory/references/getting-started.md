# 接入已有记忆服务

本指南假定后端服务已部署，并由部署方提供地址、凭证和身份。没有服务时先看[部署说明](<deployment.md>)。KPBot 配置向导只创建客户端配置，不安装或启动后端，也不创建后端账号。

## 1. 准备客户端入口

需要本机已有 Python 3.10+ 和 uv。以下是 Bash 命令；把 `SKILL_ROOT` 设为实际安装目录，例如项目的 `.claude/skills/kpbot-memory` 或 `.opencode/skills/kpbot-memory`，全局安装则使用对应工具的全局 skills 目录。

```bash
SKILL_ROOT="$PWD/.claude/skills/kpbot-memory"  # 按实际安装位置修改
MEM=(uv run --no-project --no-python-downloads --python python3 --with httpx==0.28.1
     "$SKILL_ROOT/scripts/kpbot-memory.py")
"${MEM[@]}" --help
```

这组参数使用现有 Python，为本次客户端准备固定版本的 `httpx`，不需要激活环境，也不会自动下载 Python。uv 首次准备依赖可能需要网络，会自行显示实际安装进度；这不是后端启动进度。若没有满足要求的 Python，应先自行安装，再重试。数组传参可保留含空格的安装路径。

没有 uv 时，可使用独立 venv，勿安装到系统 Python：

```bash
python3 -m venv "$HOME/.local/share/kpbot-memory/client-venv"
"$HOME/.local/share/kpbot-memory/client-venv/bin/python" -m pip install httpx==0.28.1
MEM=("$HOME/.local/share/kpbot-memory/client-venv/bin/python"
     "$SKILL_ROOT/scripts/kpbot-memory.py")
```

npm 安装提供的等价入口是 `kpbot memory ...`；它使用当前 `python3`，不会自动准备依赖。以下统一用 `MEM` 数组，避免误用未安装 `httpx` 的解释器。

## 2. 交互创建私有配置

在真实交互终端执行，选择一个新文件路径：

```bash
MEM_BACKEND_CONFIG="$HOME/.config/kpbot-memory/mem0.json"
"${MEM[@]}" configure --backend mem0 --config "$MEM_BACKEND_CONFIG"
```

`--backend` 可改为 `openviking` 或 `tencentdb`，并使用独立配置文件。向导依次询问服务地址、KPBot 用户、当前项目、允许项目列表及后端身份，要求明确确认项目范围，再隐藏输入 API key。不要把 key 写成命令行参数、环境中的业务 payload 或 shell 历史。

- stdin 和 stderr 必须是交互终端；不要用管道喂给向导。它不会退回明文回显凭证。
- 文件由当前 OS 用户所有、权限为 0600；向导可创建缺少的父目录，但拒绝覆盖已有文件或符号链接。修改已有配置可在本机私密编辑器中进行，再保持 0600；不要打印整份配置。
- OpenViking 默认 `vectors_only`；mem0 首次配置固定 `derive_memories=false`，先验证原文链路。
- 成功 JSON 中 `connection_verified` 为 false：只代表配置文件已创建。向导没有联网验证，也没有确认模型可用。

## 3. 检查连接并理解进度

```bash
"${MEM[@]}" backend status --config "$MEM_BACKEND_CONFIG" --timeout 30 --progress
```

检查 stdout 的 JSON：必须同时满足 `ok:true` 和 `data.state:"ready"`。若失败，保留 `error.code`、安全错误说明及已知 `source_id`，按提示排查。

`--progress` 在 stderr 显示 initialize、所选操作和 close 阶段，以及真实已等待秒数；不是完成百分比，也不表示模型正在生成多少 token。stderr 是终端时默认启用；重定向或程序调用时默认关闭，可显式使用 `--progress` 或 `--no-progress`。stdout 始终保留单个最终 JSON，可分别保存：

```bash
"${MEM[@]}" backend status --config "$MEM_BACKEND_CONFIG" --progress \
  > status-result.json 2> status-progress.log
```

每次 CLI 都新建客户端、初始化、执行一项操作并关闭。`initialize` 不是必须先运行一次的持久登录；`close` 只释放该次客户端，不停止后端服务。`--timeout` 默认 30 秒，initialize 与所选异步操作共用预算；关闭清理不包含在其中。进度中的 close 等待不是新一次写入。

**ready 不等于 LLM 提炼就绪。** mem0 会验证受保护的只读访问，但不测试 embedding 或 LLM 推理；TencentDB 检查存储与检索能力，不检查每条记忆的加工进度；OpenViking 检查配置模式所需的服务就绪信息，也不能替代一次实际读写验证。

## 4. 可选：明确选择后写入合成样例

以下操作会留下测试记录；仅在你确认使用开发服务和项目后执行。它不是自动初始化步骤，样例也不是已验证的优化经验。假设配置允许 `project-a`：

```bash
umask 077
mkdir -p "$HOME/.local/state/kpbot-memory/smoke"
cd "$HOME/.local/state/kpbot-memory/smoke"
cat > remember.json <<'JSON'
{
  "kind": "task_process",
  "project_id": "project-a",
  "task_id": "synthetic-first-connection",
  "content": [
    {"role": "user", "content": "合成接入测试：检查循环优化记忆接口"},
    {"role": "assistant", "content": "仅验证原文读写，不包含真实性能结论。"}
  ]
}
JSON
cat > recall.json <<'JSON'
{
  "query": "合成接入测试 循环优化",
  "project_ids": ["project-a"],
  "kinds": ["task_process"],
  "limit": 5
}
JSON
"${MEM[@]}" backend remember --config "$MEM_BACKEND_CONFIG" --input remember.json --progress
"${MEM[@]}" backend recall --config "$MEM_BACKEND_CONFIG" --input recall.json --progress
```

写入成功时记录返回的 `source_id` 和 `processing`；在召回结果中核对该来源及完整内容，不只看是否有任意命中。审核经验用 `kind:"experience"`；经验必须由用户确认，不能把本例改名后当作真实性能证据保存。

`ok:true` 的保存表示原文已完整保存。`processing` 的 ready/pending/failed/unknown 描述后续处理，不能把 unknown 或 failed 说成提炼完成。`WRITE_UNKNOWN` 表示可能已写入，**不要自动重提**；先凭 `source_id` 和后端记录调查。当前北向接口没有删除、列举或按 ID 读取命令，测试记录的清理由部署方使用原生管理能力决定。

## 5. 三种配置模板与身份

下列都是完整的五字段 JSON 模板；占位符不是真实凭证。优先使用向导隐藏输入 key；手动填写时在私密编辑器中保存，不用携带 secret 的 shell 命令。配置属于当前 OS 用户、必须为 0600、不得为符号链接。能改写该文件的人拥有配置管理权限，不能把它当作不同不可信租户之间的隔离边界。

### OpenViking 0.4.22

```json
{
  "backend": "openviking",
  "user_id": "user-01",
  "current_project": "project-a",
  "allowed_projects": ["project-a", "project-b"],
  "options": {
    "endpoint": "http://127.0.0.1:1933",
    "api_key": "<user-api-key>",
    "account_id": "<existing-account-id>",
    "user_id": "<existing-provider-user-id>",
    "processing_mode": "vectors_only",
    "max_input_bytes": 1048576
  }
}
```

顶层 `user_id` 是 KPBot 记录归属；`options.user_id` 是部署方已创建的 OpenViking 用户，可不同，但必须与 key、account 一致。不要使用 root key 充当普通用户 key。此适配器目前只接受回环 HTTP(S) 地址，端口需与实际服务一致；1933 是此向导的建议值，不是所有 OpenViking 部署的默认端口。`vectors_only` 仍需要可用 embedding；改为 `semantic_and_vectors` 前，先单独验证服务的语义处理依赖。

### mem0 2.2.1 self-hosted REST

```json
{
  "backend": "mem0",
  "user_id": "user-01",
  "current_project": "project-a",
  "allowed_projects": ["project-a", "project-b"],
  "options": {
    "endpoint": "http://127.0.0.1:8888",
    "api_key": "<service-supported-api-key>",
    "derive_memories": false,
    "max_input_bytes": 1048576
  }
}
```

key 使用官方 `X-API-Key` 认证，可由服务管理方提供 API key 或管理 key。顶层用户及项目用于适配器命名空间，不会自动创建 mem0 登录账号。不要为绕过鉴权错误而关闭服务鉴权。只有明确采用本机 `AUTH_DISABLED=true` 开发服务的手动配置才可省略 `api_key`；向导不会创建这种无凭证配置。

原文存储和检索也需要服务的 embedding 与向量库。首次保持 `derive_memories=false`；确认原文链路后，只有服务的 LLM 已验证可用且你明确需要提炼时，才改为 true。保存成功但 `processing=failed` 仍可能表示提炼失败；提炼超过时限但原文已确认时会保留保存成功并报告 unknown。

### TencentDB Agent Memory v2.0.3

```json
{
  "backend": "tencentdb",
  "user_id": "user-01",
  "current_project": "project-a",
  "allowed_projects": ["project-a", "project-b"],
  "options": {
    "endpoint": "http://127.0.0.1:8420",
    "api_key": "<gateway-bearer-key>",
    "service_id": "<existing-instance-id>",
    "agent_id": "kpbot",
    "chunk_chars": 8000,
    "max_input_bytes": 2097152
  }
}
```

`service_id` 对应真实实例身份，适配器发送 `x-tdai-service-id` 与 Bearer 认证；`agent_id` 是固定后端 Agent 维度，不能由业务 payload 切换。KPBot 项目、用户和任务映射为原生 team/user/task 范围。当前链路存取 L0 原文，`processing=unknown` 不代表 LLM 提炼已完成。BM25 模式需要 SQLite FTS5，不必另配 embedding。

公共层从顶层配置注入 `owner_id`，业务记录不要携带其他人的 owner、任意 sources 或共享权限。当前仅支持 private，同用户可跨配置允许的项目检索；请求共享会明确返回 UNSUPPORTED。`max_input_bytes` 还约束完整序列化封装，元数据也占空间；不要通过静默截断来规避上限。

## 6. 常见故障

| 返回或现象 | 下一步 |
|---|---|
| 找不到 Python / uv，或缺少 `httpx` | 先完成第 1 节；后端状态命令不安装客户端依赖。 |
| configure 拒绝非交互输入或已有路径 | 在真实终端运行，使用新路径；不要改为明文 argv 传 key。 |
| ACCESS_DENIED | 检查文件 owner/0600、凭证、后端身份和项目范围；不要打印 key 排错。 |
| UNAVAILABLE | 检查服务实际地址、进程和必要存储/模型；TencentDB 使用 v2.0.3 基线。 |
| TIMEOUT / 持续 waiting | 查看服务延迟和资源，再决定是否调整时限；不代表命令卡死或已写入。 |
| WRITE_UNKNOWN | 保存来源标识，调查后端，不自动重试写入。 |
| READ_FAILED | 检查命中原文是否仍可读取；不能当成无命中。 |
| TOO_LARGE | 核对输入及封装限制，使用后端支持的大小；不要删减正文后声称原文完整。 |
| ready 但提炼失败 | 分开检查原文、索引与 LLM；status 不执行一次真实 LLM 提炼。 |

## 7. 安装内容与源码开发边界

已安装 skill 自包含客户端、接入指南和部署说明，不包含 KPBot 仓库测试或 OpenViking 开发 fixture helper。npm 包同样不包含该 helper；它仅位于完整源码 checkout 的 `tools/memory/setup-openviking.py`。不要从安装目录推导仓库路径或尝试调用源码工具。

普通服务接入使用本文的 configure、status 和合成往返步骤即可。部署资源与服务验收边界见[部署说明](<deployment.md>)。
