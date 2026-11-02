# memory — 跨会话优化经验记忆

`memory` 是 KPBot 的经验记忆插件：把优化任务的条件（conditions）、做法（method）、结果（outcome）与证据（evidence）保存为完整记录，供之后的会话、不同 agent、不同用户在决策前按条件召回。存储后端可选 OpenViking、mem0 或 TencentDB Agent Memory，通过 `backend` 命名空间的一次性客户端调用；接口规范见 [后端契约](<docs/backend-contract.md>)，后端实现指南见 [后端开发指南](<docs/backend-development.md>)。

## 能力

| 操作 | 说明 |
|------|------|
| `initialize` / `status` | 建立客户端并核对服务身份、授权与就绪状态 |
| `remember` | 保存完整审核经验（experience）或显式提供的完整任务过程（task_process）；内容原样可逆存储 |
| `recall` | 同用户跨项目检索；经验返回完整原文，过程可返回原文或后端派生记忆，并附来源与原生分数 |
| `close` | 释放本次调用客户端；已存数据与后端服务不受影响 |

## 安装

`install.sh` / `kpbot install` 会按目录整体拷贝插件下的 skills，因此 `Plugins/memory/skills/kpbot-memory/` 及其 `scripts/` 一起落盘到目标工具的 skills 目录：

```bash
# 项目级（当前目录），Claude Code
./install.sh project claude
# 项目级，OpenCode
./install.sh project opencode
```

安装后脚本位置：

| 工具 | 路径 |
|------|------|
| Claude Code（项目 / 全局） | `.claude/skills/kpbot-memory/scripts/kpbot-memory.py` / `~/.claude/skills/...` |
| OpenCode（项目 / 全局） | `.opencode/skills/kpbot-memory/scripts/kpbot-memory.py` / `~/.config/opencode/skills/...` |
| KPBot 仓库内 | `Plugins/memory/skills/kpbot-memory/scripts/kpbot-memory.py` |

CLI 也可以从包入口透传：

```bash
bin/kpbot memory backend --help             # 仓库内
kpbot memory backend --help                 # npm 全局安装后
```

## 快速开始

首先准备一个已部署的后端及其访问凭证。客户端只需 Python >=3.10 和 `httpx==0.28.1`，不需要安装三个后端的 SDK、模型或 Docker。下面从源码目录使用已有 uv 自动准备独立客户端依赖，无需手动激活环境，也不自动下载 Python：

```bash
MEM=(uv run --no-project --no-python-downloads --python python3 --with httpx==0.28.1 bin/kpbot memory)
"${MEM[@]}" configure --backend mem0 --config "$HOME/.config/kpbot/memory.json"
"${MEM[@]}" backend status --config "$HOME/.config/kpbot/memory.json" --progress
```

`configure` 通过交互输入创建新的 0600 配置，密钥不回显；不覆盖已有配置，也不安装/启动后端。只有 `backend status` 实际检查后，才知道服务能否连接。uv 首次准备依赖会显示自身进度；已安装依赖的 Python 环境可直接调用脚本，`source <venv>/bin/activate` 不是必需步骤。

已安装 skill、npm 包、无 uv 环境的等价命令，以及三后端完整配置和首次读写样例，见随 skill 分发的 [接入指南](<skills/kpbot-memory/references/getting-started.md>)；服务部署与验收边界见 [部署与复现说明](<skills/kpbot-memory/references/deployment.md>)，源码测试条件见[后端开发指南](<docs/backend-development.md>)。

配置文件包含 `backend`、`user_id`、`current_project`、`allowed_projects`、`options`，由用户/宿主明确提供且保持 0600；`owner_id` 与 `visibility` 由公共层核定，payload 不能覆盖。完整调用约定见 [`skills/kpbot-memory/SKILL.md`](<skills/kpbot-memory/SKILL.md>)。

## 后端选择与配置

三个后端使用相同的北向接口，`options` 按后端校验，不接受任意 HTTP 头或业务输入指定的身份。

