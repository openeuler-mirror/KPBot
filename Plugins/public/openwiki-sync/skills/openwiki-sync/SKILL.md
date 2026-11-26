---
name: openwiki-sync
description: OpenWiki 文档的编排与同步层，负责代码仓 OpenWiki 的全生命周期：存储模式选择（独立 wiki 仓 vs 仓内维护）、初始化/生成、GitCode/Gitee/GitHub wiki 仓创建或关联、代码仓↔wiki仓关联映射记录、代码 commit 后自动增量更新 wiki 并生成对应 commit、新机器关联恢复、wiki 知识查询。任何 openwiki 初始化/生成/更新请求都应先经本技能编排（含"弄一份 openwiki 文档""初始化 openwiki""更新 wiki"等中性表述）；openwiki 官方技能与 CLI 仅是本技能调用的生成引擎。当用户提到 openwiki、wiki 生成/仓库/子仓/文档仓、openwiki 放哪/是否单独建仓、commit 后同步/更新 wiki、wiki 关联或恢复，或要求"给这个仓库建 wiki""更新 wiki""查一下 wiki"时必须使用本技能；在已配置 openwiki（存在 .openwiki-link.json 或 AGENTS.md 同步契约区块）的仓库中执行任何会产生 commit 的任务时，也必须使用本技能完成 wiki 联动，不得只提交代码仓。
---

# OpenWiki 代码仓文档生成与双向同步

## 使命

让每个代码仓都有一份**持续演进的 OpenWiki 文档**：wiki 内容由 openwiki CLI 生成与增量更新；存放位置由用户选择——独立 wiki 仓（本技能提供 commit 级联动同步）或代码仓内（OpenWiki 标准模式）；**代码每提交一次，wiki 就跟着更新一次**，独立仓模式下两边 commit 互相可追溯。

## 与 openwiki 官方技能的协作协议

会话中可能同时存在官方 openwiki 技能（负责 MCP 页面生成生命周期）和本技能。**分工：本技能负责全局编排**——场景判定、存储模式选择、wiki 仓准备、关联记录、commit 联动同步；openwiki 技能（或 CLI）仅作为**生成引擎**被本技能在"生成/更新 wiki"这一步调用。用户请求"初始化/生成/弄一份 openwiki 文档"时**先走本技能**（它决定模式与去向），不要直接进入生成。已关联仓库的 `.openwiki-link.json` 与 AGENTS.md 中的同步契约区块是持久化的联动信号，与技能是否加载无关。

核心约定（理解后灵活执行，不要死记步骤）：

- **SYNC 是所有脚本调用的简称**，实际命令是：
  `python3 <本技能目录>/scripts/openwiki_sync.py <子命令>`
  本技能目录 = 本 SKILL.md 所在目录（技能加载时你会知道它的路径，也可从技能元数据的 location 得到）。执行前先确认该脚本存在；脚本是纯 Python 标准库实现，无第三方依赖。
- **独立仓模式下，wiki 仓是 wiki 内容的唯一持久存储**。代码仓内的 `openwiki/` 目录只是工作副本，已被 gitignore；OpenWiki 的增量更新依赖 `openwiki/.claims/`、`openwiki/.last-update.json`、`openwiki/.page-manifest.json`，它们随镜像同步，增量能力不丢失。
- **关联关系双份存储**：代码仓根的 `.openwiki-link.json`（随代码 commit，团队共享）+ 全局注册表 `~/.kpbot/openwiki-links.json`（记录 last_sync 等机器本地状态）。
- 机械操作全部走 SYNC 脚本，它保证幂等与一致性；你负责场景判断、用户交互、openwiki CLI 调用和 commit message 编排。

## 第一步永远是 status

```bash
python3 <本技能目录>/scripts/openwiki_sync.py status [--repo <代码仓路径>]
```

输出 JSON 中的 `scenario` 字段决定后续流程：

