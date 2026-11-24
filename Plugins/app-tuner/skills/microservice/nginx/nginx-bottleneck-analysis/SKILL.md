---
name: nginx-bottleneck-analysis
description: Nginx 瓶颈分析技能（调优链第二环）。当 nginx 已建立部署基线（nginx-baseline-setup 产出 L0 与场景标签）但性能不达标或瓶颈不明时使用本 skill：按瓶颈判定树定位瓶颈类别（TLS 握手/记录层/epoll+sendfile/客户端/连接队列/中断不均/内存分配），用 perf 火焰图、PMU/mpstat、网卡与 TCP 统计做证据采集，输出瓶颈结论、分级优化路由建议（配置级→环境级→加速级，按收益/风险比排序）与参数精调方向。命令自包含可直接执行，鲲鹏/ARM 平台实测经验。
---

# Nginx 瓶颈分析（调优链第二环）

> 经验数据来源：鲲鹏平台 nginx 压测实测（原始记录见 kpbot-dev-journal `a+k/bale/nginx/`）。热点对策为实测经验，非官方规格。

**链路定位**：消费 `nginx-baseline-setup`（!218）的 `scene_label` + `L0_baseline`，产出瓶颈结论与分级路由建议；不直接实施变更，变更由分级优化 skill 执行并回基线复测。分级优化 skill 中 `nginx-conf-tuning` / `nginx-env-tuning` / `nginx-kunpeng-tuning` 为规划中（!223/!226/!227），合入前一律路由到现有 `nginx-optimization` 对应层，不写不存在的相对路径。

约定占位符：`$CORES`（worker 核列表）、`$ETH`（业务网卡）、`$CONF`、`$NGINX_DIR`。

## 1. 触发条件

- 基线已建立（L0 + 场景标签在手），但 RPS 不达标或分层优化后仍无增益
- 现象证据指向瓶颈不明：CPU 打满但热点未知、QPS 与连接数不匹配、延迟抖动
- 用户明确要求热点分析、火焰图、PMU 分析

## 2. 瓶颈判定树（按优先级排查，含量化阈值）

量化阈值为业界常用参考线（perf top 函数聚合占比、mpstat 百分比、TCP 重传率为通行判读口径），非硬性规格；现场证据落在阈值附近时按判定树顺序并结合场景标签取高优先级类别。

| 顺序 | 瓶颈类别 | 判定证据（量化阈值） | 对策路由 |
|------|----------|----------------------|----------|
| 1 | TLS 握手/记录层 | 火焰图 `ngx_ssl_handshake`/`RSA_*`/`EVP_*` 符号聚合 **≥ 20%** CPU 采样；https 场景 | 证书协议决策（配置级）→ KAE/tcmalloc（加速级） |
| 2 | 连接建立销毁频繁 | `epoll_wait`/`ngx_epoll_process_events` 相关聚合 **≥ 30%**，或上下文切换 **> 100k/s**（`vmstat` cs 列） | reuseport/multi_accept/keepalive（配置级） |
| 3 | 拷贝开销 | `memcpy`/`ngx_writev`/sendfile 路径聚合 **≥ 20%** | sendfile/sendfile_max_chunk/open_file_cache（配置级） |
| 4 | 中断不均 | 单核 `%soft` **≥ 20%** 且显著高于均值（**> 3×** 其余核），或单队列中断速率占网卡总中断 **≥ 50%** | IRQ 绑核 + toeplitz 多队列（环境级） |
| 5 | 并发分配竞争 | `malloc`/`free`（glibc arena 锁）聚合 **≥ 10%** | tcmalloc LD_PRELOAD（加速级） |
| 6 | accept 风暴 | 先用 `ps/top` 确认 master 核被 **nginx worker** 占用（不是同核其他任务）；且 worker `%usr+%sys` 高、`ngx_event_accept`/`ngx_http_wait_request_handler` 为热点，`TcpExtListenOverflows`/`ListenDrops` 有增量 | 核数规划 max(1,N-1) + reuseport（配置级） |
| 7 | 客户端/链路瓶颈 | 客户端任一核 `%idle < 10%`；或 TCP 重传率 **> 1%**（`nstat` 重传段/发出段）；或 `rx_dropped`/`rx_missed` 持续增长 | 回 `nginx-baseline-setup` 修客户端布局/加客户端 |

