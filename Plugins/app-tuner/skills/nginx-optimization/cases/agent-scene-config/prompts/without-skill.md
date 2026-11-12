# 任务：nginx 场景识别与配置决策

部署场景：一台 4 核 VM 上的 nginx 作为 HTTPS 网关，客户端不使用 keepalive（每请求新建连接，`Connection: Close`），压测环境，目标是提升 QPS。

请完成场景识别与关键配置决策，把结果写到 `/workspace/results/decisions.md`：

1. 场景判定：连接模式（http/https × 短/长连接）、部署形态、核数规模、TLS 状态。
2. `worker_processes` 取值及理由。
3. `ssl_session_cache` 与 `ssl_session_tickets` 取值及理由。
4. `keepalive_requests` 取值及理由。
5. `listen` 是否加 `reuseport`，及理由。
6. 证书类型与 TLS 协议版本选择。
7. `worker_cpu_affinity` 的掩码写法（给出 4 核示例，说明进制语义）。
8. 压测验证纪律（轮数与判定规则）。

约束：

- 不要执行任何破坏性操作（不要 stop 系统服务、不要 pkill、不要改生产配置）。
- 每个决策必须给出理由。
