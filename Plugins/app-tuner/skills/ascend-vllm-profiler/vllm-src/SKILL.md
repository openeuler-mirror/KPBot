---
name: ascend-vllm-src
description: |
  vllm / vllm-ascend 源码解读子 skill（ascend-vllm-profiler 的配套）。
  用途：用户给出版本号 → 定位对应源码 → 结合 profiling trace 中的 python 帧做
  语义解读（函数在做什么、为什么走这条路径、行号映射）。主 skill 负责 trace
  分析，本 skill 负责把 trace 帧映射到源码语义。
---

# vllm / vllm-ascend 源码解读

## 目录结构

```
vllm-src/
├── SKILL.md            # 本文件
├── knowledge/          # 各版本"训练"产物（知识文档，一版本一文件）
│   └── v0.23.0rc1.md
└── repos/              # 源码库（浅克隆）
    ├── vllm-ascend/    # v0.23.0rc1 (f4a08bd)
    └── vllm/           # v0.23.0 (0fc695f)
```

## 使用流程（解读 profiling 时）

1. 用户给出 vllm-ascend 版本（如 "23.0.rc1" → `v0.23.0rc1`）
2. 读 `knowledge/<版本>.md`：版本指纹、worker 结构、执行模式决策链、锚点函数行号表
3. trace 中某函数语义不明/行号不匹配时，去 `repos/` 查源码：
   - `grep -n 'def <函数名>' vllm_ascend/worker/model_runner_v1.py` 等
   - **行号漂移处理**：生产 trace 常是 tag + 内部补丁（见 knowledge 的"部署版差异"节），
     先比对 knowledge 中的 rc1→部署版行号映射；仍不匹配时按偏移量推断插入点
4. 解读结论回写主 skill 的报告（reports/），必要时把新发现补进 knowledge 文档

## 训练流程（新版本到来时）

对每个新版本执行以下步骤，产出 `knowledge/<版本>.md`（"训练"= 建版本知识文档）：

1. **取源码**：`git clone --depth 1 --branch <tag> https://github.com/vllm-project/vllm-ascend.git repos/vllm-ascend`
   （vllm 版本看 vllm-ascend 仓库的 `.github/vllm-release-tag.commit`，同样浅克隆）
2. **版本指纹**：记录 commit、日期、配套 vllm tag
3. **读主干**（按序）：
   - `vllm_ascend/worker/model_runner_v1.py`：execute_model 全流程（P1/P2/P3 锚点函数与行号）
   - `vllm_ascend/compilation/acl_graph.py`：ACLGraphWrapper 回放/内联决策
   - `_determine_batch_execution_and_padding` + `cudagraph_dispatcher.dispatch`：uniform_decode 判定
   - `vllm_ascend/worker/worker.py`、`vllm/v1/executor/multiproc_executor.py`：worker 循环
   - spec decode 路径（如有）：propose_draft_token_ids / drafter
   - `vllm_ascend/attention/`、`vllm_ascend/ops/`：attention/builders
4. **行号验证**：拿一份该版本的真实 trace（或用户提供的帧名），逐个验证
   `file.py(行号): 函数名` 是否落在 def/装饰器行；全部吻合 → 纯净版；有整体偏移 →
   记录映射表（哪些函数前插入了多少行、出现了哪些 `*_orig` 包装）
5. **写 knowledge 文档**：版本指纹 / 部署版差异 / 决策链 / 锚点行号表 / 关键文件地图 /
   trace 可见性注意（哪些事发生在 profiler 启动前）

## 已知版本

| 版本 | vllm | 部署差异 | 知识文档 |
|---|---|---|---|
| v0.23.0rc1 (2026-07-19) | v0.23.0 | 生产 trace 含 ~210 行 MTP 内部补丁 | knowledge/v0.23.0rc1.md |
