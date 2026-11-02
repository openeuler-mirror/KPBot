# 记忆后端开发指南（OpenViking、mem0、TencentDB）

本指南说明现有三个记忆后端的代码组织、共同契约、验证方式和新增后端的接入点。接口以[后端契约](<backend-contract.md>)为准，也可查看 [HTML 版](<backend-contract.html>)。用户接入说明随 skill 分发，见[接入指南](<../skills/kpbot-memory/references/getting-started.md>)和[部署说明](<../skills/kpbot-memory/references/deployment.md>)；本页面向源码开发者。

## 1. 代码与文档位置

以下路径均相对 KPBot 仓库根目录。命令在完整源码 checkout 的根目录运行。

| 组件 | 路径 |
|---|---|
| 客户端入口 | `Plugins/memory/skills/kpbot-memory/scripts/kpbot-memory.py` |
| CLI 顶层分派 | `Plugins/memory/skills/kpbot-memory/scripts/kpbot_memory/cli/__init__.py` |
| 一次性 backend CLI 与工厂 | `Plugins/memory/skills/kpbot-memory/scripts/kpbot_memory/cli/backend.py` |
| 配置向导 | `Plugins/memory/skills/kpbot-memory/scripts/kpbot_memory/cli/configure.py` |
| 公共层（北向门面） | `Plugins/memory/skills/kpbot-memory/scripts/kpbot_memory/northbound.py` |
| 配置加载与校验 | `Plugins/memory/skills/kpbot-memory/scripts/kpbot_memory/backend_config.py` |
| OpenViking 适配器 | `Plugins/memory/skills/kpbot-memory/scripts/kpbot_memory/backends/openviking_http.py` |
| mem0 适配器 | `Plugins/memory/skills/kpbot-memory/scripts/kpbot_memory/backends/mem0_http.py` |
| TencentDB 适配器 | `Plugins/memory/skills/kpbot-memory/scripts/kpbot_memory/backends/tencentdb_http.py`（`TencentDBBackend`） |
| 公共层契约测试 | `tests/memory/test_northbound.py`（FakeBackend 定义了全部期望行为） |
| 配置校验测试 | `tests/memory/test_backend_config.py` |
| CLI 生命周期测试 | `tests/memory/test_cli_backend.py` |
| 配置向导测试 | `tests/memory/test_cli_configure.py` |
| OpenViking 开发 fixture helper | `tools/memory/setup-openviking.py`（仅源码 checkout） |

插件开发文档集中在 `Plugins/memory/docs/`；已安装 skill 的用户说明集中在 `references/`，客户端集中在 `scripts/`。OpenViking helper 位于 `Plugins/` 之外，不随 npm 包或 skill 安装分发。

## 2. 必须实现的南向接口（6 个 async 方法）

```python
async def initialize(self, options: dict, *, timeout=30.0) -> dict
async def status(self, *, timeout=30.0) -> dict
async def store_experience(self, record: dict, *, timeout=30.0) -> dict
async def store_process(self, record: dict, *, timeout=30.0) -> dict
async def search(self, request: dict, *, timeout=30.0) -> dict
async def close(self) -> None   # 成功必须返回 None
```

除 `close` 外，各方法返回统一信封，成功时 `{"ok": true, "data": ...}`，失败时
`{"ok": false, "error": {"code": ..., "message": ..., "source_id": ...}}`。`source_id`
是已知资源定位符（如写入已发生但结果不确定时必须带上）。

`options` 来自信任配置文件的 `options` 字段，由调用方显式提供；
`record` 恰好六个字段：`kind`（experience/task_process）、`project_id`、
`owner_id`、`task_id`、`visibility`（初版只接受 `private`）、
`content`（str / dict / list，必须原样可逆保存，不截断不摘要）。
`search` 的 `request`：`query`、`features`、`sources`（`[{project_id,
owner_id}]`，由公共层按信任配置生成，payload 无法伪造身份）、
`kinds`、`limit`（1..20）。返回 `data.items`，每项含 `content`、
`content_type`（`original` / `excerpt` / `derived`；审核经验必须为 `original`）、`kind`、`project_id`、
`owner_id`、`task_id`、`source_id`、`score`，另有 `partial` 标志。

错误码全集（`code` 字段取值）：`INVALID_ARGUMENT`、`TOO_LARGE`、
`UNSUPPORTED`、`ACCESS_DENIED`、`UNAVAILABLE`、`NOT_READY`、`TIMEOUT`、
`WRITE_UNKNOWN`、`READ_FAILED`、`BACKEND_ERROR`。

## 3. 行为契约要点

- 整调用时限：调用方给定 deadline，超时不得开始新的后端 IO；原文未完整确认时，已开始写入后的超时返回 `WRITE_UNKNOWN` 并保留已知 `source_id`，不得重试。原文已读回确认而后处理超时时，保留保存成功并返回 `processing=unknown`；公共层的 `confirmed_source_id` 取消标记约定见接口规范。显式调用方取消维持原有不确定写入语义。
- 写后确认：不能仅凭后端错误码推断未落盘；原文需读回比较。已知发生在原生写入处理前的鉴权拒绝可直接返回 `ACCESS_DENIED`；其余无法确认的写入返回 `WRITE_UNKNOWN`。参考三个适配器各自的 `_store` 与回归测试。
- 身份绑定：`owner_id`/项目范围由公共层核定；适配器只校验
  `record["owner_id"]` 与自身绑定一致。
