---
name: nginx-baseline-setup
description: Nginx 调优链入口（部署基线）。当识别到 nginx 应用场景（Web 服务器、静态内容分发、HTTPS/TLS 网关、反向代理、负载均衡、容器/Docker 内 nginx）需要性能调优、压测或 QPS/RPS 提升时，先使用本 skill：完成场景四要素判定（连接模式/部署形态/核数规模/TLS）、标准化部署检查与压测基线建立（httpress/wrk/ab），产出可复现的 L0 基线与场景标签，并按链路路由到瓶颈分析或分级优化 skill。命令自包含可直接执行，鲲鹏/ARM 平台实测经验。
---

# Nginx 部署基线建立（调优链入口）

> 经验数据来源：鲲鹏平台 nginx 压测实测（原始记录见 kpbot-dev-journal `a+k/bale/nginx/`）。基线与布局经验为实测值，非官方规格；跨环境使用前须按本 skill 重新压测确认。

**调优链定位**（本 skill 是链入口，只负责基线，不做任何调优变更）：

```
nginx-baseline-setup（本 skill：场景判定 + 部署检查 + L0 基线 + 循环纪律）
  → nginx-optimization（当前可执行的分层调优：L1 环境 / L2 配置 / L3 鲲鹏加速）
  → 规划中（后续 PR !220/!223/!226/!227）：nginx-bottleneck-analysis /
    nginx-env-tuning / nginx-conf-tuning / nginx-kunpeng-tuning
```

> **系列依赖与合入顺序**：本 skill 可独立合入（下游回退到已存在的 `nginx-optimization`，不会死链）；完整分层链路依赖 !220 / !223 / !226 / !227，建议顺序为 !218 → !220 → !223 → !226 → !227。下游目录合入前一律标为“规划中”，不得在结果中断言其存在。

约定占位符（使用时按实际环境替换）：
- `$NGINX_DIR`：nginx 安装目录（默认 `/usr/local/nginx`）
- `$CONF`：实际生效的 nginx 配置文件路径（从运行中 master 的启动参数确认，见 §2.1）
- `$ETH`：业务网卡名（如 eth0）
- `$IP`：压测目标服务端地址（httpress/wrk/ab 的 URL 主机）

## 1. 触发条件与场景识别

命中以下任一信号即进入本链（由本 skill 先行）：
- 部署/代码中出现 nginx（nginx.conf、`sbin/nginx`、listen 80/443、`ngx_http_*` 模块、反向代理配置）
- 业务为：静态内容分发、HTTPS/TLS 网关、API 负载均衡、容器化 Web 服务
- 需求为：nginx 压测、QPS/RPS 提升、SSL 握手优化、长/短连接吞吐优化
- 平台为鲲鹏 920（Kunpeng-920，AArch64）

**先做场景四要素判定**（决定后续所有配置选择，判定结果随路由传递）：

| 要素 | 取值 | 判定方法 |
|------|------|----------|
| 连接模式 | `http_short` / `https_short`（无 keepalive，连接即断）/ `http_long` / `https_long`（keepalive 复用） | 客户端是否复用连接；`Connection: Close` 头；业务 QPS 高但单请求轻 → short；网关/代理持续转发 → long |
| 部署形态 | `vm`（虚拟机）/ `physical`（物理机）/ `docker`（容器） | `systemd-detect-virt`、`cat /proc/1/cgroup` |
| 核数规模 | 小规格（如 4T，worker=核数）/ 全机（如 1P=128 核）/ 容器规格（如 8U16G） | `nproc` + `cpuset.cpus.effective` + cgroup 配额（v2 `cpu.max`；v1 `cpu.cfs_quota_us`/`cpu.cfs_period_us`），不能只看 `cpuset.cpus` |
| TLS 是否启用 | https 场景瓶颈在握手（RSA/ECDHE/记录层加解密），http 场景瓶颈在 epoll+sendfile | listen 是否带 ssl |

> **安全边界**：只读取工作区（用例容器为 `/workspace`）内的配置/报告与本机系统指标；禁止读取凭证文件及工作区之外的宿主路径。

## 2. 标准化部署检查（只检查与记录，不变更）

