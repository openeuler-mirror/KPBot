---
name: nginx-env-tuning
description: Nginx 性能优化·环境级（调优链第三环之一）。当 nginx 瓶颈分析（nginx-bottleneck-analysis）结论指向系统层（中断不均/softirq 高/网卡单队列打满），或部署基线检查发现 ulimit/干扰服务/docker 网络损耗需要修复时使用本 skill：关闭干扰服务（irqbalance/firewalld，含存档与恢复）、文件句柄提升、网卡多队列/RSS/toeplitz、IRQ 逐队列绑核、NUMA 同侧布局与 worker×IRQ 布局表。系统级变更均含存档与回退命令，仅一次性压测机/基线机执行，生产与共享节点须先确认。
---

# Nginx 环境级优化（系统与网卡）

> 经验数据来源：鲲鹏平台 nginx 压测实测（原始记录见 kpbot-dev-journal `a+k/bale/nginx/`）。布局经验为实测值，跨环境使用前须回到 nginx-baseline-setup 的基线复测确认。

**链路定位**：消费 `nginx-baseline-setup` 的场景标签与 `nginx-bottleneck-analysis` 的路由建议；本层变更完成后必须回基线复测，增益归因后才保留。

**安全纪律**：服务停用与 IRQ 写入是**系统级变更**——仅一次性压测机/性能基线机执行，生产与共享/多租户节点须先向用户确认；执行前记录状态存档，结束后按恢复命令还原（命令见 references）。

约定占位符：`$NGINX_DIR`、`$ETH`（业务网卡）、`$IRQ_NUM`（队列数）、`$CORES`、`$MASTER_CORE`。

## 1. 触发条件

- 瓶颈分析判定树命中第 4 类：softirq `%soft` 高、网卡单队列打满、中断不均
- 基线部署检查发现：ulimit 过低、irqbalance/firewalld 干扰、docker bridge 网络损耗
- 短连接场景需要中断与 worker 同核分布、长连接场景需要独立中断核

## 2. worker 与网卡中断布局（鲲鹏实测经验值，4T=4 核为例）

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

## 3. 变更清单（命令与回退见 references/env-irq-playbook.md）

| 变更项 | 收益 | 风险 | 回退 |
|--------|------|------|------|
| 停 irqbalance（改手工绑核） | 中断稳定不漂移 | 系统级 | 按存档 `systemctl start` |
| 停 firewalld | 消除 netfilter 开销 | 系统级（安全边界） | 按存档 `systemctl start` |
| ulimit / LimitNOFILE=102400 | 高并发句柄 | 低 | 还原配置 |
| `worker_rlimit_nofile 102400`（nginx.conf） | worker 进程句柄上限（与 ulimit/LimitNOFILE 配套） | 低 | 还原配置并重启/平滑重载 |
| 网卡多队列 + toeplitz RSS | 流量分散多队列 | 低 | ethtool 还原队列数 |
| IRQ 逐队列绑核 | 消除中断单核饱和 | 低 | `echo ff > smp_affinity` |
| （可选）cgroup 限内存 | 避免内存抖动 | 中 | cgroup 删除 |

句柄项要同时看三层：启动会话 `ulimit -n 102400`（只影响之后启动的进程）、systemd 单元 `LimitNOFILE=102400`、nginx.conf `worker_rlimit_nofile 102400`（worker 启动时读取，修改后需重启 nginx 或平滑重载生成新 worker 才生效，并用 `cat /proc/<worker-pid>/limits` 核对）。

docker 场景补充：网络模块预加载、网络模式选择（host/macvlan/bridge）与 conntrack 检查见 references「Docker/容器场景补充」（单一事实源，此处不重复）。

绑定后校验（必做）：压测 30s 前后对比 `/proc/interrupts`，每个队列的增量最大 CPU 列必须等于绑定核、其余列增量 ≈ 0（校验命令见 references「绑核后校验判据」）；校验不通过说明布局未生效或掩码写错。

## 4. 输出契约

- `env_changes`：已实施的变更项与存档路径
- `gain_vs_baseline`：回基线复测的 RPS 对比（L0 → +环境级）
- 无增益（<2%）项按回退命令还原，并在结果中注明

路由（按 skill 名 + `microservice/DOMAIN.md` 域路由定位，不使用 `../` 相对路径）：环境级后仍不达标 → `nginx-bottleneck-analysis`（!220）重新分析，或按其路由进入 `nginx-conf-tuning`（规划中 !226；合入前 `nginx-optimization` 配置层）、`nginx-kunpeng-tuning`（规划中 !227；合入前 `nginx-optimization` L3 加速层）。

## 参考资料

- `references/env-irq-playbook.md` — 干扰服务关停/恢复（含存档）、句柄、RSS 多队列、IRQ 逐队列绑核与增量校验的完整命令
