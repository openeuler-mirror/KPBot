# Codex

KPBot 提供 Codex 项目级和全局技能安装。需要 Python 3.8 或更新版本；通过 `kpbot` 命令安装还需要 Bash。

```bash
kpbot install project codex "/path/to/project"
kpbot install global codex
```

项目级技能复制到 `<project>/.agents/skills/`，全局技能复制到 `~/.agents/skills/`。省略安装路径时，项目级使用当前目录，全局使用用户主目录；全局命令也可指定另一用户主目录作为最后一个参数。

从源码安装：

```bash
bash install.sh project codex "/path/to/project"
bash install.sh global codex
```

Windows 上也可直接使用 Python，无需 Bash：

```powershell
python adapters/codex/install.py install project codex "C:\my project"
python adapters/codex/install.py uninstall project codex "C:\my project"
```

安装后启动或重新打开 Codex 会话，查看技能列表，并通过 `$kpbot-code-optimizer` 或自然语言启动优化流程。技能脚本各自需要的 Bash、编译器、性能分析工具和目标机器环境仍需按场景准备。

## 适配范围

- 安装器读取插件的 `.claude-plugin/plugin.json`。其中 `platforms` 包含 `codex` 的插件会参与技能发现，例如 `kpbot-pr-maintainer` 和 `common`。安装后可通过 `$kpbot-cannlab-webide` 操作已授权的 CANNLab WebIDE。
- common 中新增的绘图与 Host 验证技能目前仅随 Codex 适配器发布，本次验证覆盖该适配器的安装与更新。使用 `$kpbot-excalidraw-diagrams` 绘图。定期跟踪训推社区变化时使用 `$kpbot-training-inference-host-insights`。要验证一项具体的 CPU 推理或 Host 开销变化时使用 `$kpbot-ai-host-insight`。
- 从 `Plugins/` 的分组目录递归发现插件，遇到 `.claude-plugin/plugin.json` 即停止扫描包内资源；嵌入的插件清单作为资源保留，不会产生第二份安装。未声明 Codex 的插件也遵守该包边界，领域拆分时不要在分组目录保留旧聚合清单。
- 按插件的 `codex.exclude` 跳过依赖其他工具专用工作流的技能。kunpeng-knowledge 的 OpenCode 账户授权流程尚未适配，不在 Codex 中安装。
- 在安装副本中加入 Codex 工具映射说明，将 Claude/OpenCode 的技能目录改为 `.agents/skills/`；源技能保持原样。
- 工作流默认在当前会话执行。`agents/*.md` 保留为角色参考，不自动注册为 Codex 子代理；只有授权且当前环境提供子代理能力时才使用。
- 保留完整脚本、参考资料和嵌套技能资源，并补齐被引用的公共脚本。插件可通过 `codex/<source-skill-directory>/` 提供覆盖层。
- 对自带 `package-lock.json` 的技能，安装器保留锁文件，跳过技能根目录下的本地依赖 `node_modules/` 和测试输出 `test-output/`。安装或更新绘图技能后，按其 `SKILL.md` 在安装目录执行 `npm ci` 准备依赖。
- Codex 递归发现嵌套 `SKILL.md`；含入口的技能包保持原目录结构，不额外安装同名副本。
- 安装器不写入 `AGENTS.md`、`.codex/config.toml`，也不修改模型、权限或 MCP 配置。

## 更新与卸载

重复运行安装命令即可更新。安装清单为 `.agents/kpbot-codex-manifest.json`，每个安装技能带有 KPBot 所有权标记。遇到同名但不属于 KPBot 的技能时，安装会报错并保留用户文件；更新会移除清单中已不再发布的技能。

```bash
kpbot uninstall project codex "/path/to/project"
kpbot uninstall global codex
```

卸载仅处理清单记录且所有权匹配的 KPBot 技能，保留其他技能和用户配置。

发布前会再次核对安装清单、技能所有权及目录冲突；如果准备期间清单发生变化、所有权标记失效或目标位置出现新目录，会停止发布并保留现场文件。卸载遇到删除错误时保留清单，处理错误后可重新运行卸载命令。