1. **确认实际加载配置**：`nginx -V` 只显示编译参数，不能代表运行中的 `-c`/`-p`。先读运行中 master 的实际启动参数（`ps -eo pid,args | grep '[n]ginx: master'`），解析 `-c`/`-p`；未显式指定时再用 `nginx -V` 的 `--conf-path`/`--prefix` 补全。vm 场景 conf 需拷到目标机 `$NGINX_DIR/conf/`
2. **文件句柄**：检查当前 ulimit 与 systemd 单元 `LimitNOFILE`（修复执行属 `nginx-optimization` 的 L1 环境层；细分 skill `nginx-env-tuning` 规划中）
3. **干扰服务状态记录**：`systemctl is-active irqbalance firewalld` 存档（关停/恢复执行属 `nginx-optimization` 的 L1 环境层；`nginx-env-tuning` 规划中）
4. **docker 场景**：网络模式（host/macvlan/bridge）确认；有效 CPU 集合与配额分别记录——`cpuset.cpus.effective`（仅 `--cpus=N` 时可能为空或等于宿主全集）、cgroup v2 `cpu.max`、cgroup v1 `cpu.cfs_quota_us` + `cpu.cfs_period_us`。配额为 `max`（v2）或 `-1`（v1）表示不限，容量取有效 CPU 集合核数；仅正配额做 `quota/period` 折算，最终容量取「有效 CPU 核数」与「折算核数」的较小值。不要把 `cpuset.cpus` 单独当作可用算力
5. **基线配置快照**：先解析 `$CONF` 的真实目标（`CONF_REAL=$(readlink -f "$CONF")`），用 `cp -pL "$CONF_REAL" "$CONF.bak.<ts>"` 复制当时内容并 `sha256sum` 核对；不要用 `cp -a`，否则符号链接只会复制链接，后续原地修改会让备份一起变化

## 3. 压测基线建立

### 3.1 压测工具与标准用法

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

> httpress/wrk 均不可用时兜底 `ab`（httpd-tools 包）。按场景选 URL：短连接 `ab -n 100000 -c 100 http://$IP:80/index.html`，https 换 `https://`；长连接必须加 `-k`（`ab -k -n 100000 -c 100 http://...`）并核对输出中的 `Keep-Alive requests` 行。默认 `ab` 每请求新建连接，短连接结果不得记入长连接基线。

### 3.2 基线记录

- 记录格式：场景标签（四要素）+ 工具与参数 + **≥3 轮 RPS 均值** + 客户端布局（台数/绑核）
- 模板见 `references/baseline-playbook.md`
- 此基线即 L0，后续所有层级变更按 §4 纪律与之对比

## 4. 验证纪律与调优循环（全链共用）

- 客户端按核分段 `taskset` 绑核；多客户端（≥2 台）结果求和，避免客户端先瓶颈
- 结果提取：httpress 解析 `TIMING:` 行取第 4 字段 rps（`awk '/^TIMING:/ {print $4}'`）求和；wrk 解析 `Requests/sec:` 求和
- **有效轮校验**：压测前用 `curl -fsS -o /dev/null -w '%{http_code}'` 核对目标返回 2xx 且内容不是错误页；每轮保留 wrk 的 `Non-2xx or 3xx responses` 与 `Socket errors`（或 httpress 的失败计数）。出现非 2xx/3xx 或 socket 错误即该轮无效，不得计入 L0/对比均值；L0 要求错误计数为 0
- 压测前确认客户端 CPU 未打满（`mpstat -P ALL 1`），否则加客户端
- 每层优化后跑 **≥3 轮取均值**，波动 >5% 视为无效重测
- 输出对比表：L0 基线 → +环境级 → +配置级 → +加速级，同时给出「相对 L0 总增益」与「相对上一保留配置增益」；保留/回退看后者，禁止用总增益掩盖叠加后的回退
- **单变量原则**：逐层逐项实施，禁止一次改全部参数后无法归因
- **改完更差**：立即回滚该项（保留前一版本 conf 备份），逐项归因

## 5. 输出契约与路由

产出（传给下游 skill）：
- `scene_label`：四要素判定结果（如 `https_short / vm / 4T / TLS on`）
- `L0_baseline`：RPS 均值 + 压测工具与客户端布局
- `deploy_check`：部署检查结果与基线配置快照路径

路由（按 skill 名 + `microservice/DOMAIN.md` 域路由定位，不使用 `../` 相对路径；安装后所有 skill 平铺在 `<config>/skills/<skill-name>/`）：
- 瓶颈不明或不达标 → `nginx-optimization`（分层调优 + 热点分析；当前唯一可执行的下游）
- 优化方向明确 → 进入 `nginx-optimization` 的 L1/L2/L3 对应层
- `nginx-bottleneck-analysis` / `nginx-env-tuning` / `nginx-conf-tuning` / `nginx-kunpeng-tuning` 为规划中 skill（依赖 PR !220/!223/!226/!227），合入前不得作为路由目标，也不得在结果中断言其存在
- 各级变更后必须回本 skill 的 L0 基线复测，增益归因后才允许保留

## 常见问题速查

- **配置不生效**：从运行中 master 的 `-c`/`-p` 与实际加载文件确认 conf 路径，`nginx -V` 仅用于补全编译默认值（判定见本 skill §2.1；生效与回滚命令见 `nginx-optimization` 配置层，细分 skill `nginx-conf-tuning` 规划中）
- **压测客户端先瓶颈**：多客户端 + taskset 分核，确认客户端自身 CPU 未打满再采信数据

## 参考资料

- `references/baseline-playbook.md` — 客户端纪律细节、基线记录模板与调优循环归因表模板
