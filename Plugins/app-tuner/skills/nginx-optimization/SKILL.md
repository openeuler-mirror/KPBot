---
name: nginx-optimization
description: Nginx 场景专属性能优化技能（鲲鹏/ARM 平台）。当识别到用户应用场景使用 nginx（Web 服务器、静态内容分发、HTTPS/TLS 网关、反向代理、负载均衡、容器/Docker 内 nginx 服务），或用户提出 nginx 性能优化、压测调优、QPS/RPS/吞吐提升、SSL 握手性能等需求时，使用本技能。提供场景识别、分层优化（L1 环境层 / L2 配置层 / L3 鲲鹏加速库层）、基线建立、火焰图热点分析与回归验证的完整方法，所有命令与配置模板自包含、可直接套用。
---

# Nginx 专属性能优化技能（鲲鹏/ARM）

> 经验数据来源：鲲鹏平台 nginx 压测实测（原始记录见 kpbot-dev-journal `a+k/bale/nginx/`）。IRQ/worker 布局与各场景决策为实测经验值，非官方规格；跨环境使用前须按 §4 重新压测确认。

目标：识别到 nginx 应用场景后，按分层策略（L1→L2→L3）实施专属优化并用压测验证性能提升。
本技能所有命令、配置均自包含，可直接在任何 nginx 部署环境执行，不依赖外部脚本。

约定占位符（使用时按实际环境替换）：
- `$NGINX_DIR`：nginx 安装目录（默认 `/usr/local/nginx`）
- `$CONF`：nginx 配置文件路径
- `$ETH`：业务网卡名（如 eth0）
- `$CORES`：可用核（如 `0-3`）、`$MASTER_CORE`：master 绑定核
- `$IRQ_NUM`：启用网卡队列数、`$KEY_BUS`：网卡驱动名（`ethtool -i $ETH` 查 driver）

## 1. 触发条件与场景识别

命中以下任一信号即启用本技能：
- 部署/代码中出现 nginx（nginx.conf、`sbin/nginx`、listen 80/443、`ngx_http_*` 模块、反向代理配置）
- 业务为：静态内容分发、HTTPS/TLS 网关、API 负载均衡、容器化 Web 服务
- 需求为：nginx 压测、QPS/RPS 提升、SSL 握手优化、长/短连接吞吐优化
- 平台为鲲鹏（Kunpeng-920 / 920G / 920GPro，AArch64）

**先做场景四要素判定**（决定后续所有配置选择）：

| 要素 | 取值 | 判定方法 |
|------|------|----------|
| 连接模式 | `http_short` / `https_short`（无 keepalive，连接即断）/ `http_long` / `https_long`（keepalive 复用） | 客户端是否复用连接；`Connection: Close` 头；业务 QPS 高但单请求轻 → short；网关/代理持续转发 → long |
| 部署形态 | `vm`（虚拟机）/ `physical`（物理机）/ `docker`（容器） | `systemd-detect-virt`、`cat /proc/1/cgroup` |
| 核数规模 | 小规格（如 4T，worker=核数）/ 全机（如 1P=128 核）/ 容器规格（如 8U16G） | `nproc`、cgroup cpu 配额 |
| TLS 是否启用 | https 场景瓶颈在握手（RSA/ECDHE/记录层加解密），http 场景瓶颈在 epoll+sendfile | listen 是否带 ssl |

## 2. 优化总工作流（Pipeline）

```
场景识别(§1)
  → 建立基线（§4：httpress/wrk 压测 + 记录 RPS）
  → L1 环境层优化（playbook「L1 环境层」：中断绑核/系统参数）        → 验证增益
  → L2 nginx.conf 场景化配置（§3 决策表 + playbook「L2 配置层」）    → 验证增益
  → L3 鲲鹏加速层（playbook「L3 加速层」：KAL OpenSSL/KAE/tcmalloc/xcall）→ 验证增益
  → 若仍不达标：火焰图/PMU 热点分析（playbook「热点分析」）
    → 参数精调（playbook「参数精调」）或转入代码级优化
```

**原则**：逐层实施、每层压测对比，只保留有增益的变更；禁止一次改全部参数后无法归因。

## 3. 分层决策速查（命令与配置模板见 references/nginx-tuning-playbook.md）

### 3.1 worker 与网卡中断布局（鲲鹏实测经验值，4T=4 核为例）

| 场景 | worker 布局 | IRQ 队列数 / 绑核 |
|------|-------------|-------------------|
| https_short (4T) | 4 worker 于核 0-3 | 4 队列，核 0-3 |
| http_short (4T) | 4 worker 于核 0-3 | 2 队列，核 2-3 |
| http_long / https_long (4T) | 3 worker 于核 0-2 | 1 队列，核 3 |
| docker 8U https_short | 8 worker | 8 队列 |

