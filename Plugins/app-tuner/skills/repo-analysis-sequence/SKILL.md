---
name: repo-analysis-sequence
description: >
  分析代码仓库并生成调用时序视图（一次调用链的完整交互）。当用户提到以下场景时触发：时序图、调用链、A 怎么调用
  B、一次请求的完整流程、接口调用顺序、调用关系图、请求生命周期、消息往返。产出带源码证据锚定的 Markdown 报告：Mermaid 时序图
  + 参与者证据表 + 裁剪声明。
license: MulanPSL-2.0
compatibility: 本地 git + 代码仓库；Node.js（验证脚本）；Mermaid 渲染环境
metadata:
  skill_type: debugging
  category: code_analysis
  domain: repo_view
  version: "1.0.0"
---

# repo-analysis-sequence：调用时序视图

# 什么时候用本 skill

用户想看**一次具体交互**的全过程：谁先发起、经过谁、返回什么。焦点是一条链路的时序，不是整个系统（那是 repo-analysis-arch 的事）。

**执行链**（三个公共 skill，按阶段加载，缺一不可）：
1. 读仓前 → 同级 `repo-view-engine/SKILL.md`（考古五步法 + 证据规则）
2. IR 落盘后 → 同级 `repo-view-verify/SKILL.md`（verify-sources.mjs 自验，`ok:true` 才继续）
3. 渲染交付前 → 同级 `repo-view-render/SKILL.md`（IR 公共约定 + Mermaid 映射 + 报告契约）

# 领域判定规则（本 skill 的核心资产）

## 参与者怎么认

- 参与者 = **运行时角色**（进程/服务/客户端/存储/外部系统），不是代码模块名——模块是 arch 视图的概念，时序图里出现的是"活着的实体"
- ≤6 个参与者；间接转发层（只透传不改语义）合并省略，裁剪说明里注明"经 X 透传"
- 每个参与者 1~3 条 sources（类型定义/实例化点/入口），锚到"这个角色活在代码哪里"

## 消息怎么认

- **一次业务动作 = 一条消息**；不按函数行数画，参数校验/循环体内部调用不单独成消息
- 追踪规则：
  - 从用户指定入口出发，**round-trip 双向分开追**（请求与返回各一条，返回用 `-->>`）
  - dispatcher/switch 分派处**每个分支都追**，画成各自的分支消息，不合并成"转发"
  - 异步调用（回调、消息队列、task 提交）用虚线/异步箭头，label 写提交机制
  - 追到**真实副作用为止**（落库/发网络/写文件），中间的纯包装层折叠
- 分阶段用 `rect` 色块或 `alt/opt` 块（如"校验 / 查询 / 回填"）

## 排版纪律（实测教训，写死）

- **Note 每条 ≤1 行、不放源码路径**——窄列长 note 必折行遮挡下一条消息；路径证据一律进证据表
- ≤14 条消息；超过先砍中间透传层

# 领域诚实约束

- 消息 label 写代码里的真实方法/接口名，不写想象中的协议
- 超时/重试/异常分支：静态可见的（错误处理代码）画出来并引用行号；运行时才发生的（重试次数）写"策略：<来源>"，不编数字
- 入口拿不到（库被外部调用）：参与者声明"入口不在本仓，从 <调用方类型/接口> 推断"，并把推断依据引用出来
- 裁剪要声明：省略了哪些中间层，不静默删除

# IR 骨架

```json
{
  "diagram": "sequence",
  "meta": { "title": "…", "repository": { "url": "…", "revision": "<40位SHA>" }, "worktree": "clean" },
  "elements": [
    { "id": "http", "label": "HTTP Server", "role": "participant", "sources": [{ "path": "src/server.py", "line": 356, "end_line": 371, "label": "handler" }] },
    { "id": "m2", "role": "message", "from": "http", "to": "engine", "label": "add_request(id, prompt, config)", "variant": "sync|async|return", "note": "≤1 行，无路径" }
  ],
  "segments": [{ "label": "调度", "messages": ["m4", "m5", "m6"] }],
  "notes": ["…"]
}
```

# Mermaid 映射

```
sequenceDiagram
  participant Client
  participant HTTP as HTTP Server
  participant Engine as Engine Core
  participant Worker as WorkerProcess
  rect rgb(235,245,255)
    Note over Client,HTTP: 接入
    Client->>HTTP: POST /v1/completions (stream)
  end
  HTTP->>Engine: add_request(id, prompt, config)
  HTTP-->>Client: SSE 流建立
  Engine->>Worker: StepCommand · mp.Queue (async)
  Worker-->>Engine: StepResult
  HTTP-->>Client: data: [DONE]
```

- 实线请求 `->>`；返回 `-->>`；异步提交用 `-x` 或 `(async)` 后缀注记
- 阶段 → `rect` 色块（每块一条 `Note over` 标阶段名）；关键注记 `Note over` 限 1 行

# 输出

按执行链交付：IR 落盘 → repo-view-verify 自验 → `<repo>-sequence-view.md`（报告契约见 repo-view-render）。
