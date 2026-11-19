# KPBot 的 DSH 运行协议

本节是平台适配协议。后续业务技能或共享模板中出现 Claude Code/OpenCode 工具示例时，按本协议映射到当前 DSH 工具；业务预算、验收判据、原始证据及隔离要求不得降低。不要执行原平台的设置文件写入、权限扩大、模型配置或 CLI 驱动示例。

## 工具与执行

- 使用 `skill({name: "<frontmatter name>"})` 加载技能；它返回指令，不会自动运行整个阶段。技能输入取自当前任务上下文和协调者提供的完整 JSON，不依赖 `$ARGUMENTS` 的平台替换。
- `Skill/Read/Write/Edit/Glob/Grep/Bash` 示例分别映射到 `skill/read/write/edit/glob/grep/bash`，以当前工具的真实参数 schema 为准，不照搬 `filePath`、`skill`、`args`、`subagent_type` 等其他平台参数。读取文件使用 `read`，查路径使用 `glob`，搜正文使用 `grep`。
- `AskUserQuestion/question` 使用 `ask_user_question`；`TaskCreate/TaskUpdate/todowrite` 使用 `todo_write` 的完整任务列表。工具缺失时报告缺少的能力，不伪造调用或结果。
- `Agent/Task/task` 委派使用 `subagent({description, prompt, name?, cwd?})`。prompt 载明阶段、任务输入、资源位置、职责与写入边界、必须返回的 JSON/证据位置。角色文件是提示词资源：要求子代理先读取后执行，不能假定 DSH 自动注册 `agents/*.md` 或支持 `oracle/fixer/general-purpose` 类型参数。
- `subagent` 的回执仅表示已启动，回执中的代理名称是后续消息标识。等待完成通知后核对阶段结果和落盘证据；需要补充输入时用 `send_message`。不要在子代理运行时重复启动相同工作，不靠循环/睡眠轮询；仅剩等待时结束当前回合，让完成通知唤起后续步骤。
- 普通优化请求使用普通子代理协议。不要自动调用 `workflow`。只有用户显式要求 workflow 或大规模多代理编排时，才可使用它；首版不提供旧 `Workflow({scriptPath})` 接口兼容，也不声明 workflow 入口可用。

## 资源与路径

以 `skill` 返回的资源根（resource base）解析 `scripts/`、`prompts/`、`references/`、`agents/` 等资源，不能猜测工作目录、领域包名或用户配置目录。原生 bundle 的技能资源根与指令路径可能不同，以资源根为准。跨技能优先按 frontmatter 名称加载，并使用该技能的资源根；同级目录路径只可在已确认安装布局时使用。不要把 `Plugins/code-optimizer`、`.claude/skills`、`.opencode/skills` 写死为运行时路径。

启动 banner 只在资源中存在相应脚本时运行，缺失不阻塞业务流程。共享模板中出现旧平台 banner、权限预检或工具调用代码时同样遵循本协议，不启动另一个 agent CLI。

## 安全与证据

只使用当前会话已有的工具与审批能力。不能写入 `.claude/settings*.json`、OpenCode 权限文件、DSH 预设/配置来绕过限制。提权、下载、远端执行、系统变更及提交遵循实际授权边界；未答复、跳过问题或超时不等于同意。运行失败、工具缺失和性能未测必须明确记录。

维持业务技能的独立子代理阶段、稳定 optimization-point 身份、预算持久化与 execution reservation 一次性消费语义。候选在隔离目录执行，父状态和用户修改在前后核验；正确性、本地性能、系统验收分别判定，只有完整通过的候选能晋升 accepted parent。模型自报成功、启动回执和静态测试不能替代真实编译、测试、基准与证据。系统验收必须满足预先约定的完整判据，包括用户指定的收益阈值、噪声门控和目标环境；仅命令退出 0 或正确性通过不构成 system_acceptance=passed。未决/未测使用 inconclusive/not_run，不与旧 status 形成矛盾。

可选记忆、知识服务或其他领域技能不可用时准确报告能力边界；不得编造检索证据或静默跳过必要依赖。`app-tuner`、Claude 批量驱动及旧 workflow 不属于首版 DSH 支持范围。
