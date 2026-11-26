---
name: repo-analysis-arch
description: >
  
  分析代码仓库并生成架构视图（组件/模块关系）。当用户提到以下场景时触发：画架构图、仓库整体架构、系统结构图、组件关系、模块依赖图、这个项目由什么组成、high-level
  architecture。产出带源码证据锚定的 Markdown 报告：Mermaid 架构图 + 逐组件证据表（钉定 commit 与行号的
  forge 链接）+ 不确定项声明。
license: MulanPSL-2.0
compatibility: 本地 git + 代码仓库；Node.js（验证脚本）；Mermaid 渲染环境
metadata:
  skill_type: debugging
  category: code_analysis
  domain: repo_view
  version: "1.0.0"
---

# repo-analysis-arch：代码仓架构视图

# 什么时候用本 skill

用户想看一个代码仓的**静态组成**：有哪些组件、谁依赖谁、边界在哪。主路径 = 回答"这个系统是干嘛的、怎么组成"的主要用户旅程。

**执行链**（三个公共 skill，按阶段加载，缺一不可）：
1. 读仓前 → 同级 `repo-view-engine/SKILL.md`（考古五步法 + 证据规则）
2. IR 落盘后 → 同级 `repo-view-verify/SKILL.md`（verify-sources.mjs 自验，`ok:true` 才继续）
3. 渲染交付前 → 同级 `repo-view-render/SKILL.md`（IR 公共约定 + Mermaid 映射 + 报告契约）

# 领域判定规则（本 skill 的核心资产）

## 组件怎么认（判定优先级，从高到低）

1. **构建单元**：CMake target / Make 模块 / Maven 模块 / Go package / Cargo crate——构建边界是最硬的组件边界
2. **包边界**：Python `pyproject.toml`/`setup.py` 声明的包 > 顶层目录
3. **显式目录语义**：以上都无时按源码目录分组，但目录名 ≠ 组件名，按真实职责命名

禁止把"文件很多的大目录"机械拆成多个组件；也禁止把构建上就是一个单元的东西按目录拆开。

## 依赖边怎么认（从强到弱）

1. **运行时调用/数据传递**：函数调用链、RPC、消息收发——最强，必画
2. **数据所有权**：A 独家写、B 独家读的存储——画 store 边
3. **仅 import 而无调用**：弱边，只有当它是公共 API 边界（对外暴露面）时才画，并降低强调

连线 label 写真实机制（"HTTP"/"函数调用"/"写 SQLite"），不写抽象名词。

## 边界怎么认

只画**真实隔离/所有权/构建事实**：构建边界、进程边界、部署边界、信任边界。给单个组件画框必须有显式隔离事实。

## 主路径与粒度

- 默认 8~12 个组件；更细的内容进"裁剪说明"或元素注记，不靠加边表达
- 主用户旅程用粗边（`==>`）提亮；分支和存储就近连接

# IR 骨架

```json
{
  "diagram": "arch",
  "meta": { "title": "…", "repository": { "url": "…", "revision": "<40位SHA>" }, "worktree": "clean" },
  "elements": [
    {
      "id": "http_server", "label": "HTTP Server", "sublabel": "FastAPI + uvicorn", "type": "external|backend|frontend|database|cloud|security|messagebus",
      "sources": [{ "path": "src/server.py", "line": 196, "end_line": 235, "label": "路由注册" }],
      "deps": [{ "to": "engine", "label": "add_request", "variant": "emphasis|dashed|default" }]
    }
  ],
  "boundaries": [{ "label": "信任边界：内网服务", "wraps": ["engine", "worker"] }],
  "notes": ["…全局不确定项…"]
}
```

# Mermaid 映射

```
flowchart LR
  subgraph 信任边界["信任边界：内网服务"]
    engine["Engine<br/>AsyncLLMEngine"]
    worker["Worker<br/>独立进程"]
  end
  client["Client"] ==>|"POST /v1"| http_server
  http_server -->|"add_request"| engine
  engine -.->|"mp.Queue"| worker
```

- 组件 → 节点；依赖 → 边；真实边界 → `subgraph`（最多两层）
- 主路径 `==>`，异步/弱边 `-.->`，其余 `-->`
- 边 label 只写机制短语；路径进证据表

# 输出

按执行链交付：IR 落盘（`<repo>-arch-view.md.ir.json`）→ repo-view-verify 自验 → 渲染 Mermaid 进 `<repo>-arch-view.md`（报告骨架、证据表、forge 链接、回复契约见 repo-view-render）。
