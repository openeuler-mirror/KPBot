---
name: kptools-dplb-profiler
description: 采集 vLLM DP 节点负载指标并生成负载均衡分析 HTML 报告（单份分析 / A vs B 对比，如 proxy_new vs coordinator 两种路由策略对比）。当用户提到以下场景时使用本 skill，即使没有明说"负载均衡报告"：负载均衡分析或对比、DP 节点流量/请求分发不均、引擎间负载倾斜、主机内端口级失衡、extract_metric 采集脚本、dp_load_metrics CSV 解析、proxy/coordinator 路由策略均衡性、采集节点 metrics 指标出均衡性报告、TTFT 与负载的相关性分析、PD 分离集群 P 节点与 D 节点双池同时采集或两池各自均衡分析、从 Prometheus 按时间段回采历史负载数据（Grafana 代理或直连、指定起止时间、导出 dp_load_metrics CSV）等。支持 SSH 远端现场采集（上传脚本→执行→下载 CSV；PD 双池一次运行产出两份 CSV）、Prometheus 时间段回采（任意历史时间窗，产出同构 CSV）和本地已有 CSV 直接解析三种模式。
---

# kptools-dplb-profiler：DP 节点负载均衡采集与分析报告

对一组 vLLM DP 引擎采样 20 项负载指标（v2/Prom 口径额外采集 5 个 histogram 的 sum/count，共 30 列），计算均衡性（引擎级/主机级/主机内端口级 CV、尾部失衡 LIF、失衡比、方差分解、TTFT 相关性），生成自包含 HTML 报告。

核心原则：**采集与解析分离**。数据有三种来源，产物统一为 `dp_load_metrics` CSV（v2 口径 30 数据列，timestamp 为可读本地时间，末列 `queue_fixed` 标记负 queue 修正点），分析与报告由 `scripts/generate_report.py` 统一完成——不要手写 HTML 或手算指标，脚本输出可复现且口径一致：

| 采集方式 | 脚本 | 适用场景 |
| --- | --- | --- |
| 方式 1：现场轮询 `/metrics` | `extract_metric_v2.sh`（单池）/ `extract_metric_pd.sh`（PD 双池） | 实时观测、指定未来采样时长 |
| 方式 2：Prometheus 时间段回采 | `extract_metric_prom.py` | **已知历史时间段**、Prometheus 已有数据、无需登录业务机 |
| 已有 CSV | 无 | 复用历史数据 |

## 快速决策