未排除的技能目录必须包含 `SKILL.md` 或受支持的分组子技能入口；入口文件意外缺失时会报错，保留现有安装。停用技能应通过 `codex.exclude` 排除，或从发布包中完整移除该技能目录。

安装目录不能位于仓库 `Plugins/` 内部，也不能包含 `Plugins/`。保护范围包括公共脚本 `Plugins/common/`、未声明 Codex 的插件及未来新增资源目录。安装器会在创建目录和复制资源前拒绝此类重叠，防止递归复制或修改来源；正常在工程根目录安装到 `.agents/` 不受影响。

更新失败时，安装器会恢复原有技能和清单。如果恢复过程中也遇到文件系统错误，会保留 `.agents/.kpbot-codex-*/` 中的恢复文件，并在错误信息中给出路径；其中 `old/` 保存尚未恢复的原有技能。处理错误后应先恢复备份，再重新安装。

## 验证

技能计数区分三个口径，不能将清单条目数等同于递归可见的技能文档数。
将主干 `873e9c6f` 和加入搜推分析技能的版本分别安装到空目录，后者的安装清单只增加了 `sra-scenario-analysis`。

| 口径 | 主干 `618dc4ec` | 主干 `bf688e47`（含甘特图） | 主干 `873e9c6f` | 加入搜推分析技能后 |
| --- | ---: | ---: | ---: | ---: |
| 安装清单 `installed_skills`（安装器管理的技能根） | 369 | 370 | 372 | 373 |
| 安装树递归 `SKILL.md`（包含嵌套技能文档） | 415 | 416 | 418 | 419 |
| 声明 Codex 的插件 `skills/` 源树递归 `SKILL.md`（排除前） | 478 | 479 | 487 | 488 |

从 `bf688e47` 到 `873e9c6f`，源树新增 5 份 `ascend_kunpeng/layerwise` 技能文档，以及 Go、Flink 和 MySQL 技能文档各 1 份。

递归文档数只证明文件已安装，不代表真实 Codex CLI
已逐个成功加载；真实加载验证须另运行下述可选 app-server 测试。此前 PR 描述中的“139 个技能”
未绑定可复现的树状态，不应用作当前版本的验证结论。
计数快照保存在 `tests/fixtures/codex-skill-counts.json`，仓库安装回归精确断言三种数量。
增删技能后重新运行项目级安装，并同步更新快照和本文。如果多个 PR 同时修改技能目录，后合入的 PR 应在新的目标基线上重新安装、核对三个计数。可在候选合并树的仓库根目录单独运行计数回归，再运行全套安装测试。

```bash
python3 -m unittest discover -s tests -p test_codex_install.py -k test_repository_catalog_matches_measured_counts
python3 -m unittest discover -s tests -p test_codex_install.py
```

测试覆盖安装、更新、卸载、元数据校验、目录冲突、插件包边界、来源及目标目录的链接检查、发布失败与回滚失败，以及命令行路径和编码。Windows 来源链接测试使用真实目录联接，验证插件目录、元数据目录、分组容器、覆盖层和公共脚本的父目录均不能借助联接绕过检查。

Windows 的 Bash 入口会转换适配器脚本路径，Python 安装器同时识别 MSYS 和 Cygwin 盘符路径；回归测试包含源码和目标目录中的中文、空格与单引号，避免依赖 Git Bash 的隐式参数转换。

如需同时验证真实 Codex 技能发现，可将环境变量 `KPBOT_CODEX_TEST_EXECUTABLE` 设置为本机 Codex 可执行文件的完整路径后运行相同命令。该测试创建隔离的项目、用户目录和 `CODEX_HOME`，通过 `app-server` 的 `skills/list` 检查全部安装技能、加载错误和重复名称，不发送模型请求；未设置该变量时跳过该项。

目录约定参考 OpenAI 官方 Codex Skills 文档：`https://developers.openai.com/codex/skills/`。
