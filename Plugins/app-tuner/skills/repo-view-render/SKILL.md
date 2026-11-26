---
name: repo-view-render
description: >
  代码仓视图系列的渲染与报告契约 skill（被六个 repo-analysis-* 分析 skill
  引用）。唯一职责：怎么组织产出与呈现——IR 公共骨架约定、六图型 Mermaid 映射总表、排版守则、Markdown 报告结构（含
  forge 证据链接规则）与回复契约。本 skill 是执行链第三环：写 IR 时读"IR 公共约定"，交付时读"报告契约"。
license: MulanPSL-2.0
compatibility: 任意支持 Agent Skills 的运行时；Markdown/Mermaid 渲染契约
metadata:
  skill_type: knowledge_base
  category: code_analysis
  domain: repo_view
  version: "1.0.0"
---

# repo-view-render：渲染与报告契约

# 定位

考古（repo-view-engine）产出事实，验证（repo-view-verify）证实证据，本 skill 负责**怎么组织与呈现**：IR 的结构约定、IR→Mermaid 的映射规则、报告的固定骨架。

证据呈现的分工边界：sources **怎么记**（path/line/end_line/≤3 条）归 engine 的证据规则；**怎么拼成 forge 链接、怎么排版证据表**归本 skill。

# IR 公共约定

每个 viz 产出一个 typed IR JSON（落盘为 `<报告名>.ir.json`，可 diff、可再生成），公共骨架：

```json
{
  "diagram": "arch | process | sequence | class | tree | dataflow",
  "meta": {
    "title": "…",
    "repository": { "url": "https://…/<org>/<repo>", "revision": "<40位SHA>" },
    "worktree": "clean | dirty: <声明>"
  },
  "elements": [ …图型自定义，主元素可带 sources[]… ],
  "notes": [ "全局不确定项，逐条" ]
}
```

图型专属的 `elements` 结构见各 repo-analysis-* skill 的"IR 骨架"章节。

# 渲染映射总表

| 图型 | Mermaid 语法 | 关键映射 |
|---|---|---|
| arch | `flowchart LR` | 组件→节点；依赖→带 label 边；真实隔离/构建边界→`subgraph`；主路径→粗边 `==>`；异步/弱边→`-.->` |
| process | `flowchart TB` | **进程→subgraph**（每个进程一个）；线程→节点；IPC→跨 subgraph 边，label 必须写真实机制 |
| sequence | `sequenceDiagram` | 参与者→`participant`；返回→`-->>`；异步→`-x` 或虚线注记；阶段→`rect`/`alt/opt` 块；注记→`Note over`（≤1 行） |
| class | `classDiagram` | 继承→`<|--`；实现→`<|..`；组合→`*--`；聚合→`o--`；依赖→`..>` |
| tree | `flowchart TD` | 根在上，单亲链 |
| dataflow | `flowchart LR` | stages→`subgraph` 分带；flow label 写变换语义；存储→圆柱 `[()]` |

# 排版守则（Mermaid 无布局门禁，靠纪律保可读）

- 节点数遵循各 viz 上限；超限先折叠进注记，不硬塞
- label 短语化；**源码路径只进证据表，不进节点/边 label**
- subgraph 最多两层嵌套；边尽量同层就近连接，避免长程交叉
- 每张图渲染后自查一遍 Mermaid 源码语法（括号/引号/中文标点）
- 实战教训持续沉淀在本节：新图型踩到的新坑，以条目形式追加

# 报告骨架

每个 viz 交付一个自包含报告 `<repo>-<type>-view.md`：

```markdown
# <仓库> · <视图名>

> 锚定 commit: `<40位SHA>` · origin: `<url>` · 工作树: <clean/脏声明>

<Mermaid 图>

## 证据表
| 元素 | 证据说明 | 位置（钉定行） |
|---|---|---|
| … | … | [path:line-end](<forge-url>/blob/<sha>/<path>#L<n>-L<m>) |

## 不确定项
- …（逐条，引用所在元素）

## 裁剪说明
- …（省略了哪些中间层/同构项，为什么不画）

## 验证
<verify-sources 回执 或 命令清单结果>
```

**forge 证据链接规则**：`<repository.url>/blob/<revision>/<path>#L<line>-L<end_line>`；GitHub/GitLab 均适用；目录引用（无行号）用 `/tree/<revision>/<path>`。URL 必须是 **HTTPS 格式**（SSH `git@host:org/repo.git` 先转 `https://host/org/repo`，见 engine 冻结身份节），否则 `/blob/...` 路径不成立。

# 回复契约

- 用户的语言，能短则短：一行结论 + 最后一行给报告绝对路径
- 不复述命令、验证轮次、检查清单——它们都在报告"验证"章节里
- 验证未通过（两轮后仍有 failure）：文本回答 + 如实列出缺口，不交付报告
