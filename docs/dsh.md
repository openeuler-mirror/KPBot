# DSH 接入

## 首版范围

关联 [issue #11](https://gitcode.com/openeuler/KPBot/issues/11)。首版聚焦 code-optimizer 的技能加载与普通子代理优化流水线；支持项目级/用户级安装，以及 DSH 原生 bundle。app-tuner、Claude 批量驱动、`kpbot-code-optimizer-workflow`、使用旧 `scriptPath` 接口的 `multimedia-code-optimizer` 和 OpenCode 专属 TF 推理执行系列暂不声明 DSH 支持；通用的 `tf-inference-opt` 方法技能仍可加载。硬件专项能力仍受实际机器、工具链和数据约束，安装成功不等于完成专项性能验收。

社区贡献插件 `kpbot-pr-maintainer` 的技能名与插件名相同。独立的项目级安装测试覆盖该插件及其 DSH 运行协议。在基线 `de731ebc`，全仓 DSH 扫描会因 `Plugins/code-optimizer/skills/benchmark/evals` 缺少入口而中断。

验证目标版本：DSH 0.2.2-rc.3；需要 `skill`、`subagent`、`ask_user_question`、`todo_write` 和文件/命令工具。不同预设缺少必要工具时明确停止，不以主代理代做隔离阶段或跳过审批。

## 安装

以下 npm 命令适用于已包含 DSH 适配的发布版本；验证当前开发分支时请使用下方源码安装。

```bash
npm install -g @kunpengbot/kpbot
kpbot install project dsh /path/to/project
# 用户级安装遵循 DSH_HOME；未设置时使用 ~/.dsh
kpbot install global dsh
kpbot uninstall project dsh /path/to/project
```

开发者从源码目录安装：

```bash
npm ci --ignore-scripts
bash install.sh project dsh /path/to/project
```

项目级技能位于 `.dsh/skills/`，用户级位于 `${DSH_HOME:-$HOME/.dsh}/skills/`。DSH 项目根是最近的 `.git` 祖先：请将项目级技能安装在该根目录，未使用 Git 时安装到启动 DSH 的目录。同名非 KPBot 管理技能会阻止安装；安装清单只管理本次安装的技能，不修改用户的 AGENTS.md、预设或权限配置。

启动 DSH 后加载 `kpbot-code-optimizer`，说明项目、目标平台、待优化函数或测试用例、构建/正确性/基准命令和资源限制。Skill 加载只返回指令，由代理按指令执行。普通子代理返回启动名称，完成结果通过通知收集，不能把启动回执当作阶段结果。

更新先在 `.dsh/.kpbot-dsh-stage-*/new/` 构建完整结果，再将原有技能备份到同一临时目录的 `old/`，最后发布技能和清单。发布前重新检查安装状态和目录冲突；复制、备份或发布失败时保留原清单并恢复旧技能。若恢复本身失败，错误信息会给出保留的恢复目录路径，`old/` 中尚未恢复的技能应先人工恢复。卸载过程中出错时保留清单，排除文件系统错误后可重试。

## 原生 bundle

也可以在 DSH 插件管理器中添加 npm 包 `@kunpengbot/kpbot` 或本仓库目录。bundle 通过独立的 `kpbot-dsh` Skill Provider 提供技能，不把源文件复制进用户目录；禁用/卸载 bundle 后该 Provider 注销。不要同时安装 bundle 与同名项目技能，DSH 的项目级技能优先，可能遮蔽 bundle 版本。

Provider 按需读取指令正文，包目录内的资源使用原始技能资源根。新增、移动技能或修改名称/描述后，重新加载 bundle；修改正文在下一次加载技能时生效。首版未实现文件监听。插件使用 DSH 默认图标，避免为 KPBot 新造品牌图形；图形界面外观验收另行说明。

## 解耦边界

采用现有包声明和稀疏覆盖层，不引入第二套领域索引。DSH 适配器位于 `adapters/dsh/`，业务技能及其覆盖文件随领域包移动。

发现器在 `Plugins/` 下按包声明递归发现，领域目录名及分组深度不参与识别。包的 `.claude-plugin/plugin.json` 显式声明 `platforms` 包含 `dsh`，才会被加载；`skills/` 为基础内容，`dsh/` 首版只提供同名技能的 `SKILL.md` 指令覆盖，额外资源覆盖会明确报错，确保原生 Provider 与复制安装行为一致；脚本、提示词和 references 复用基础技能资源。`dsh.exclude` 排除暂不支持的技能。技能通过 frontmatter 的稳定 `name` 标识，目录名与资源相对关系保留。

无入口的一级分组支持 `skills/<group>/<skill>/SKILL.md`：每个子技能以自己的目录作为资源根，安装时使用子技能目录名，覆盖文件仍位于 `dsh/<skill>/SKILL.md`。`dsh.exclude` 可排除整个分组或单个子技能。已有 `SKILL.md` 的技能目录保持完整，其内部嵌套资源不会再次展平成重复技能；既无入口也无子技能的目录仍会报错。

```json
{
  "name": "example-domain",
  "platforms": ["claude", "opencode", "dsh"],
  "dsh": { "exclude": ["platform-specific-driver"] }
}
```

发现到包声明即停止扫描其内部资源；分组目录不应保留旧聚合包声明再在内部嵌套新包。领域拆分时迁移包声明、技能和指令覆盖文件，并保持稳定技能名称及包内资源关系；跨包依赖按技能名称查找。适配器不保存 `app-tuner`/`code-optimizer` 路径。重复名称、无效元数据和越界路径报错，不能静默选择某份实现。

DSH 工具协议集中在 `adapters/dsh/runtime.md`，原生加载和复制安装共同注入该说明；主协调入口用 DSH 差异文件表达业务编排。原有模板中的业务 JSON、预算、reservation、候选隔离及正确性/性能/系统验收门控继续复用。领域算法或业务流程的后续修改仍需对应更新技能。

## 回归

复制安装要求至少发现一个可安装的 DSH 技能。插件目录缺失、全部插件未声明 DSH 或所有技能均被排除时，安装会报错并保留旧技能和清单；如需清空安装，请使用卸载命令。

安装目录与仓库 `Plugins/` 不能互相包含；公共脚本 `Plugins/common/`、未声明 DSH 的插件及新增资源目录也受保护。此类路径在创建目录和复制资源前即被拒绝，避免将安装结果写回源码。安装在工程根目录的 `.dsh/` 不受影响。

```bash
npm ci --ignore-scripts
npm run test:dsh
npm run test:install
python -m unittest discover -s tests -p test_install_cli.py
npm pack --dry-run
```

测试使用 Node.js 内置 test runner、Python unittest 和临时目录，不默认调用模型、修改系统设置或访问硬件服务。覆盖原生 Provider 加载/注销、包移动与拆分、分组技能、冲突及资源路径、项目/用户安装、重装与卸载。Windows 测试使用无需管理员权限的目录联接验证链接拒绝行为，并优先使用已安装的 Git Bash。旧版 Claude/OpenCode 安装回归另行覆盖 UTF-8 元数据、CRLF、含中文和单引号的路径、Python 回退以及用户技能保留。设置 `DSH_INSTALL_DIR` 时额外运行两项独立 Cordis 容器中的真实 DSH Registry/Filesystem 测试，未设置时明确跳过；C 夹具检查需要 GCC，缺失时明确跳过。可复用的最小优化目标和固定正确性/基准工具位于 `tests/fixtures/dsh-optimizer/`，包含错误实现反例检查。真实代理优化验收须另提供实际执行记录，不能用离线安装测试替代。本次真实模型的覆盖范围、阻塞结果与未验证项见 [首版验证记录](dsh-validation.md)。