**SSL 握手 QPS 低的排查优先级**（高频场景专列）：先查证书类型（RSA→换 ECDSA，实测完整握手吞吐差约 5 倍量级）→ 再查协议版本（TLS1.3 握手 1-RTT）→ 再查 session 复用策略（短连接 off、长连接 shared:SSL:1m）→ 配置层确认 reuseport/multi_accept/绑核 → 最后才上加速级（KAL OpenSSL → KAE/UADK → tcmalloc），每步单独压测验证。

## 3. 证据采集命令（自包含）

perf 采样、火焰图生成、热点排名、每核 CPU 分布、网卡丢包统计、TCP 重传与 RTT 的完整命令见 `references/hotspot-playbook.md`。

> **执行边界**：perf/内核符号/`kptr_restrict` 属系统级只读或安全配置操作，仅限隔离压测机/性能基线机；生产与共享/多租户节点须先向用户确认，并保留状态存档与恢复步骤。

要点：
- perf 采样 30s@99Hz 覆盖 worker 核；FlameGraph 固定到**已审阅 commit**（新建/复用目录都核对 HEAD，生产/隔离不足环境用内网镜像分发）
- 火焰图生成步骤尽量非 root 运行，避免以 root 执行第三方脚本；`perf record` 需要权限时优先 CAP_PERFMON/`perf_event_paranoid` 调整，不默认整链 root
- 内核符号 `[unknown]` 时优先用用户态符号（nginx/libc）做对策匹配；确需临时 `kernel.kptr_restrict=0` 时先记录原值，结束立即 `sysctl -w kernel.kptr_restrict=<原值>` 恢复；生产/共享节点禁止该变更

## 4. 参数精调路由（消除跨 skill 双源）

热点确认后若需要参数精调（对 `keepalive_timeout` / `keepalive_requests` / `tcp_nopush` / `sendfile_max_chunk` / `worker_processes` 等做单变量网格搜索），**搜索空间与规范统一归属 `nginx-conf-tuning`**（规划中 !226；合入前以现有 `nginx-optimization` 的配置层决策与 playbook 为准，不重复维护参数网格）；本 skill 只输出「哪些参数值得精调」的方向结论并路由过去。

## 5. 输出契约

- `bottleneck_conclusion`：瓶颈类别（判定树编号）+ 证据引用（火焰图/统计输出路径）
- `level_routing`：分级优化建议（按收益/风险比：配置级先行 → 环境级 → 加速级），写 skill 名而不是相对路径
- `fine_tuning_directions`：值得精调的参数方向（路由到 `nginx-conf-tuning`；合入前落 `nginx-optimization` 配置层）

路由（按 skill 名 + `microservice/DOMAIN.md` 域路由定位，不使用 `../` 相对路径）：
- 配置级 → `nginx-conf-tuning`（规划中 !226；合入前 `nginx-optimization` 配置层）
- 环境级 → `nginx-env-tuning`（规划中 !223；合入前 `nginx-optimization` L1 环境层）
- 加速级 → `nginx-kunpeng-tuning`（规划中 !227；合入前 `nginx-optimization` L3 加速层）
- 客户端/链路问题 → `nginx-baseline-setup`（!218；未安装时回退 `nginx-optimization` 的基线章节）修压测布局后重测

## 参考资料

- `references/hotspot-playbook.md` — perf/火焰图/mpstat/网卡/TCP 完整命令与 ARM 典型热点对策表
