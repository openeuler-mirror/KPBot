# Nginx 基线 Playbook（客户端纪律与基线记录执行细节）

> 本文件是 `nginx-baseline-setup` SKILL.md 的执行细节层：压测工具标准用法（httpress/wrk/ab 命令）见 SKILL.md §3.1，此处承载客户端纪律细节、基线记录模板与调优循环归因表模板。
> 经验数据来源：鲲鹏平台 nginx 压测实测（kpbot-dev-journal `a+k/bale/nginx/`）。

## 客户端纪律细节

- 每台客户端按核分段 `taskset` 绑核（如 8 核客户端分 2 段各 4 核跑 2 个压测进程）
- **多客户端（≥2 台）结果求和**，单台结果不作为服务端结论
- 压测前确认客户端 CPU 未打满：

```bash
mpstat -P ALL 1    # %idle 各核应留余量；打满则增加客户端
```

- 客户端与服务端同机时须反向绑核（客户端核与服务端 worker 核分离），并明确标注结果仅供参考
- 结果提取：httpress 解析 `TIMING:` 行取第 4 字段 rps（`awk '/^TIMING:/ {print $4}'`）求和；wrk 解析 `Requests/sec:` 求和；ab 取 `Requests per second` 行
- 有效轮校验：压测前 `curl -fsS -o /dev/null -w '%{http_code}\n' <URL>` 必须为 2xx，并抽查内容不是 5xx 错误页；每轮保存 wrk 的 `Non-2xx or 3xx responses` / `Socket errors`（或 httpress 失败计数），任一非零即该轮无效，不能计入 L0 或对比均值。L0 阈值：HTTP 非 2xx/3xx = 0 且 socket errors = 0
- ab 兜底按场景用：短连接 `ab -n ... -c ... http://...`；https 换 `https://`；长连接必须 `-k` 并核对 `Keep-Alive requests` 行，禁止把短连接 RPS 记入长连接基线

## 容器规格核对（docker 场景）

仅设置 `--cpus=2` 时，`cpuset.cpus` 可能为空或仍等于宿主全集，不能代表可用算力。分别记录：

```bash
# cgroup v2
cat /sys/fs/cgroup/cpu.max                  # "<quota> <period>"，quota=max 表示不限
cat /sys/fs/cgroup/cpuset.cpus.effective    # 实际可用 CPU 集合
# cgroup v1
cat /sys/fs/cgroup/cpu/cpu.cfs_quota_us
cat /sys/fs/cgroup/cpu/cpu.cfs_period_us
cat /sys/fs/cgroup/cpuset/cpuset.cpus
```

结论里写明「有效 CPU 集合 + 配额折算核数」两项。`quota=max`（v2）或 `-1`（v1）表示不限，容量直接取有效 CPU 集合核数；仅正配额做 `quota/period` 折算，最终取「有效 CPU 核数」与「折算核数」的较小值（无限配额不能直接相除）。

## 基线记录模板

```markdown
## L0 Baseline
- scene_label: <连接模式> / <部署形态> / <核数规模> / <TLS>
- 工具: httpress|wrk|ab，参数: <-n/-c/-t/-d/-k 及 header>
- 客户端布局: <台数、每台绑核>
- 轮次: r1=<RPS> r2=<RPS> r3=<RPS> 均值=<RPS>（波动 <5%）
- 环境备注: <irqbalance/firewalld 状态、ulimit、docker 网络模式>
- 基线配置快照: <conf 备份路径>
```

## 调优循环归因表模板

```markdown
| 层级 | 变更项 | RPS（均值） | 相对 L0 总增益 | 相对上一保留配置增益 | 结论（保留/回退） |
|------|--------|------------|----------------|----------------------|-------------------|
| L0   | 基线   | <RPS>      | -              | -                    | -                 |
| 环境级 | <项>  | <RPS>      | <+x%>          | <+x%>                | 保留/回退          |
| 配置级 | <项>  | <RPS>      | <+x%>          | <+x%>                | 保留/回退          |
| 加速级 | <项>  | <RPS>      | <+x%>          | <+x%>                | 保留/回退          |
```

- 每行只允许一个变更项（单变量原则）
- **保留/回退只看「相对上一保留配置」增益**：<2% 视为无增益，回退该项；否则即使相对 L0 总增益为正、但相对上一保留配置为负，也必须回退（例：L0=100、上一保留=120、当前=115 → 总增益 +15%，相对上一保留 −4.2%，应回退）
- 波动 >5% 的轮次无效，重测
- 多个有增益参数最终合并后整体复测一次，并单独标注合并后的相对上一保留配置增益
