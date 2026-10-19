# memory — 跨会话优化经验记忆

`memory` 是 KPBot 的经验记忆插件：把优化任务的条件（conditions）、做法（method）、结果（outcome）与证据（evidence）保存为完整记录，供之后的会话、不同 agent、不同用户在决策前按条件召回。存储后端为 OpenViking，通过 `backend` 命名空间的一次性客户端调用；接口规范见 [`docs/memory-backends/adapter.md`](../../docs/memory-backends/adapter.md)，后端实现指南见 [`BACKEND-ADAPTER-GUIDE.md`](BACKEND-ADAPTER-GUIDE.md)。

## 能力

| 操作 | 说明 |
|------|------|
| `initialize` / `status` | 建立客户端并核对服务身份、授权与就绪状态 |
| `remember` | 保存完整审核经验（experience）或显式提供的完整任务过程（task_process）；内容原样可逆存储 |
| `recall` | 同用户跨项目检索，返回完整原文、来源与相似度 |
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

前提：已部署的 OpenViking 服务、部署方提供的 0600 信任配置文件，以及安装了后端依赖的 uv 环境。仓库内可用 `Plugins/memory/skills/kpbot-memory/scripts/setup-openviking.py` 准备开发环境（依赖、本地模型、私有配置与用户凭证）：

```bash
source <venv>                                                    # uv 管理的独立 Python 环境
bin/kpbot memory backend status   --config "$MEM_BACKEND_CONFIG"  # 核对服务身份与就绪状态
bin/kpbot memory backend remember --config "$MEM_BACKEND_CONFIG" --input experience.json
bin/kpbot memory backend recall   --config "$MEM_BACKEND_CONFIG" --input query.json
bin/kpbot memory backend close    --config "$MEM_BACKEND_CONFIG"
```

配置文件包含 `backend`、`user_id`、`current_project`、`allowed_projects`、`options`，由用户/宿主明确提供且保持 0600；`owner_id` 与 `visibility` 由公共层核定，payload 不能覆盖。完整调用约定见 [`skills/kpbot-memory/SKILL.md`](skills/kpbot-memory/SKILL.md)。

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

- 不做自动提炼、不自动采集会话、不自动发布记忆；完整候选由宿主 agent 提交并经人工确认。
- 只保存与返回完整原始内容，**不声称自动证明因果或验证任意自然语言结论**。
- 模型驱动的端到端性能收益如未实测，一律记为"未证明"。

## 设计与协作

- 适配器接口规范：[`docs/memory-backends/adapter.md`](../../docs/memory-backends/adapter.md)
- 后端实现指南：[`BACKEND-ADAPTER-GUIDE.md`](BACKEND-ADAPTER-GUIDE.md)
- 插件清单：[`.claude-plugin/plugin.json`](.claude-plugin/plugin.json)
- Skill 常驻说明：[`skills/kpbot-memory/SKILL.md`](skills/kpbot-memory/SKILL.md)

## 测试

```bash
bash tests/test_install_platform.sh                                   # 安装回归（平台过滤）
PYTHONDONTWRITEBYTECODE=1 <venv>/bin/python -m unittest discover -s tests/memory -p 'test_*.py'   # 完整套件
```

`tests/memory/` 覆盖 backend CLI 生命周期、公共层契约、OpenViking 适配器与可选的真实服务验证（设置 `KPBOT_OPENVIKING_LIVE_CONFIG` / `KPBOT_OPENVIKING_LIVE_SECOND_CONFIG` 后运行）；具体用例数量随开发变化，不在本文档固化。安装回归 `bash tests/test_install_platform.sh` 验证平台过滤（Claude/OpenCode 各自安装的 skill 集合）。`kpbot-memory` 的自包含落盘可用临时目录单独验证：执行 `install.sh project claude <dir>`（或 `opencode`）后，`<dir>/.claude/skills/kpbot-memory/scripts/kpbot-memory.py` 可直接运行。
