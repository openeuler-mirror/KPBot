---
name: nginx-conf-tuning
description: Nginx 性能优化·配置级（调优链第三环之一，通常为分级优化的首选级别）。当 nginx 需要场景化配置调优——worker 数与绑核、keepalive、ssl session 复用、listen reuseport、TLS 证书与协议选择（ECDSA/TLS1.3）、docker 场景配置模板，或瓶颈分析（nginx-bottleneck-analysis）路由到配置级时使用本 skill。按四场景（http/https × 长/短连接）决策表输出 nginx.conf 变更，每项变更经 nginx -t 校验后生效并回基线复测。命令与配置模板自包含。
---

# Nginx 配置级优化（nginx.conf 场景化）

> 经验数据来源：鲲鹏平台 nginx 压测实测（原始记录见 kpbot-dev-journal `a+k/bale/nginx/`）。决策表为实测经验值，跨环境使用前须回到 nginx-baseline-setup 的基线复测确认。

**链路定位**：消费 `nginx-baseline-setup` 的场景标签（四要素）与 `nginx-bottleneck-analysis` 的路由建议；本层变更后必须回基线复测，增益归因后才保留。配置级是**低风险高性价比的首选级别**（瓶颈分析按收益/风险比优先路由到本层）。

约定占位符：`$NGINX_DIR`（安装/运行前缀，`-p`）、`$CONF`、`$NGINX_PID_FILE`、`$MASTER_CORE`。

## 1. 触发条件

- 场景标签在手（来自 nginx-baseline-setup），需要输出场景化 nginx.conf
- 瓶颈分析命中第 1/2/3/6 类（TLS 握手/连接销毁/拷贝/accept 风暴），对策落在配置层
- 用户要求 nginx.conf 调参、TLS 证书/协议优化、keepalive 策略
- `nginx-bottleneck-analysis`（!220）输出参数精调方向——参数搜索空间与规范以本 skill 为单一事实源

## 2. 场景差异决策表（核心）

| 配置项 | http_short | https_short | http_long | https_long |
|--------|-----------|-------------|-----------|------------|
| worker_processes | N | N | **max(1, N-1)** | **max(1, N-1)** |
| keepalive_timeout | 65（实际不生效） | 65 | 65 | 65 |
| keepalive_requests | 2000 | 2000 | **200000** | **200000** |
| ssl_session_cache | - | **off**（客户端不做 session 恢复时；TLS1.2 客户端会带 session ID 重连时改 shared 并验证） | - | **shared:SSL:1m**（复用握手） |
| ssl_session_timeout | - | 5m | - | 5m |
| ssl_session_tickets | - | off | - | off |
| listen (ssl 端口) | - | `ssl` | - | `ssl reuseport` |
| sendfile / tcp_nopush | on | on | on | on |

TLS 场景补充决策：优先 **ECDSA 证书**（ECC 运算量远低于 RSA，鲲鹏上握手吞吐显著更高）；生产协议只留 TLSv1.2/1.3（1.3 握手 1-RTT 收益大）。

worker 绑核布局（与中断布局的配合）见 `nginx-env-tuning`（!223）§2 场景表。

## 3. 配置模板与生效

- 通用骨架（epoll/worker_connections/multi_accept/sendfile/open_file_cache/reuseport）、HTTPS TLS 模板、docker 场景模板与自签证书生成：见 `references/conf-playbook.md`
- **生效纪律**：每项变更前先备份当前生效配置（`$CONF.bak.<ts>`）→ `nginx -t -c $CONF -p $NGINX_DIR` 校验**同一实例** → `nginx -s reload -c $CONF -p $NGINX_DIR` 平滑重载（`worker_cpu_affinity` 也走平滑重载，检查新 worker 的亲和性与 worker 数；只有参数明确要求时才进入显式的 `FULL_RESTART=1` 分支：核验目标 master、按 pidfile 停止、等待端口释放、启动后实际请求成功，**禁止 pkill**）→ 压测复测。回退时恢复该备份、用同一实例校验并重载，再发一次请求确认服务恢复。

## 4. 常见问题速查

- **配置不生效**：`nginx -V` 确认 prefix 与实际加载的 conf 路径；vm 场景 conf 需拷到目标机 `$NGINX_DIR/conf/`
- **worker_cpu_affinity 掩码**：掩码是**二进制字符串**（非十六进制，nginx 只接受 0/1 字符）——核0=`1`、核1=`10`、核8=`100000000`；掩码与 worker 顺序一一对应，`lscpu` 核对拓扑；跨 NUMA 时优先同侧核。注意与内核 IRQ 掩码（`/proc/irq/*/smp_affinity`，十六进制）进制不同。改掩码后平滑重载并检查新 worker 的 `ps -o psr`/`taskset -pc`，不要用 `-s stop` 中断长连接
- **worker_rlimit_nofile 同时设置**，与 ulimit/LimitNOFILE 配合（环境级见 `nginx-env-tuning`）

## 5. 输出契约

- `conf_changes`：场景标签 + 变更项清单（每项对应决策表行）+ 新 conf 路径与备份路径
- `gain_vs_baseline`：回基线复测的 RPS 对比（L0 → +配置级）
- 单变量原则：逐项变更逐项复测（纪律见 `nginx-baseline-setup` !218）；每个候选点只与“仅差该参数的当前对照”比较，增益 <2% 即回退。与 L0 的累计增益单列展示，不用于单项保留判断

路由（按 skill 名 + `microservice/DOMAIN.md` 域路由定位，不使用 `../` 相对路径）：配置级后仍不达标 → `nginx-bottleneck-analysis`（!220）重新分析；命中系统层 → `nginx-env-tuning`（!223）；加速层 → `nginx-kunpeng-tuning`（规划中 !227；合入前 `nginx-optimization` L3 层）。

## 参考资料

- `references/conf-playbook.md` — 通用骨架、TLS 模板、docker 模板、配置生效/回滚完整命令与参数精调搜索空间（单一事实源）