1. 用户已有 CSV（本地或告知路径）→ 走 [流程 B：解析生成报告](#%E6%B5%81%E7%A8%8B-b%E8%A7%A3%E6%9E%90%E5%B9%B6%E7%94%9F%E6%88%90%E6%8A%A5%E5%91%8A)
2. 用户要求现场采集、且没给时间段（从现在开始）→ 走 [流程 A：SSH 远端采集](#%E6%B5%81%E7%A8%8B-assh-%E8%BF%9C%E7%AB%AF%E9%87%87%E9%9B%86)
3. **用户指定了历史时间段 / 提到 Prometheus、Grafana、看板、回采、导出某段时间数据** → 走 [流程 A2：Prometheus 时间段回采](#%E6%B5%81%E7%A8%8B-a2prometheus-%E6%97%B6%E9%97%B4%E6%AE%B5%E5%9B%9E%E9%87%87)
4. 对比模式（如 proxy_new vs coordinator）需要两份 CSV：已有则直接用；需现场采则流程 A 采完第一份后提示用户切换路由策略/配置，再采第二份；若两份历史数据都在 Prometheus 里，可用流程 A2 两次回采（相同时长/步长）
5. PD 分离集群，P 池与 D 池要同时采集（或用户给出 P/D 两组节点）→ 流程 A 的 A-PD 小节（现场采）或流程 A2 的 `--pool`（历史回采）；报告阶段 P、D 各出一份单池报告，不做 A vs B 对比

涉及保存位置（CSV 下载、报告输出）时**先询问用户**，不要默认覆盖已有文件。

## 流程 A：SSH 远端采集（方式 1：现场轮询 /metrics）

### A1. 确定三件事（缺什么问什么）

- **节点列表**：用户给出集群名 → 查 `references/cluster_registry.md`；用户直接给节点列表（IP:端口 或 URL）→ 生成临时列表并询问是否登记入清单
- **采集机**：一台能访问全部节点 metrics 端口的 SSH 机器。清单里有 collector 建议则用之；否则询问用户或与用户确认用集群内任一节点
- **采样时长**：默认建议 **30~60 分钟**（1800~3600 秒），用户可指定；10 分钟只适合快速冒烟/连通性验证，统计意义有限。采样间隔默认 10s（`SAMPLE_INTERVAL` 环境变量可调）。为什么建议采久一点：
  1. **压低随机噪声**：CV 的随机底噪 ≈ 1/√N̄（N̄ = 窗口内每引擎平均请求数 = 总流量强度 × 时长 ÷ 引擎数）。10 分钟窗口 N̄ 常只有几十，底噪可达 0.15~0.25——纯噪声就能伪装成"轻度倾斜"；N̄≥200（底噪 ≤0.07）时 CV 分级才可靠，报告的样本护栏也才解除。低流量集群可按公式反推所需时长（例：总 2 req/s × 64 引擎，要 N̄=200 需约 107 分钟——这种情况要么延长、要么接受护栏提示、要么改用 Prometheus 回采长时段）
  2. **跨过短窗警示线**：窗口 <30 分钟且判定非均衡时，报告会挂「窗口范围说明」（失衡可能只是时段性/突发现象）。30 分钟以上的窗口覆盖多个负载相位，第七节窗口内走势也能区分失衡是持续还是突发
  3. **时延与队列指标稳定**：TTFT/queue 等 hist 窗口平均（Δsum/Δcount）与 waiting 均值/峰值需要足够样本，短窗容易只覆盖一两个相位
  4. **上限也有理由**：单窗口不建议超过 1~2 小时——过长会把不同时段的流量形态平均掉（时段性失衡被稀释），跨时段对比应做多窗口/多次采集，而不是无限拉长单窗

  数据量参考：10s 间隔 × 60 分钟 × 64 引擎 ≈ 2.3 万行 CSV，解析仍在秒级，不用担心时长带来的开销

### A2. 部署与执行

```
# 1. 本地生成 urls.txt (每行一个 http://ip:port/metrics)
# 2. 上传两个文件到采集机 (ssh-mcp-server_upload)
#    scripts/extract_metric_v2.sh -> /tmp/lbprof/extract_metric_v2.sh
#    urls.txt                     -> /tmp/lbprof/urls.txt
# 3. 执行 (ssh-mcp-server_execute-command, timeout 设 DURATION+180000 ms)
chmod +x /tmp/lbprof/extract_metric_v2.sh
cd /tmp/lbprof && nohup ./extract_metric_v2.sh <DURATION秒> urls.txt > run.log 2>&1 & echo "started pid $!"
# 4. 轮询直到完成 (每 60s 一次, tail -3 run.log, 看到 "已达到设定采样时长" 即完成)
tail -3 /tmp/lbprof/run.log; ls -la /tmp/lbprof/dp_load_metrics_*.csv
```

注意事项：

- v2 脚本从 urls.txt 读节点列表，**不需要修改脚本内容**；不用 nohup 也可前台执行，但 execute-command 的 timeout 必须大于采样时长
- PD 分离集群（一次同时采 P、D 两池）不用 v2，改用下方 A-PD 小节的 `extract_metric_pd.sh`
- 结束时脚本会打印各节点最终累计 counter 与 CSV 文件名
- 若远端已有历史部署，确认用的是本 skill 的 v2 脚本（`grep ttft_sum` 表头可验证）

### A3. 下载与命名

- CSV 下载（ssh-mcp-server_download）到用户指定目录
- 按样例命名习惯重命名：`dp_load_metrics_<原时间戳>_<上下文>.csv`，上下文如集群名/策略名（例：`dp_load_metrics_20261106184634_dsv4.1_2_coordinator.csv`）。报告副标题会引用文件名，命名带上下文让报告可追溯
- 对比模式两份 CSV 建议都采相同时长；两份之间提示用户：**是否需要先切换路由策略/配置再采第二份**

### A4. 验证采集质量（下载后快速检查）

```
# 行数是否合理 (应约为 DURATION/间隔×节点数)
# 每节点是否有数据、-1(缺失)占比是否过高、时间跨度是否符合预期
```

有问题（如大量 -1、节点缺失）先告知用户并共同决定是否重采，不要直接进报告流程。

### A-PD. PD 分离集群双池同时采集

P(Prefill) 池与 D(Decode) 池需要同时间窗、时间戳对齐的数据时，用 `scripts/extract_metric_pd.sh` 替代 v2——一次运行同时采集两池，同轮 P/D 全节点并行采样：

```
# 1. 生成 p_urls.txt 与 d_urls.txt（每行一个 http://ip:port/metrics，可含 # 注释）
# 2. 上传 extract_metric_pd.sh 和两个 urls 文件到采集机（目录同 A2）
chmod +x /tmp/lbprof/extract_metric_pd.sh
cd /tmp/lbprof && nohup ./extract_metric_pd.sh <DURATION秒> p_urls.txt d_urls.txt > run.log 2>&1 & echo "started pid $!"
# 3. 轮询同 A2；结束后产出 dp_load_metrics_P_<ts>.csv 与 dp_load_metrics_D_<ts>.csv 两份（30 列 v2 格式）
```

- 采集机必须同时能访问 P、D 两池全部 metrics 端口；节点列表 P/D 各查 `references/cluster_registry.md` 取一组
- 脚本要求两组列表都非空；采样与轮询、下载命名（文件名自带 `_P_`/`_D_` 上下文）、质量验证（两份 CSV 分别按 A4 做）均同流程 A
- 报告阶段：P、D 各自生成单池分析报告。不要把 P 与 D 两份 CSV 喂成 A vs B 对比——两池工作负载口径不同（P 池看 prefill/TTFT 分流，D 池看 decode 分流），对比模式是给同池不同路由策略用的

## 流程 A2：Prometheus 时间段回采

适用：Prometheus 里已有历史数据，用户给出**起止时间**（或"最近 N 小时/某天 02:00-03:00"），不需要登录业务机轮询。产物与方式 1 **完全同构**的 30 列 CSV，直接进流程 B。

### A2.1 先确认三件事

- **数据源**：优先直连 Prometheus（如 `http://<prometheus-host>:9090`）；端口不通时用 Grafana 数据源代理（`--grafana-url` + `--user` + `--datasource-uid`，**密码经 `GRAFANA_PASSWORD` 环境变量或 `--password-file` 传入，不要写在命令行里**）。**不要**在报告里引用 Grafana 看板 UID——看板面板可能绑定了已删除的数据源 UID（实测存在：面板指向的数据源不存在，面板全空，但数据本身在默认数据源里）
- **job 与模型**：用户给模型名 + job。先 `--list-jobs` 确认 job 正则命中（`--start` 可省略，默认窗口 now-1h ~ now；输出会区分**窗口内有效**与**仅历史标签残留**——`label/job/values` 是全时段并集，集群改名后旧 job 名是残留，正则查空时会按词元模糊提示你可能想用的名字）；必要时加 `--match 'model_name="xxx"'` 二次过滤
- **时间窗与时区**：绝对时间按用户给的时区（默认 `+08:00`）；相对时间用 `now-30m` / `now-6h`。步长不给就 `auto`（自动取 ~600 点，最小 5s）

### A2.2 上传并执行

```
# 1. 上传（本地 -> 采集机/跳板机，需能访问 Prometheus 或 Grafana）
#    scripts/extract_metric_prom.py -> /tmp/lbprof/extract_metric_prom.py
# 2. 先看 job 是否命中（--start 可省略，默认窗口 now-1h ~ now；显式给出更符合目标时段）
cd /tmp/lbprof && python3 extract_metric_prom.py \
    --prom-url http://<prometheus-host>:9090 --list-jobs --job 'glm53_2_decode_.*' \
    --start now-1h --end now
# 3. 回采（Grafana 代理模式：把 --prom-url 换成下面这组；密码不进命令行，
#    经 GRAFANA_PASSWORD 环境变量或 --password-file 传入；http:// 需显式 --allow-insecure-http）
python3 extract_metric_prom.py \
    --prom-url http://<prometheus-host>:9090 \
    --job 'glm53_2_decode_.*' \
    --match 'model_name="glm53-2"' \
    --start '2026-11-17 02:00:00' --end '2026-11-17 03:00:00' \
    --step auto -o /tmp/lbprof/dp_load_metrics_prom_<ts>.csv
#   Grafana 代理模式形如（密码来源优先级 --password > --password-file > 环境变量）：
#   export GRAFANA_PASSWORD='<grafana-password>'
#   python3 extract_metric_prom.py --grafana-url https://<grafana-host>:3000 --user <grafana-user> \
#       --datasource-uid <Prometheus 数据源 UID> --job 'glm53_2_.*' --start now-1h --step 30s
```

**PD 双池一次回采**（时间窗与步长天然对齐，产出 `_P_`/`_D_` 两份）：

```
python3 extract_metric_prom.py --prom-url http://<prometheus-host>:9090 \
    --pool 'P=glm53_2_prefill_.*' --pool 'D=glm53_2_decode_.*' \
    --start now-1h --end now --step 30s -o /tmp/lbprof/pd/
```

**按模型名一次选中该部署全部 job**（不用逐个列 job；自动按 `service_role` 拆成 decode/prefill 两份，健康探针等无角色序列自动跳过并提示）：

```
# glm53-2 当前 = 64 decode + 8 prefill（job 前缀 glm53_2_*）
python3 extract_metric_prom.py --prom-url http://<prometheus-host>:9090 \
    --model glm53-2 --start now-1h --step 30s -o /tmp/lbprof/glm53_2/
# 产出 dp_load_metrics_glm53_2_decode_<ts>.csv / dp_load_metrics_glm53_2_prefill_<ts>.csv
```

- `model_name` 可能被多个部署共用（实测 `glm52_*` 早前也挂过 `glm53-2`）：脚本用窗口内**存活序列**判断实际部署，命中多个部署时会打印警告并列出，加 `--job 'glm53_2_.*'` 收窄即可
- `--split-by none` 可合并成一份（仅当确认角色口径一致时用）；`--split-by <label>` 可换成其它标签（如 `dashboards_tag`）

耗时 ≈ 25 条聚合查询 × 分段数（与引擎数量无关；别名回退时略有增加）：常规窗口（≤1 万点）秒级完成，超长窗口（>11000 点）自动分段后按段数线性增加，`execute-command` 的 timeout 给 300000ms 足够。

### A2.3 参数速查

| 参数 | 说明 |
| --- | --- |
| `--start` / `--end` | `now` / `now-30m` / `-2h` / `30m`（start，表示最近30分钟）/ `"2026-11-17 02:00:00"` / unix 秒；`--start` 仅 `--list-jobs` 模式可省略（默认窗口 now-1h ~ now） |
| `--step` | `auto`（默认）/ `15s` / `1m`；超过 11000 点自动分段拼接 |
| `--job` / `--pool` | 单个或正则 job；`--pool NAME=正则` 走 PD 双池多文件输出 |
| `--model` | 按 `model_name` 一次选中该部署全部 job；默认按 `service_role` 自动拆分输出，可与 `--job` 组合收窄 |
| `--split-by` | `--model` 模式的拆分标签，默认 `service_role`；`none` = 合并一份；也可换 `dashboards_tag` |
| `--match` | 附加 label 过滤（AND），如 `model_name="glm53-2"`、`service_role="glm53_2_decode"` |
| `--grafana-url` / `--user` / `--datasource-uid` | Grafana 代理模式；密码来源优先级 `--password` > `--password-file` > 环境变量 `GRAFANA_PASSWORD`，**推荐环境变量/密码文件**（`--password` 会留在 ps/history/agent 轨迹）；向 `http://` 的 Grafana 发凭证需显式 `--allow-insecure-http` |
| `--tz` | 时间解析与 CSV 时间戳时区，默认 `+08:00` |
| `--negative-hist` | `auto`（默认，修正已知 queue_sum<0 问题）/ `off`（保留原值） |
| `-o` / `--out-dir` | 单池：`-o` 为 CSV 文件路径（省略则写 `--out-dir/dp_load_metrics_prom_<ts>.csv`）；`--pool` 模式：`-o` 视为目录，输出 `dp_load_metrics_<池名>_<ts>.csv` |
| `--tag-label` | 生成 `ip_port` 的标签，默认 `instance`（Pod 级粒度非 IP:端口 时可改） |

### A2.4 产物与校验

- 表头与 v2 **逐列一致**（30 数据列 + queue_fixed 标记列），`ip_port` 为 `ip_port` 或 `ip_port.engine`（同一实例多 engine 时追加引擎号，保证主机/端口分组仍可解析；`process_start_time_seconds` 等无 engine 标签的序列自动合并进同实例分组）
- 脚本自带体检并打印：引擎数、每引擎点数、缺失率、覆盖率不足 90% 的引擎、process_start 一致性（跨引擎/跨段累计口径是否可比）、queue_sum 为负的引擎及修正说明；**缺失率 >30% 或大面积掉点先用 `--list-jobs`/放宽 `--match` 复核，再进报告**
- 时间戳是采样点本身的时间（非采集时刻），所以报告里的「窗口」= 用户给的起止时间；timestamp 落地为可读时间（`--tz` 口径），与现场采集一致

### A2.5 与方式 1 的口径差异（必须知道）

1. **累计量求和**：`request_success_total` 会按 `finished_reason`（stop/length/abort/error/repetition）拆成多条序列，脚本用 `sum by(引擎)` 汇总——与 v2 的 `snap_counter` 求和一致；不要自己写 `max`
2. **缺失填 -1**：该采样点无序列则整行对应列为 `-1`，报告端视为缺失（不会当 0）
3. **counter 回落**：Prometheus 侧若窗口内发生重启，脚本不抹平，报告端识别为疑似重启并弃用增量（与方式 1 相同）
4. **queue/prefill 已知导出 bug**：部分实例 `request_queue_time_seconds_sum` 为负且 `prefill_time_sum` 同步虚高（两者相加才是真实排队+预填时长）。默认 `--negative-hist auto` 把这些点修正为 `queue=0, prefill=queue+prefill`，**CSV 末列 `queue_fixed=1` 标记被修正的点**（v2/Prom 采集均为 33 列表头 = 30 数据列 + queue_fixed），并打印修正清单；需要原始值对比时用 `--negative-hist off`。三种采集路径修正口径一致
5. **`kv_cache_usage_perc` 是 0~1 比值**（不是百分数）；命中率按 `hits_total/queries_total` counter 累计比落盘（queries=0 时记 0），仅旧版 vLLM 有 gauge 时回退 gauge 口径
6. **`process_start_time_seconds` 无 `engine` 标签**（部分部署）：脚本自动把这种"幽灵 tag"（如 `ip:port`，仅 1 列数据）合并进同实例的 `ip:port.engine` 分组，避免引擎数翻倍；采集结束时打印 process_start 一致性检查结论

## 流程 B：解析并生成报告

### B1. 识别输入

- 单份 CSV → 单份均衡性分析报告；两份 CSV → A vs B 对比报告。例外：PD 双池采集产出的 `_P_`/`_D_` 两份 CSV 是两个池的数据，应各自生成单池报告（见 A-PD），不要做成 A vs B 对比
- CSV 格式自动识别：20 列（原版，TTFT 为累计平均，报告自动挂口径警示）/ 30 列（v2，TTFT 用窗口内平均）；时间戳兼容可读时间与 epoch；`queue_fixed` 列可选。格式可疑时用 `--print-schema` 输出识别结果排查
- 从文件名/用户描述提取 A/B 标签（如 proxy_new / coordinator）与报告标题；拿不准就问

### B2. 生成报告

```
python "<skill目录>/scripts/generate_report.py" \
  --csv <a.csv> [<b.csv>] \
  --label-a "<A标签>" [--label-b "<B标签>"] \
  --title "<报告标题>" \
  -o <输出路径> \
  --summary-out <摘要.json>
```

- 输出路径**先询问用户**
- 脚本会打印关键指标摘要（引擎级 CV、主机内失衡、方差分解、口径提示），据此向用户口头总结
- 报告自包含单文件 HTML，直接浏览器打开即可

### B3. 可选：附加深度分析

脚本生成的结论是规则化的。若发现值得展开的点（如端口偏重方向的共性、某引擎异常、口径差异影响），可：

1. 读 `--summary-out` 的 JSON（含全部指标与每引擎明细）
2. 写一段 markdown 分析，用 `--extra-analysis extra.md` 重新生成报告，分析会渲染在综合结论后
3. 指标口径与结论规则的严格定义见 `references/report_spec.md`——解释报告数字时以该文件为准

## 报告结构速览

每个数据板块末尾附一行「说明」小字，介绍该板块指标含义与阅读方法（不下结论，判断留给读者）。

**单池**：头部结论横幅（判定 + CV/LIF/失衡来源/热点/时延一句依据）与关键指标卡片（CV / LIF / 失衡比）→ 一、数据口径（与对比版同字段：数据文件、时间窗口、时长、指标列数、引擎数、累计请求数、窗口增量请求数、流量强度；采样点数×步长并入质量检查小字）→ 二、均衡性计分卡（效应量固定档：LIF、失衡比 max/min、队列、TTFT；CV 工程经验分级：请求/主机间/主机内按 <0.05 极度均衡 / 0.05~0.15 健康均衡 / 0.15~0.30 轻度倾斜 / >0.30 严重倾斜；Token CV 为参考值；小样本时挂样本护栏，此时 CV 分级只展示不参与判定）→ 三、请求分配（窗口增量口径：窗口请求增量、请求占比带内嵌条形（蓝）、累计请求数 counter 末值带内嵌条形（灰，长度=累计占比，与蓝条对照可区分本窗新增失衡 vs 长期倾向）——数据缺失时该列不显示）→ 四、失衡来源（一句话来源结论 + 主机间/主机内占比卡片 + 主机级对照表带内嵌条形）→ 五、Token 负载与缓存命中（输入/输出 token、本地/外部命中率、KV 均值 + KV 峰值；输出 token 带内嵌条形）→ 六、TTFT 与时延分解（TTFT/Queue/Prefill/E2E 逐引擎 + 相关性卡片）→ 七、窗口内均衡走势与引擎分流（引擎 / 增量（内嵌条形）/ 窗口速率 / running 均值一张表）→ 八、等待队列分布（waiting 均值/峰值/非零占比/请求占比、waiting 累计占比带红色内嵌条形并标注「集中」top1；零星毛刺自动降级不参与计分）→ 九、综合结论与建议（分点合并列出结论与建议；短窗口判定非均衡时附「窗口范围说明」）。

**对比**：头部 A/B 总览卡片（引擎数、窗口请求总量、请求 CV、LIF、失衡比、主机内方差占比）+ 带指标对比（请求 CV / LIF / 主机间 CV / TTFT CV）的结论横幅 → 一、数据口径对比 → 二、计分卡对比（A/B 各自效应量评级，CV 为参考值；小样本时按池挂样本护栏）→ 三、请求分配对比（A/B 请求增量与占比，按变化幅度降序）→ 四、失衡结构对比（A/B 主机间/主机内占比卡片 + 主机级对照表（蓝/橙内嵌条形））→ 五、Token 与缓存命中对比（输出 token、外部命中率、KV 均值/峰值 A/B）→ 六、TTFT 与时延对比（TTFT/Queue/Prefill/TPOT/E2E 每格 A/B 对照，说明含 vLLM 官方时延口径）→ 七、窗口内分流对比（引擎 / A、B 增量（蓝/橙内嵌条形）/ 窗口速率 A/B / running 均值 A/B 对照表）→ 八、等待队列对比（集中度/waiting CV 卡片 + waiting 均值/峰值/非零占比对照表）→ 九、综合总结（逐项指标佐证 + 占优方 + 改善幅度；两窗均 <30 分钟时提示长时间窗复核）。

## 边界与注意

- 单池采集默认用 v2（单次 curl/节点/轮 + 精确匹配 + hist sum/count）；PD 双池同时采集用 `scripts/extract_metric_pd.sh`（同 v2 口径，P/D 全节点同轮并行采样，一次产出两份 CSV）；原版 `scripts/extract_metric_single.sh` 仅作历史参考（20 列），其 CSV 仍可解析
- **timestamp 列统一写人类可读本地时间** `YYYY-MM-DD HH:MM:SS`（三种采集路径一致），报告端同时兼容 epoch 秒与 ISO8601；解析不了的行计入告警，全坏则非零退出并提示原因（可用 `--print-schema` 排查）
- **采集 curl 一律带 `--noproxy '*'`**：集群节点常设 `http_proxy`，走代理会被拦截成 403 伪装成正常响应（实测被解析成 -1）
- **指标名口径（对齐当前 vLLM 版本）**：TPOT 用 `vllm:request_time_per_output_token_seconds_*`（旧名 `time_per_output_token_*` 作为回退别名）；命中率由 `hits_total/queries_total` counter 累计比计算（queries=0 记 0），不再依赖旧版 gauge。vLLM v1 天然不导出的列（`swapped`、`request_failure_total`、`request_validation_time_total`、`cpu_cache_usage_perc`、`pending_prefix_cache_hit_rate`）为 -1，报告端识别为「该部署未导出」——**不计入缺失率分母**，并在数据口径脚注标注，不会误触 >30% 告警
- 术语：「引擎」= 一个采集分组（`ip_port` 或 `ip_port.engine`），现场采集下 1 个 `ip:port` 端点 = 1 个 vLLM 服务实例（引擎数 = 实例数）；Prometheus 回采同一实例多 engine 时按 engine 细分；`process_start_time_seconds` 等无 engine 标签的序列会自动合并进同实例分组（避免引擎数翻倍）
- 评级：**效应量固定档**（LIF ≤1.2/1.5/2.0、失衡比 max/min ≤1.3/1.7/2.2、队列与 TTFT 固定档）+ **CV 工程经验参考区间**（<0.05 极度均衡 / 0.05~0.15 健康均衡·生产合格线 / 0.15~0.30 轻度倾斜·建议队列深度/剩余 KV 感知路由 / >0.30 严重倾斜·考虑 Prefix Cache 感知、PD 分离、扩缩容）。CV 区间无业界统一标准（vLLM 等官方未发布 CV 目标），为工程经验总结；小样本时随机底噪 1/√N̄ 即可落入轻度倾斜区间（每引擎 44 个请求时底噪≈0.15），故样本护栏激活（N̄<200）时 CV 分级只展示、不参与横幅判定；Token CV 为参考值不评级（受请求长度构成影响）。**工业对照**：sgl-router cache_aware 的 balanced 模式用"负载比例（--affinity-load-factor，默认 2）+ 绝对差（--affinity-load-gap）"双阈值决定是否打破缓存亲和（旧 balance_abs/rel_threshold 约 1.1~1.2 已移除），口径为瞬时队列、与本报告窗口累计 LIF 不同，仅证明"尾部失衡容忍带 + 绝对差护栏"是工业通行做法。最终判据以业务影响（TTFT 相关性、排队、KV、抢占）为准，详见 `references/report_spec.md` §3
- **短窗 vs 长窗判读**：窗口判定只回答"采样时段内是否失衡"，长窗口/累计均衡只说明"路由无长期系统性偏置"。现场短窗（<30 分钟）采集发现失衡但长窗均衡 = 时段性现象（突发/相位流量），处置方向是突发应对（队列感知路由），不是调长期权重；窗口 <30 分钟且判定非均衡时报告自动挂「窗口范围说明」，A/B 对比两窗均短时提示用长时间窗（Prometheus 回采，相同 step）复核。判读顺序：样本护栏排除噪声 → 窗口内业务影响定即时处置 → 长窗口确认系统性偏置
- **方式 2（`extract_metric_prom.py`）的产物与 v2 同构（30 数据列 + queue_fixed 标记列）**，可直接复用 `generate_report.py`；它与方式 1 的唯一差异是数据来自 Prometheus 存储（写入时间点、抓取间隔可能被下采样），因此：① 窗口内点位数由 `--step` 决定，跨数据集对比时两份必须用**相同 step 与相同时长**；② 超出 Prometheus 保留期的时段取不到数据（会报"未取到任何序列"并列出可用 job）
- `--list-jobs` 会区分**窗口内有效 / 仅历史标签残留**（`label/job/values` 返回全时段并集，改名后的旧 job 名是残留，直接用会查空）；无命中时会按词元做模糊提示（如 `glm53_prefill_*` → `glm53_0_prefill_*`）；`job` 正则查空时先看提示再怀疑保留期
- Prometheus 侧 `job` 与 Pod/端口是多对一或一对多关系，取决于部署：glm53 集群实测 1 job = 1 引擎（`instance` 唯一），同一实例多引擎时脚本会自动把 `engine` 并入分组并输出 `ip_port.engine` 形式的 tag
- Grafana 看板的**面板数据源 UID 可能已失效**（面板全空）而默认数据源正常：先 `/api/datasources` 确认 UID，再决定直连还是代理，不要看到板子空就判定"没数据"
- **urls.txt / urls 文件白名单校验**：节点列表常来自工单、聊天、wiki 等非可信文本，三个采集脚本（v2 / pd / single）对 urls 每行做白名单校验——仅接受 `http(s)://` 开头且只含 `A-Za-z0-9 . _ : / -` 的 URL，含引号/分号/命令替换等字符或 `file://` 等非 HTTP 协议的行直接报错退出（防注入与本地文件经 curl 抓取混入 CSV）
- skill 内脚本不内置任何真实节点列表，节点列表一律通过 urls.txt / `--job` / `--pool` 传入；如从外部工作副本同步脚本，只移植逻辑改动，不要把真实节点列表拷回 skill
- 对比结论跨数据集只比**分布形态与窗口增量**，不比累计绝对量（不同服务实例）；流量强度差 >1.5x 时报告会自动警示；两段 uptime（process_start）不一致或**负 queue 修正比例差异 >20%** 时报告会挂醒目警示（修正差异不是排队差异，逐引擎看 queue+prefill 合计）
- CSV 中 `-1` 为采集缺失兜底，解析端自动视为缺失；counter 回落视为疑似重启，增量弃用；报告对负 queue 修正过的引擎在数据口径脚注标注（queue=0 可能是被修正而非真无排队）
- **累计口径可比性验证**：跨引擎解读累计 counter 前，建议从采集机对各引擎 metrics 端点抓取 `process_start_time_seconds`（一次 curl 循环，采集前顺带做连通性检查即可）确认 uptime 一致。uptime 不一致 → 累计不可比；uptime 一致 → 累计差异为真实长期差异，但**当前状态评估仍以窗口增量口径为准**——引擎会动态加入/退出分发（PD 集群实测：每池存在 1~2 个窗口内无新请求的引擎；按窗口速率反推的"有效分流时长"可远小于实际 uptime），累计口径放大的是历史差异
- 若节点无法访问或某节点持续 -1，先与用户确认网络/节点状态再决定取舍