要点：
- **长连接场景 worker 数 = 核数 - 1**，留 1 核专跑 master + 网卡中断
- 短连接场景中断与 worker 同核分布，减少跨核调度
- NUMA 服务器：`lscpu` 查网卡所在 NUMA 节点，worker 与中断绑同侧节点，避免跨 NUMA 访问

### 3.2 nginx.conf 场景差异决策表（核心）

| 配置项 | http_short | https_short | http_long | https_long |
|--------|-----------|-------------|-----------|------------|
| worker_processes | N | N | **N-1** | **N-1** |
| keepalive_timeout | 65（实际不生效） | 65 | 65 | 65 |
| keepalive_requests | 2000 | 2000 | **200000** | **200000** |
| ssl_session_cache | - | **off**（短连接会话不复用，缓存纯开销） | - | **shared:SSL:1m**（复用握手） |
| ssl_session_timeout | - | 5m | - | 5m |
| ssl_session_tickets | - | off | - | off |
| listen (ssl 端口) | - | `ssl` | - | `ssl reuseport` |
| sendfile / tcp_nopush | on | on | on | on |

TLS 场景补充决策：优先 **ECDSA 证书**（ECC 运算量远低于 RSA，鲲鹏上握手吞吐显著更高）；生产协议只留 TLSv1.2/1.3（1.3 握手 1-RTT 收益大）。

## 4. 基线建立与性能验证

### 4.1 压测工具与标准用法

**httpress**（适合 vm/physical 场景）：
```bash
# http 短连接（无 -k 即每请求新建连接）
taskset -c 0-7 httpress -n 10000000 -c 200 -t 10 http://$IP:80/index.html
# http 长连接（-k 复用连接）
taskset -c 0-7 httpress -n 50000000 -c 200 -t 10 -k http://$IP:80/index.html
# https 短/长连接（端口按实际，-n 量级降低：100000 / 40000000）
taskset -c 0-7 httpress -n 100000 -c 100 -t 10 https://$IP:443/index.html
taskset -c 0-7 httpress -n 40000000 -c 200 -t 10 -k https://$IP:443/index.html
```

**wrk**（适合 docker/通用场景）：
```bash
# 短连接：显式 Connection: Close
wrk -H "Connection: Close" -c 100 -t 4 -d 60 http://$IP:80/index.html
# 长连接
wrk -c 100 -t 4 -d 60 http://$IP:80/index.html
# https 同理换 https://
```

> httpress/wrk 均不可用时兜底 `ab`（httpd-tools 包）：`ab -n 100000 -c 100 http://$IP:80/index.html`，默认即短连接，结果取 `Requests per second` 行。

### 4.2 验证纪律

- 客户端按核分段 `taskset` 绑核；多客户端（≥2 台）结果求和，避免客户端先瓶颈
- 结果提取：httpress 解析输出 `TIMING,<rps>` 行求和；wrk 解析 `Requests/sec:` 求和
- 压测前确认客户端 CPU 未打满（`mpstat -P ALL 1`），否则加客户端
- 每层优化后跑 **≥3 轮取均值**，波动 >5% 视为无效重测
- 输出对比表：L0 基线 → +L1 → +L2 → +L3，量化每层增益，只保留有增益的变更

## 5. 常见问题速查

- **配置不生效**：`nginx -V` 确认 prefix 与实际加载的 conf 路径；vm 场景 conf 需拷到目标机 `$NGINX_DIR/conf/`
- **worker_cpu_affinity 掩码**：掩码是**二进制字符串**（非十六进制，nginx 只接受 0/1 字符）——核0=`1`、核1=`10`、核8=`100000000`；掩码与 worker 顺序一一对应，`lscpu` 核对拓扑；跨 NUMA 时优先同侧核。注意与内核 IRQ 掩码（`/proc/irq/*/smp_affinity`，十六进制）进制不同
- **ulimit 不生效**：须在启动 nginx 的同一 shell 设置，或 systemd 单元加 `LimitNOFILE=102400`；worker_rlimit_nofile 同时设置
- **压测客户端先瓶颈**：多客户端 + taskset 分核，确认客户端自身 CPU 未打满再采信数据
- **docker 网络慢**：宿主机 conntrack/nat 模块加载、容器网络模式（host 最优）、conntrack 表满检查
- **SSL 握手 QPS 低**：先查证书类型（ECDSA 优先）、协议版本（1.3 优先）、session 是否复用，再上硬件加速（playbook「L3 加速层」）
- **改完更差**：立即回滚该项（保留前一版本 conf 备份 `cp $CONF $CONF.bak`），单项单项归因

## 参考资料

- `references/nginx-tuning-playbook.md` — L1/L2/L3 完整命令与配置模板（环境层、nginx.conf 场景化模板、鲲鹏加速层）、火焰图热点分析与 ARM 典型热点对策表、参数精调网格
