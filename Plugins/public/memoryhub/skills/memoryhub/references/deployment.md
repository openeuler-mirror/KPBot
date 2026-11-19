# 部署与复现说明

本文件随 skill 分发，说明客户端与后端服务的边界、版本基线、部署前提和真实服务验证方式。客户端不会自动安装、启动或修改任何后端服务。

## 1. 客户端与服务

`memoryhub` 是独立客户端：通过 HTTP(S) 访问已部署的记忆服务，保存原文并读回确认，不读取后端 SDK 默认配置，也不内置服务进程。部署、升级、备份、认证和网络由部署方负责。

服务不可达、版本不匹配或认证失败时，`backend status` 会返回稳定错误码；不要用“端口可连”代替就绪检查。

## 2. 版本基线

| 后端 | 验证基线 | 初始化和检索要求 |
|---|---|---|
| OpenViking | 0.4.22 | 环回 endpoint、普通用户 API key、健康/系统状态/ready 与向量存储 |
| mem0 | 2.2.1 self-hosted REST | `/openapi.json` 暴露 `MemoryCreate.infer`，pgvector 或等价向量存储，原文可读回 |
| TencentDB Agent Memory | 2.0.3 MemoryCore v3 | `/health` 为 ok，BM25 或 embedding 至少一个可用，暴露 `/v3/conversation/*` |

远程部署必须使用 HTTPS；`api_key` 会随请求发送，不要通过不可信网络传输。健康检查通过不代表模型提炼或全部查询能力已验证。

## 3. 各后端的运行前提

### OpenViking

需要运行中的 OpenViking 服务、可访问的本地 embedding 模型，以及为客户端准备的普通用户 account/user API key。源码仓库提供开发夹具，可准备私有依赖、模型、配置和用户凭证；它不启动服务。

### mem0

需要官方 self-hosted 栈：mem0 REST server、支持向量检索的存储（通常是 PostgreSQL/pgvector）与 embedding 服务。`infer=false` 的原文写入仍会经过 embedding；embedding 模型的上下文上限会限制单条输入，超限写入返回 `WRITE_UNKNOWN`。`derive_memories=true` 额外需要可用的 LLM。

真实 LLM 派生未在仓库的常规验证中做成功性验证；只能通过 `processing` 判断后处理状态，不能把 `ok=true` 当作提取成功。

### TencentDB Agent Memory

需要 2.0.3 MemoryCore 网关、service identity 和可用的 BM25 或 embedding 检索。仓库提供源码级开发夹具，固定上游版本并生成 0600 客户端配置，但不启动服务。TencentDB 的 `processing` 恒为 `unknown`，因为 L0 提炼没有逐记录进度接口。

## 4. 验证边界

- 已覆盖：真实 HTTP、认证、原文写入与读回、跨项目检索、用户隔离、CLI、关闭后持久化。
- 未覆盖：真实 LLM 抽取出成功结果、生产负载/性能、服务进程重启后的全部状态、并发故障注入。
- 未实测的收益、因果或自然语言结论一律标记为“未证明”。

源码仓库的完整测试与 live 门禁属于开发流程，不由 skill 分发，也不要求 agent 运行。
