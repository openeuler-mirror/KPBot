---
name: repo-view-verify
description: >
  代码仓视图系列的证据验证 skill（被六个 repo-analysis-* 分析 skill 在 IR 落盘后引用）。唯一职责：核实 IR
  中的源码证据是否真实——自带零依赖脚本 verify-sources.mjs 用本地 git 逐条验证
  revision/路径/行号，输出结构化回执；含无 Node 环境的命令清单兜底与两轮修复纪律。回执 ok:true 是交付的前置条件。本
  skill 是执行链第二环，一般不直接响应用户请求。
license: MulanPSL-2.0
compatibility: Node.js >= 14（verify-sources.mjs）或本地 git（命令清单兜底）
metadata:
  skill_type: tool_assisted
  category: code_analysis
  domain: repo_view
  version: "1.0.0"
---

# repo-view-verify：证据验证

# 定位

考古产出的 IR 里每一条 `sources` 引用，交付前必须被本地 git 证实。本 skill 只管这一件事：**证据是不是真的**。证据怎么记（格式约定）在 repo-view-engine；怎么呈现（forge 链接、证据表）在 repo-view-render。

# 脚本方式（有 Node 环境时首选）

```
node <本skill目录>/verify-sources.mjs <candidate.ir.json> --repo-root <repo-root>
```

- 递归收集 IR 中全部 `sources`（任何含 `path`+`line` 的对象），逐条验证：
  1. `meta.repository.revision` 是 40 位 SHA 且是本地真实 commit 对象
  2. 路径在该 commit 存在（`git cat-file -e`）
  3. 行号不越界（`git show` 数行；越界时 failure 给出"修 end_line 为 N"的具体修法）
- 输出结构化回执：

```json
{ "ok": true|false, "revision": "…", "verified": 9, "totalReferences": 9, "failures": [ { "subject": "<元素> -> <path>:<line>", "problem": "…" } ] }
```

# 命令清单方式（无 Node 环境兜底）

逐条执行并把结果附进报告"验证"章节。**超过 10 条引用时写 shell 脚本批量执行**，
不要在命令行逐条手打（命令行长度有限制，且容易漏条）：

```
git -C <repo> rev-parse --verify <sha>^{commit}
git -C <repo> cat-file -e <sha>:<path>
git -C <repo> show <sha>:<path> | sed -n '<line>,<end_line>p'
```

# 修复纪律

- **`ok: true` 才算可交付**；失败时修引用（宁修引用不美化断言），重跑验证
- **同一问题最多 2 轮**：两轮后仍失败，停止修复，如实报告缺口
- 验证不过的交付红线：文本回答 + 列出缺口，不产出报告，不给"已验证"的说法
- 回执原样附进报告"验证"章节（它就是交付物的质检单）