- `initialize` 不得把健康检查当作完全 ready 的证明；返回的 `data`
  需含 `state`、`message`、`supports_shared`（初版为 `false`）。
- `close` 只释放本客户端持有的资源，不删除任何数据、不影响服务。

## 4. 接入公共层的两处改动

1. `backend_config.py` 的 `BACKEND_OPTIONS`：已有 `openviking`、`mem0`、`tencentdb` 的选项白名单与类型校验；新增后端时增加其显式允许的配置，不接受任意凭证或 HTTP 头透传。
2. `cli/backend.py` 的 `create_backend()`：已有三个后端的显式工厂分支；按 `config["backend"]` 创建相应对象。新增分支需同步配置、`cli/configure.py` 的配置向导和测试说明。

当前直接分派足以支持三个后端，不需要另外引入动态注册框架。

## 5. 验证方式

把新后端接到 `NorthboundMemory` 后，参照
`tests/memory/test_northbound.py` 中 FakeBackend 的全部用例做契约
测试；CLI 层参照 `tests/memory/test_cli_backend.py`，配置向导参照 `tests/memory/test_cli_configure.py`。在仓库根目录运行：

```bash
PYTHONDONTWRITEBYTECODE=1 <venv>/bin/python -m unittest \
  tests.memory.test_northbound tests.memory.test_backend_config \
  tests.memory.test_cli_backend tests.memory.test_cli_configure
```

CLI 手工冒烟：

```bash
<venv>/bin/python Plugins/memory/skills/kpbot-memory/scripts/kpbot-memory.py backend \
  initialize --config <信任配置> --timeout 120
```

配置文件要求：当前 OS 用户所有、0600、常规文件（非符号链接）、
严格 UTF-8 JSON、恰好五个字段（`backend`、`user_id`、
`current_project`、`allowed_projects`、`options`）。

### 完整回归与真实服务测试

下面命令及 `npm run test:*` 需要完整源码；npm 发布包和已安装 skill 不包含开发测试集。

```bash
bash tests/test_install_platform.sh  # 平台过滤与 memory 自包含安装回归
bash tests/test_memory_package.sh    # 实际 npm 包解压与入口冒烟
PYTHONDONTWRITEBYTECODE=1 <venv>/bin/python -m unittest discover -s tests/memory -p 'test_*.py'

# 使用已有 Python 和 uv 准备 httpx，不自动下载 Python。
uv run --no-project --no-python-downloads --python python3 --with httpx==0.28.1 \
  python -m unittest discover -s tests/memory -p 'test_*.py'
```

`tests/memory/` 覆盖 backend CLI 生命周期、配置向导、公共层契约和三个适配器；默认使用模拟 HTTP，真实服务测试需显式选择私有开发配置，合成记录会保留。OpenViking 设置 `KPBOT_OPENVIKING_LIVE_CONFIG` / `KPBOT_OPENVIKING_LIVE_SECOND_CONFIG`；TencentDB 设置 `KPBOT_TENCENTDB_LIVE_CONFIG` / `KPBOT_TENCENTDB_LIVE_SECOND_CONFIG`；mem0 设置 `KPBOT_MEM0_LIVE_CONFIG`，双用户隔离另需 `KPBOT_MEM0_LIVE_SECOND_CONFIG`，基础测试在内存配置中关闭提炼；提炼测试另需 `KPBOT_MEM0_LIVE_DERIVATION=1`，该独立用例在内存配置中启用提炼，要求服务具备可用 LLM。未设置相应条件的真实测试会跳过，不能据此宣称该模式验证通过。具体用例数量随开发变化，不在本文档固化。

安装回归验证 Claude/OpenCode 各自安装的 skill 集合。`kpbot-memory` 的自包含落盘可用临时目录单独验证：执行 `install.sh project claude <dir>`（或 `opencode`）后，`<dir>/.claude/skills/kpbot-memory/scripts/kpbot-memory.py` 可直接运行。

## 6. OpenViking 开发辅助工具（仅源码 checkout）

完整 KPBot checkout 的开发者可在仓库根目录先查看 helper 帮助；它可能下载依赖和模型，具体资源、Python 选择及命令以帮助为准：

```bash
python3 tools/memory/setup-openviking.py --help
```

该 helper 不随 npm 包或已安装 skill 分发。它不是三个后端的通用部署器，也不会让其他人自动拥有本机历史测试环境。用户部署步骤见 skill 内的[部署说明](<../skills/kpbot-memory/references/deployment.md>)。

## 7. 公共层与首版边界

公共层不依赖第三方库（标准库即可运行）；三个 HTTP 适配器均在初始化时延迟导入 httpx，已验证客户端版本为 0.28.1。mem0 可按配置生成并检索任务过程的派生记忆；OpenViking 与 TencentDB 当前返回原文。尚未支持跨用户共享（`include_shared=true` 返回 `UNSUPPORTED`）、北向 get/list/delete、自动会话采集。Python 最低 3.10，单元测试使用标准库 unittest；真实测试必须显式选择开发配置，提炼成功与原文往返分开断言。
