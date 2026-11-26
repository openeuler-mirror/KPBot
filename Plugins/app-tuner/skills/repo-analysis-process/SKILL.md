---
name: repo-analysis-process
description: >
  
  分析代码仓库并生成进程/线程视图（运行时并发结构）。当用户提到以下场景时触发：进程线程图、运行时有哪些进程、线程模型、并发模型、master-worker、主进程起哪些线程、多进程架构、IPC
  关系、生产者消费者线程。产出带源码证据锚定的 Markdown 报告：Mermaid 进程拓扑图（进程为 subgraph、线程为节点、IPC
  机制为边）+ 证据表 + 静态不可知声明。
license: MulanPSL-2.0
compatibility: 本地 git + 代码仓库；Node.js（验证脚本）；Mermaid 渲染环境
metadata:
  skill_type: debugging
  category: code_analysis
  domain: repo_view
  version: "1.0.0"
---

# repo-analysis-process：进程 / 线程视图

# 什么时候用本 skill

用户想看代码仓的**运行时并发结构**：起几个进程、每个进程里有哪些线程、谁和谁通过什么机制通信。这不是部署性能分析，是纯代码结构的运行时投射。

**执行链**（三个公共 skill，按阶段加载，缺一不可）：
1. 读仓前 → 同级 `repo-view-engine/SKILL.md`（考古五步法 + 证据规则）
2. IR 落盘后 → 同级 `repo-view-verify/SKILL.md`（verify-sources.mjs 自验，`ok:true` 才继续）
3. 渲染交付前 → 同级 `repo-view-render/SKILL.md`（IR 公共约定 + Mermaid 映射 + 报告契约）

# 领域判定规则（本 skill 的核心资产）

## 进程识别证据（每声明一个进程，给一条 sources）

- 入口函数 + spawn 点：`main`、`fork()`、`execve`、`posix_spawn`、systemd unit、容器入口脚本
- 管理式创建：`multiprocessing.Process`、`subprocess.Popen`、`clone` 系
- master-worker / 守护进程模型：worker 数从代码常量或配置读取处引用

## 线程识别证据（每声明一个线程/池，给一条 sources）

- C/C++：`pthread_create`、`std::thread`、`thrd_create`、OpenMP pragma、线程池构造点
- Python：`threading.Thread(target=…)`、`ThreadPoolExecutor`、`asyncio` 事件循环（单线程节点标注"事件循环"）
- **线程池 = 一个节点**：不画 N 个同质线程；池大小写进注记并引用读取配置的行
- 同步原语（锁/信号量/条件变量）**不单独成节点**——写进所属线程的注记或全局裁剪说明（实测教训：单独成节点会稀释主拓扑）

## 连线机制 label 词汇表（只许写代码里真实出现的机制）

| 代码证据 | label |
|---|---|
| `socketpair`/`unix socket`/`listen+accept` | `unix socket` / `TCP socket` |
| `pipe`/`mkfifo` | `pipe` |
| `shm_open`/`mmap MAP_SHARED` | `shared memory` |
| `kill`/`sigaction` | `signal <名>` |
| `fork+exec` 父子 | `fork + exec` |
| 锁/条件变量（同进程内） | 虚线边 `互斥 <对象名>` |
| 队列/消息传递 | 虚线边 `队列 <名>` |

## 粒度与取舍

- 6~10 个线程类节点为宜；同质 worker 合并为一个池节点
- **条件性 spawn 必须标注 enabling condition**（如"仅当 `config.enable_tls` 为真才创建"，给创建点+条件分支两处证据）
- 协程/goroutine/事件循环不冒充 OS 线程：节点注明"协程/事件循环"

# 领域诚实约束

- 线程数运行时才确定：写"池，大小 = <配置来源>"，不编数字（代码常量除外）
- 插件系统等动态创建：声明"运行时动态创建，静态不可枚举"
- IPC 机制拿不准（抽象层遮蔽）：标注"机制：<抽象层名>，具体传输取决于配置"

# IR 骨架

```json
{
  "diagram": "process",
  "meta": { "title": "…", "repository": { "url": "…", "revision": "<40位SHA>" }, "worktree": "clean" },
  "elements": [
    {
      "id": "api_process", "label": "进程 · API/HTTP", "kind": "process",
      "threads": [
        { "id": "evloop", "label": "主线程 · 事件循环", "sources": [{ "path": "src/engine.py", "line": 813, "end_line": 855, "label": "引擎循环" }] }
      ]
    },
    { "id": "worker_process", "label": "进程 · Worker", "kind": "process", "threads": [ … ] },
    {
      "id": "ipc1", "kind": "ipc", "from": "evloop", "to": "w_main", "label": "StepCommand · mp.Queue",
      "sources": [{ "path": "src/worker.py", "line": 1646, "end_line": 1648, "label": "队列创建" }]
    }
  ],
  "notes": ["静态不可知项逐条"]
}
```

# Mermaid 映射

```
flowchart TB
  subgraph P_API["进程 · API/HTTP"]
    evloop["主线程 · 事件循环"]
    bridge["to_thread 桥接（临时）"]
  end
  subgraph P_WORKER["进程 · Worker（mp spawn）"]
    w_main["worker 主线程"]
    w_dev["pypto-device"]
  end
  client -->|"HTTPS"| evloop
  evloop ==>|"StepCommand · mp.Queue"| w_main
```

- **进程 → `subgraph`**（天然正确的 containment 语义，每个进程一个）
- 线程 → 节点（带角色 tag）；跨进程 IPC → 跨 subgraph 边，label 必须写真实机制
- 主链路 `==>`；条件性线程/可选 IPC 用虚线 + 注记

# 输出

按执行链交付：IR 落盘 → repo-view-verify 自验 → `<repo>-process-view.md`（进程拓扑图 + 证据表 + 静态不可知声明进"不确定项"章节；报告契约见 repo-view-render）。