| backend | 版本基线 | 主要 options |
|---|---|---|
| `openviking` | 0.4.22 | `endpoint`、`api_key`、`account_id`、后端 `user_id`、`processing_mode`（`vectors_only` / `semantic_and_vectors`）、`max_input_bytes` |
| `mem0` | 2.2.1 self-hosted REST | `endpoint`（必填）、`api_key`（启用鉴权时必填）、`derive_memories`（默认 true）、`max_input_bytes` |
| `tencentdb` | 2.0.3 MemoryCore | `endpoint`、`api_key`、`service_id`（必填）、`agent_id`、`chunk_chars`、`max_input_bytes` |

三后端的完整五字段模板、身份映射和隐藏密钥输入步骤统一放在 [接入指南](<skills/kpbot-memory/references/getting-started.md>)，随 skill 一起安装。优先用向导创建新文件，避免手写权限、项目范围或凭证字段出错。

mem0 的 `api_key` 通过官方 `X-API-Key` 请求头发送；可使用服务支持的 API key 或 `ADMIN_API_KEY`。只有显式设置 `AUTH_DISABLED=true` 的本地开发服务才可省略。初始化及状态检查会读取公开接口描述，再访问带用户范围的受保护列表接口；不写业务记录，也不探测 embedding 或 LLM 推理。远程传输凭证应使用 HTTPS。

`derive_memories=false` 仅保存与检索原文；设为 true 时，任务过程原文仍独立保存并读回确认，随后调用 mem0 原生 LLM 提取。原文已确认但提取失败时，返回保存成功及 `processing=failed`；后处理超时时返回保存成功及 `processing=unknown`。不要据 `ok:true` 单独判断提取成功。

TencentDB 使用 `/v3/conversation/*` 保存与检索 L0 原文；默认单次输入封装上限为 2 MiB，适配器按实际 JSON 请求体大小拆批。`processing=unknown` 不表示已完成 LLM 提取。v2.0.0–v2.0.2 缺少初始化所检查的健康字段，不属于此处的版本基线。

## 与 code-optimizer Pipeline 集成

| 阶段 | 动作 | 约束 |
|------|------|------|
| `decide-optimization`（决策前） | `backend recall` 历史经验作参考 | 仅作参考；来源标注表示条件不完全吻合时需更保守；不写入 `expected_speedup` 冒充实测 |
| `adversarial-review`（review 结束） | `backend remember` 提交候选 | 必须有人工明确确认；只有真实 before/after 与 correctness 证据才可提交；不自动提交 |

两个接入点都是可选增强：未安装插件、未提供配置或用户拒绝时直接跳过，不阻塞流水线。

## 安全与信任边界

- 记录当前为 private；跨用户共享未提供。
- 配置文件由部署方提供：当前 OS 用户所有、0600、非符号链接；`user_id`、`current_project`、`allowed_projects` 由公共层核定，payload 不能覆盖身份或项目范围。
- 召回内容是**不可信数据**：禁止指令性注入与自动执行，历史经验不能跳过本次验证。
- CLI 不打印配置或凭证内容；能直接读写配置文件的人等价于管理员，不是被隔离的租户。

## 效果边界

- 不自动采集会话、不自动发布记忆；完整候选由宿主 agent 提交并经人工确认。公共层不提炼正文，后端可按配置生成派生记忆。
- 审核经验完整保存并返回原文；任务过程保留原文，也可检索后端派生记忆。**不声称自动证明因果或验证任意自然语言结论**。
- 模型驱动的端到端性能收益如未实测，一律记为"未证明"。

## 设计与协作

- 适配器接口规范：[后端契约](<docs/backend-contract.md>)
- 后端实现指南：[后端开发指南](<docs/backend-development.md>)
- 插件清单：[`.claude-plugin/plugin.json`](<.claude-plugin/plugin.json>)
- Skill 常驻说明：[`skills/kpbot-memory/SKILL.md`](<skills/kpbot-memory/SKILL.md>)

## 开发与源码验证

开发文档统一放在 `docs/`，用户接入与部署说明保留在随 skill 分发的 `references/`。代码结构、单元测试、安装及 npm 包回归、真实服务测试条件见[后端开发指南](<docs/backend-development.md>)。

OpenViking 开发辅助工具位于源码根目录的 `tools/memory/setup-openviking.py`，仅供完整 checkout 使用；npm 包和已安装 skill 均不包含该 helper。