| scenario | 含义 | 执行流程 |
| --- | --- | --- |
| `not_a_git_repo` | 不在 git 仓内 | 请用户切换到代码仓根目录或先 `git init` |
| `unlinked_no_wiki` | 初次：无关联、无 wiki | [初次全流程](#初次全流程)（先做模式选择） |
| `unlinked_wiki_exists` | 本地已有 openwiki/ 但未关联（看 `openwiki_dir.tracked_in_git`：已跟踪=仓内模式已就绪；未跟踪=需和用户确认模式） | [落地已有 wiki](#落地已有-wiki) |
| `registry_recovery_available` | 仓内 link 文件丢失但注册表有记录 | [恢复关联](#恢复关联) |
| `linked_wiki_missing_local` | 已关联但本地缺 openwiki/（新机器/新 clone） | [恢复关联](#恢复关联) |
| `linked_ready` | 关联就绪（独立仓模式） | [更新联动](#更新联动) 或 [wiki 查询](#wiki-查询) |

status 的 `environment` 字段还报告 node/openwiki CLI/provider 配置状态，前置检查不通过时先走[环境准备](#环境准备)。

## 存储模式：替用户把选择讲清楚（模式 A / 模式 B）

初次流程的第 0 步，以及用户问"openwiki 放哪/要不要单独建仓"时，向用户呈现两种模式并**由用户确认**：

| | 模式 A：独立 wiki 仓（默认推荐） | 模式 B：仓内维护 |
| --- | --- | --- |
| wiki 位置 | 单独的 git 仓（API 创建或用户自备） | 代码仓内 `openwiki/`，随代码一起提交 |
| 适合 | 团队协作、wiki 需要独立 URL 分享、代码仓保持干净、需要 commit 级联动 | 个人项目、极简偏好、不想多管一个仓 |
| 后续维护 | 本技能联动：commit 后 restore → update → push，wiki 仓生成对应 commit | `openwiki --update` 后随代码一起 commit（OpenWiki 标准用法） |
| 关联记录 | `.openwiki-link.json` + 全局注册表 | 不需要（没有第二个仓） |

判断提示：说不准时默认推荐 A；用户明确"别建新仓库/就放项目里"则 B。**不要替用户默选**——把这个表讲清楚让用户拍板。

**模式 B 流程**（选定后）：环境准备 → `openwiki --init -p` → 把 `openwiki/` 全部提交进代码仓（确认 `.gitignore` **没有**排除它）→ 以后代码 commit 后跑 `openwiki --update -p`，wiki 变化与代码一起提交。模式 B 不使用 SYNC 的 link/push/restore。

**模式迁移**：B → A（用户后来想要独立仓）：建/找 wiki 仓 → `SYNC link` → 从 `.gitignore` 移除 `openwiki/` 并 `git rm -r --cached openwiki/` → 重新加入 gitignore → `SYNC push`。A → B：`SYNC unlink` → 取消 gitignore → 提交 `openwiki/`。

## 环境准备

1. **Node ≥ 22.22**（`node --version`）+ **openwiki CLI**（未安装则 `npm install -g openwiki`；MCP 模式也依赖 CLI 提供 MCP server）。
2. **生成引擎二选一**（按会话环境自动判定，不必问用户）：
   - **MCP 集成模式**（优先）：会话中存在 openwiki 的 MCP 工具（`openwiki_begin`/`openwiki_submit_plan`/`openwiki_next_page`/`openwiki_submit_page`/`openwiki_finish`，通常还伴随 openwiki 技能）→ 由你（宿主 agent）驱动生成/更新，**无需任何模型凭证**。此时按 openwiki 技能描述的生命周期执行（begin → 规划 → 逐页研究撰写 → finish），它对页面怎么写有详细指引。
   - **CLI 模式**（无 MCP 工具时）：`openwiki --init -p` / `openwiki --update -p`，需要 `~/.openwiki/.env` 或 `OPENWIKI_PROVIDER` 环境变量已配置。
   两种引擎产出的 wiki 都在代码仓 `openwiki/` 目录，后续关联/同步流程完全相同。
3. CLI 模式凭证未配置时**不要**在非交互 shell 里跑 `openwiki --init` 向导——两条路给用户选：在自己的终端运行一次 `openwiki --init` 完成配置；或安装 MCP 集成（`openwiki integrations install opencode|claude` 后重启会话）。
4. 凭证失效（报 401/403、`token request failed` 等）：请用户重新运行 `openwiki --init` 或 `openwiki auth <provider>` 刷新，这是 openwiki 自身状态，本技能无法代为修复。

## 初次全流程（unlinked_no_wiki，模式 A）

目标：生成 wiki → 建立 wiki 仓 → 记录关联 → 完成首次同步。**每步完成后再进行下一步**，失败处理见 references/scenarios.md。

1. **环境准备**（见上）。
2. **模式选择**（见上表；用户确认模式 B 则走模式 B 流程，本页后续步骤不再适用）。
3. **生成 wiki**：MCP 模式按 openwiki 技能的生命周期驱动（begin(mode=init) → 规划 → 逐页撰写 → finish）；CLI 模式在代码仓根运行 `openwiki --init -p`。大仓库可能跑 10 分钟以上，设置足够长的超时；中途中断是安全的，重跑同一命令会从断点续跑（`openwiki/.run.json` 记录进度）。完成后 `openwiki/` 出现 `index.md`、`quickstart.md`、`pages/`、`.claims/` 等。
4. **准备 wiki 仓**，三选一（按用户意愿，默认走 a）：
   a. **API 自动创建**：`SYNC create-repo --name <代码仓名>-wiki --platform <platform>`（token 来源见 references/platforms.md）。默认私有仓。
   b. **用户已有空仓**：直接问用户要地址。
   c. **用户手动创建**：引导用户在平台网页建一个**空仓**（不要勾选初始化 README），然后提供地址。
5. **建立关联**：`SYNC link --wiki-repo <wiki仓URL>`。优先使用 SSH 形式地址（推送免 token，HTTPS 推送在非交互环境会因无法输密码失败）；脚本会写 `.openwiki-link.json`、更新注册表、在 AGENTS.md/CLAUDE.md 写入**同步契约区块**、自动维护 `.gitignore`（排除 `openwiki/` 与 `.openwiki-sync/`）。
6. **首次同步**：`SYNC push --message "openwiki: initialize wiki from <代码仓名>@<代码commit短hash>" --code-commit <代码commit全hash>`。这会把 `openwiki/` 全量镜像到 wiki 仓并生成首个 commit；若上一次推送因认证/网络失败留下了未推送 commit，push 会自动恢复推送。
7. **落地关联文件**：把代码仓的变更（`.openwiki-link.json`、`.gitignore`、AGENTS.md/CLAUDE.md 的契约区块，以及 openwiki 顺带维护的 `AGENTS.md`/`CLAUDE.md` 管理块）提交到代码仓，建议 message：`chore(openwiki): 建立代码仓与 wiki 仓关联`。**提交前向用户说明将提交哪些文件**。openwiki 生成模式 A 下附带的 `.github/workflows/openwiki-update.yml`（仓内定时更新）与独立仓同步冲突，不提交并建议删除。
8. 向用户报告：wiki 仓地址、首个 wiki commit、关联已建立、后续 commit 会自动联动。

**验证纪律**：首次 `push` 成功即代表全链路（init→link→push）验证通过。**不要为了"测试联动"而修改用户源码或制造额外 commit**——那是用户没有要求的越权行为。

## 落地已有 wiki（unlinked_wiki_exists）

status 的 `openwiki_dir.tracked_in_git` 区分两种情况：

- **已跟踪（模式 B 已就绪）**：用户此前已选仓内维护，无需关联。仅当用户要求迁移到独立仓时走模式迁移流程。
- **未跟踪**：wiki 已生成但尚未决定去处。先和用户确认模式 A / B（用上面的模式表）：
  - 选 A：**绝对不要重新 `--init`**（会覆盖已有 wiki），从初次全流程的第 4 步继续。
  - 选 B：把 `openwiki/`（连同 `.claims/` 等元数据）直接提交进代码仓。

若 `openwiki/` 是历史遗留、内容陈旧且用户确认不要了，才允许重新 `--init`。

## 恢复关联

适用 `registry_recovery_available` 或 `linked_wiki_missing_local`：

```bash
SYNC restore          # 从 wiki 仓还原 openwiki/ 到本地
```

restore 会自动从 link 文件或全局注册表解析 wiki 仓地址，clone 到 `.openwiki-sync/wiki/` 缓存后把内容复制回 `openwiki/`。他人推送过的 wiki 更新也会一并拉取。若两者都无记录但用户能提供 wiki 仓地址：`SYNC link --wiki-repo <URL>` 后再 restore。

恢复后 scenario 变为 `linked_ready`，即可正常更新联动或查询。

## 更新联动

**触发规则（最重要的一条）**：在已配置 openwiki 的仓库里（模式 A = `linked_ready`；模式 B = `openwiki/` 已跟踪），你执行了任何会产生代码 commit 的任务（修 bug、加功能、重构……），在代码 commit 完成后**必须**追加 wiki 同步；用户仅让你"提交代码"时同样适用。仅文档类、纯格式化等不改变代码语义的提交，可询问用户是否跳过。

模式 B 的联动：代码 commit 后跑 `openwiki --update -p`，wiki 变化随代码一起提交（同一 commit 或紧跟 `docs(openwiki): ...`）。以下三步流程仅适用于模式 A：

同步流程（代码 commit 完成后）：

```bash
SYNC restore                                                  # 1. 拉齐 wiki 仓远端状态（幂等，秒级）
# 2. 增量更新：MCP 模式 openwiki_begin(mode=update) 走生命周期；CLI 模式 openwiki --update -p
#    （无源码变化时是 no-op，秒级返回）
SYNC push --message "openwiki: sync after <短hash> <commit主题>" \
          --code-commit <代码commit全hash>                     # 3. 有变化才会在 wiki 仓生成 commit
```

要点：

- `openwiki --update` 依据 `.claims/` 与 `.last-update.json` 判断哪些页面需要更新，改动小、代价低；无变化时 push 会自动跳过 commit（不产生空提交）。
- wiki 仓 commit message 必须包含对应代码 commit 的短 hash 与主题，push 输出的 `wiki_commit` 与代码 commit 的映射已记入全局注册表，向用户报告时给出两边 commit 的对应关系。
- 若 update 中断：直接重跑 `openwiki --update -p` 续跑，再执行 push。
- 若 push 因权限/网络失败：报告用户具体错误；网络恢复后重试 `SYNC push`（内容来自本地 openwiki/，不会丢）。

## wiki 查询

用户问代码仓相关问题时（架构、模块职责、数据流、入口……），且已配置 openwiki（模式 A 或 B 均可）：

1. 模式 A 且本地 `openwiki/` 缺失：先 `SYNC restore`。
2. 优先读 `openwiki/quickstart.md` 和 `openwiki/index.md` 找到相关页面，再读具体页面；**以 wiki 为先验，再到源码验证**，能显著省去全文检索。
3. 若会话中有 openwiki 的 MCP 检索工具（`openwiki_search`/`openwiki_read`），优先用它们。
4. wiki 未覆盖的问题：正常读源码回答，并提示用户可要求 wiki 同步补全。

## 异常速查

| 症状 | 处置 |
| --- | --- |
| `not a git repo` | 切到代码仓根目录 |
| openwiki 401/403/token failed | 用户终端重跑 `openwiki --init` 或 `openwiki auth <provider>` 刷新凭证 |
| create-repo 报 missing_token | 按 references/platforms.md 引导配置 token，或走手动建仓分支 |
| create-repo HTTP 4xx | 输出里有 manual_fallback 提示；多为仓重名/无权限，改个名字或手动建仓 |
| push 被拒（non-fast-forward） | 先 `SYNC restore` 再 push（远端有他人更新） |
| link 拒绝本地路径/file:// 地址 | 安全限制：wiki 仓地址仅接受 https/ssh 形式（防符号链接解引用外泄本地文件）；本地测试加 `--allow-local` |
| 复制报 symlink_rejected | wiki 内容含符号链接，脚本已拒绝复制以防本地文件外泄；人工核查该 wiki 仓是否可信后再处理 |
| `--init` 误跑覆盖 wiki | 立即停止；wiki 仓仍保留最近一次同步的内容，`SYNC restore` 可找回 |
| Node 版本过低 | 提示升级 Node ≥ 22.22 |

更多细节与边界情况见 `references/scenarios.md`；平台 token、API、SSH 配置见 `references/platforms.md`。
