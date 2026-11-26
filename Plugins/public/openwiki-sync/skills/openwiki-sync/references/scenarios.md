# 场景详解与边界情况

> SKILL.md 提供分派与主干流程；本文件补充每个场景的细节、边界情况与故障恢复。
> 阅读时机：主干流程遇到意外情况、或需要向用户解释细节时。

## 场景全景

```
status ──┬── not_a_git_repo ──────────────→ 引导用户进入代码仓
         ├── unlinked_no_wiki ───────────→ 模式选择（A 独立仓 / B 仓内）→ 对应流程
         ├── unlinked_wiki_exists ───────→ tracked? 模式B已就绪 / 未跟踪: 确认模式后 A=link+push 或 B=直接提交
         ├── registry_recovery_available → restore（自动从注册表解析）→ linked_ready
         ├── linked_wiki_missing_local ──→ restore → linked_ready
         └── linked_ready ───────────────→ 更新联动 / wiki 查询
```

## 存储模式选择的话术

向用户呈现选择时，讲清楚 trade-off 而不是罗列功能：

- "你的 wiki 想放哪？**单独一个 wiki 仓**（团队协作、wiki 有独立链接可以分享给别人看、代码仓保持干净，代价是多管理一个仓），还是**直接放代码仓里**（最省事，wiki 跟代码一起走，但会出现在代码仓的每次 diff 里）？"
- 判断线索：仓库是否多人协作、是否需要对外分享 wiki、用户是否流露出"极简"偏好。
- 用户纠结时的默认建议：团队项目 → A；个人项目 → B。
- 用户问"能不能换"：可以，任何时候都能迁移（见 SKILL.md 模式迁移），不是一次性决定。

## 模式 B（仓内维护）细节

- init 后提交时**确认 `.gitignore` 没有排除 `openwiki/`**（有些项目模板会全局忽略它）。
- 更新联动：代码 commit 后 `openwiki --update -p`，wiki 变化与代码一起提交（可合并进同一 commit，或紧跟一个 `docs(openwiki): ...` commit——跟随用户仓库的提交风格）。
- `.openwiki-link.json`、全局注册表、SYNC 的 link/push/restore 在模式 B 下都不需要。
- 团队成员 clone 后天然获得 wiki（openwiki/ 在仓里），无需恢复流程——这是模式 B 的最大优点。

## 初次全流程细节

### openwiki --init 的行为

- 首次生成会把仓库研究一遍并规划页面队列，逐页写入；`openwiki/.run.json` 记录进度，**中断后重跑同一命令自动续跑**，已完成的页面不会重写。
- 完成后 openwiki 会在代码仓根维护 `AGENTS.md` / `CLAUDE.md` 的 `<!-- OPENWIKI:START -->…<!-- OPENWIKI:END -->` 管理块（指引后续 agent 检索 wiki）。该块随代码仓提交，属正常行为。
- `openwiki/INSTRUCTIONS.md` 是用户可编辑的定制 brief（比如"重点写性能模块"），openwiki 只读不重写；首次 init 后可以建议用户按需编辑。
- 大仓库可设 `OPENWIKI_PAGE_CONCURRENCY=4 openwiki --init -p` 并行生成（1-8 之间，注意 provider 限流）。

### 仓库命名与可见性

- 默认 wiki 仓名 `<代码仓名>-wiki`，用户可指定其他名字。
- 默认创建**私有仓**（`create-repo` 需要公开时加 `--public`）。团队协作仓通常需要组织/企业命名空间，API 默认建在个人空间下；**组织仓请用户手动创建**，再走 link 关联。

### link 的可选来源

优先级：用户显式给 > 从代码仓 origin 推断同账号下 `<repo>-wiki` 是否存在（可 `git ls-remote` 试探）> 询问用户。不要替用户猜私有仓地址然后静默失败。

## 更新联动细节

### 什么算"代码 commit"

- 任何改变源码/配置/构建文件语义的 commit：修 bug、加功能、重构、依赖升级。
- 纯文档（README、注释）、格式化、`.openwiki-link.json` 自身的维护提交：不触发联动，但用户要求时也可以同步。
- 多个 commit 的任务（比如一个特性拆了 3 个 commit）：**不必每个 commit 同步一次**，任务收尾时同步一次即可，push message 引用最后一个（或最关键的）commit。

### no-op 行为

- `openwiki --update` 在源码无实质变化时直接返回（刷新 `.last-update.json`）。
- `SYNC push` 在 wiki 内容与 wiki 仓无差异时输出 `"committed": false, "reason": "no_changes"`，不产生空提交。这两层防护保证联动不会污染 wiki 仓历史。

### 冲突：他人同时更新了 wiki 仓

push 被拒（non-fast-forward）说明远端有你没有的提交（通常是同事在他们的机器上同步过）。顺序：`SYNC restore`（拉齐远端到本地 openwiki/，缓存仓硬对齐远端）→ 重新 `openwiki --update -p`（OpenWiki 会 reconcile 两边的 claims）→ `SYNC push`。

### 误操作恢复

- **误跑 `--init` 覆盖本地 wiki**：wiki 仓仍有最近一次 push 的内容，`SYNC restore` 即可找回；丢了的是最后一次 push 之后的本地增量。
- **误删 `openwiki/`**：同上，restore 找回。
- **link 指错仓**：`SYNC link --wiki-repo <正确地址>` 覆盖即可（脚本会重建缓存 clone）；需要彻底解除关联用 `SYNC unlink`（保留本地 openwiki/，仅移除 link 文件与注册表条目）。

## 存量仓库迁移（已按 OpenWiki 标准模式把 openwiki/ 提交在代码仓里）

判定：`git ls-files openwiki/` 有输出。此时：

- **不要**强行 gitignore（对已跟踪文件无效）。保持代码仓双份即可：照常 `link` + `push` 镜像到独立 wiki 仓，两份并存不冲突。
- 用户若想收敛为"仅独立仓"：`git rm -r --cached openwiki/` 后提交，再确认 `.gitignore` 含 `openwiki/`。这是用户的决定，不要代做。

## wiki 查询细节

- 页面组织：`quickstart.md`（导览）→ `index.md`（全站索引）→ `pages/*.md`。先读前两者定位，避免盲翻。
- wiki 中的事实基于生成时的源码快照，**回答关键问题前对照当前源码验证**，尤其是快速迭代中的仓库。
- `openwiki/.claims/` 里是页面事实的证据链（`repo://src/...#L40-L82`），可用来快速跳到源码对应位置。

## 多仓与 monorepo

- 本技能按"一个 git 仓 = 一个 wiki 仓"建模。monorepo 的 wiki 就是整个 monorepo 的一份（openwiki 自己会按目录规划页面）。
- 一个代码仓只保留一条关联（link 文件单值）。换 wiki 仓 = 重新 `link`。

## 安全与礼貌边界

- 永远不把 token 写进任何文件、日志或 commit；token 只通过 `--token` 参数或环境变量进入进程。
- 代码仓的 commit 永远让用户知情（关联文件落地、`.gitignore` 变更等一次性动作要先说明）。
- wiki 仓默认建私有仓；公开前提醒用户 wiki 可能包含内部实现细节。
