# 记忆后端适配器实现指南（面向 TencentDB 等新后端）

本指南说明如何在现有公共层下实现并接入一个新的记忆后端。规范以
`docs/memory-backends/adapter.md` 为唯一标准（已评审定稿，请勿修改）。

## 1. 代码位置

| 组件 | 路径 |
|---|---|
| 公共层（北向门面） | `skills/kpbot-memory/scripts/kpbot_memory/northbound.py` |
| 配置加载与校验 | `skills/kpbot-memory/scripts/kpbot_memory/backend_config.py` |
| 一次性 CLI | `skills/kpbot-memory/scripts/kpbot_memory/backend_cli.py` |
| OpenViking 参考实现 | `skills/kpbot-memory/scripts/kpbot_memory/backends/openviking_http.py` |
| 公共层契约测试 | `tests/memory/test_northbound.py`（FakeBackend 定义了全部期望行为） |
| CLI 生命周期测试 | `tests/memory/test_backend_cli.py` |

（以上相对 `Plugins/memory/`；测试在仓库根 `tests/memory/`。）

## 2. 必须实现的南向接口（6 个 async 方法）

```python
async def initialize(self, options: dict, *, timeout=30.0) -> dict
async def status(self, *, timeout=30.0) -> dict
async def store_experience(self, record: dict, *, timeout=30.0) -> dict
async def store_process(self, record: dict, *, timeout=30.0) -> dict
async def search(self, request: dict, *, timeout=30.0) -> dict
async def close(self) -> None   # 成功必须返回 None
```

所有方法返回统一信封，成功时 `{"ok": true, "data": ...}`，失败时
`{"ok": false, "error": {"code", "message", "source_id"}}`。`source_id`
是已知资源定位符（如写入已发生但结果不确定时必须带上）。

`options` 来自信任配置文件的 `options` 字段，由调用方显式提供；
`record` 恰好六个字段：`kind`（experience/task_process）、`project_id`、
`owner_id`、`task_id`、`visibility`（初版只接受 `private`）、
`content`（str / dict / list，必须原样可逆保存，不截断不摘要）。
`search` 的 `request`：`query`、`features`、`sources`（`[{project_id,
owner_id}]`，由公共层按信任配置生成，payload 无法伪造身份）、
`kinds`、`limit`（1..20）。返回 `data.items`，每项含 `content`、
`content_type`（初版恒为 `original`）、`kind`、`project_id`、
`owner_id`、`task_id`、`source_id`、`score`，另有 `partial` 标志。

错误码全集（`code` 字段取值）：`INVALID_ARGUMENT`、`TOO_LARGE`、
`UNSUPPORTED`、`ACCESS_DENIED`、`UNAVAILABLE`、`NOT_READY`、`TIMEOUT`、
`WRITE_UNKNOWN`、`READ_FAILED`、`BACKEND_ERROR`。

## 3. 行为契约要点

- 整调用时限：调用方给定 deadline，超时不得开始新的后端 IO；写操作
  已经开始后超时或取消，必须返回 `WRITE_UNKNOWN` 并携带已知
  `source_id`，不得重试。
- 写后确认：后端报错（即使 4xx）不能证明未落盘；已开始的写必须通过
  读回确认，无法确认时返回 `WRITE_UNKNOWN`。参考实现
  `openviking_http.py` 的 `_store` 展示了完整模式。
- 身份绑定：`owner_id`/项目范围由公共层核定；适配器只校验
  `record["owner_id"]` 与自身绑定一致。
- `initialize` 不得把健康检查当作完全 ready 的证明；返回的 `data`
  需含 `state`、`message`、`supports_shared`（初版为 `false`）。
- `close` 只释放本客户端持有的资源，不删除任何数据、不影响服务。

## 4. 接入公共层的两处改动

1. `backend_config.py` 的 `validate_config()`：当前
   `config["backend"] != "openviking"` 直接拒绝；按后端分派校验各自
   的 `options` 结构。
2. `backend_cli.py` 的 `create_backend()`：当前固定导入
   `OpenVikingBackend`；按 `config["backend"]` 返回对应适配器实例。

调试阶段直接修改这两处即可；正式的多后端注册机制是后续工作。

## 5. 验证方式

把新后端接到 `NorthboundMemory` 后，参照
`tests/memory/test_northbound.py` 中 FakeBackend 的全部用例做契约
测试；CLI 层参照 `tests/memory/test_backend_cli.py`。运行：

```bash
PYTHONDONTWRITEBYTECODE=1 <venv>/bin/python -m unittest \
  tests.memory.test_northbound tests.memory.test_backend_config \
  tests.memory.test_backend_cli
```

CLI 手工冒烟：

```bash
<venv>/bin/python skills/kpbot-memory/scripts/kpbot-memory.py backend \
  initialize --config <信任配置> --timeout 120
```

配置文件要求：当前 OS 用户所有、0600、常规文件（非符号链接）、
严格 UTF-8 JSON、恰好五个字段（`backend`、`user_id`、
`current_project`、`allowed_projects`、`options`）。

## 6. 公共层与首版边界

公共层不依赖第三方库（标准库即可运行）；httpx 仅在 OpenViking 适配器
内部延迟导入。首版明确不支持：跨用户共享（`include_shared=true` 返回
`UNSUPPORTED`）、`excerpt`/`derived` 内容类型、北向 get/list/delete、
自动会话采集。Python 最低 3.10，测试仅用标准库 unittest。
