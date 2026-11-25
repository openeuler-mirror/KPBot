---
name: kptools-dplb-report
description: 解析 dp_load_metrics CSV 并生成 vLLM DP 节点负载均衡分析 HTML 报告（单份分析 / A vs B 对比，如 proxy_new vs coordinator 两种路由策略对比）。当用户提到以下场景时使用本 skill，即使没有明说"负载均衡报告"：负载均衡分析或对比、DP 节点流量/请求分发不均、引擎间负载倾斜、主机内端口级失衡、dp_load_metrics CSV 解析、proxy/coordinator 路由策略均衡性、TTFT 与负载的相关性分析、排队集中度、均衡性报告、已有负载数据出报告等。本 skill 只负责解析与报告生成；数据采集（现场轮询 /metrics、Prometheus 时间段回采、PD 双池同时采集）由 kptools-dplb-profiler 完成或由用户提供已有 CSV。
license: MulanPSL-2.0
metadata:
  skill_type: tool_assisted
  category: profiling
  domain: dp_load_balancing
  version: 1.0.0
  activation_mode: explicit
---

# kptools-dplb-report：DP 节点负载均衡分析报告

对 `dp_load_metrics` CSV（每 vLLM DP 引擎采样 20 项基础负载指标，v2/Prom 口径 30 数据列）计算均衡性（引擎级/主机级/主机内端口级 CV、尾部失衡 LIF、失衡比、方差分解、TTFT 相关性），生成自包含 HTML 报告。

核心原则：**采集与解析分离**。本 skill 只做解析与报告，全部指标计算与 HTML 渲染由 `scripts/generate_report.py` 统一完成——不要手写 HTML 或手算指标，脚本输出可复现且口径一致。CSV 三种来源，产物格式同构，均可直接解析：

| CSV 来源 | 说明 |
| --- | --- |
| kptools-dplb-profiler 现场采集 | `extract_metric_v2.sh`（单池）/ `extract_metric_pd.sh`（PD 双池）轮询 `/metrics` 产出 |
| kptools-dplb-profiler Prometheus 回采 | `extract_metric_prom.py` 按时间段回采产出（与现场采集逐列一致） |
| 用户已有历史 CSV | 只要列格式符合 `references/report_spec.md` §1 的定义即可 |

**配套拆分说明**：采集端 kptools-dplb-profiler 已同向拆分（删除其 `scripts/generate_report.py` 与 `references/report_spec.md` 副本及报告流程，触发面收窄为纯采集，报告实现与口径权威移交本 skill）；拆分前后过渡期的行为由两侧 SKILL.md 的交接/降级路径定义，拆分完成后仓库内报告实现与口径权威仅存本 skill 一份。CSV 列名契约以本文件 `references/report_spec.md` §1 为权威，与 profiler 侧 `references/csv_format.md` 双向同步，任一侧修改列名/口径时必须同步另一侧。

## 快速决策

1. 用户已有 CSV（本地或告知路径）→ 走 [流程 B：解析生成报告](#%E6%B5%81%E7%A8%8B-b%E8%A7%A3%E6%9E%90%E5%B9%B6%E7%94%9F%E6%88%90%E6%8A%A5%E5%91%8A)
2. 用户要分析但还没有数据 → 引导先用 **kptools-dplb-profiler** 采集（现场轮询或 Prometheus 回采，见该 skill 的流程 A / A2），拿到 CSV 再回到本 skill；不要在无数据时直接编造结论
3. 对比模式（如 proxy_new vs coordinator）需要两份 CSV：已有则直接用；只有一份则提示用户——现场采需先切换路由策略/配置再采第二份，历史数据可两次 Prometheus 回采（相同时长/步长）
4. PD 分离集群产出的 `dp_load_metrics_P_*` / `dp_load_metrics_D_*` 两份 CSV 是两个池的数据，**各自生成单池报告**，不要做成 A vs B 对比（两池工作负载口径不同，对比模式是给同池不同路由策略用的）
5. 涉及报告输出路径（`-o`）时**先询问用户**，不要默认覆盖已有文件

## 流程 B：解析并生成报告

### B1. 识别输入

- 单份 CSV → 单份均衡性分析报告；两份 CSV → A vs B 对比报告。例外：PD 双池采集产出的 `_P_`/`_D_` 两份 CSV 是两个池的数据，应各自生成单池报告（见快速决策 4），不要做成 A vs B 对比
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

- **CSV 格式自动识别**：表头含 `ttft_sum` → v2/Prom（30 数据列）；否则含 `ttft_avg` → 原版（20 数据列）。「30 列 / 20 列」均指**数据列数**；对应表头总列数：v2/Prom = 首两列（timestamp/ip_port）+ 30 数据列 + `queue_fixed` 标记列，共 33 列；原版 = 首两列 + 20 数据列，共 22 列。格式可疑时用 `--print-schema` 排查；时间戳兼容人类可读时间 `YYYY-MM-DD HH:MM:SS`、epoch 秒与 ISO8601（含时区），解析不了的行计入告警，全坏则非零退出并提示原因。**同一引擎同一时间戳出现多行时取最后一行（后写覆盖，与 Prometheus 抓取语义一致）**：解析端所有序列先按时间戳排序去重，CSV 行序乱序/重复行不影响 counter 差分与 gauge 统计
- **缺失与未导出**：CSV 中 `-1` 为采集缺失兜底，解析端自动视为缺失（绝不当 0）；全数据集 100% 缺失的列视为「该部署未导出」（如 vLLM v1 的 `swapped`、`request_failure_total`、`request_validation_time_total`、`cpu_cache_usage_perc`、`pending_prefix_cache_hit_rate`），**不计入缺失率分母**，并在数据口径脚注标注，不会误触 >30% 告警
- **counter 回落视为疑似重启**，该点切段，重启点前后的增量弃用（不跨重启相减）；同一引擎多次重启全部切段处理。**counter 有效点不足 2 个（采集全缺失或仅单点）视为缺失（n/a）而非 0**：request_success_total 增量缺失的引擎从请求类均衡计算（CV / LIF / 失衡比 / 失衡差 / 方差分解 / 相关性 / 样本护栏 N̄）中剔除，明细保留 n/a 行，报告第一节标注有效引擎数
- **v2 窗口平均时延成对差分**：sum 与 count 按时间戳对齐，只累计「两端同时有 sum 与 count、且处于同一进程生命周期（counter 未回落、process_start 未变）」的采样区间——两侧各自独立跳缺失点会把单点 sum 配全量 Δcount、报成 0 延迟；无有效区间显示 n/a
- **输出路径冲突防护**：`-o` / `--summary-out` 与输入 CSV、`--extra-analysis` 或彼此指向同一文件时（归一化实路径比对：绝对路径 + 符号链接 + 大小写，含 `./` 别名；对已存在路径另做 samefile 同 inode 比对——realpath 识别不了硬链接），脚本在打开任何输出前拒绝执行并保留原文件——防止 HTML 清空原始采样、JSON 覆盖刚写出的 HTML
- **负 queue 修正解读**：CSV 末列 `queue_fixed=1` 标记 queue_sum<0 被修正的点（已知 vLLM 导出 bug：queue 为负且 prefill 同步虚高，修正口径 queue=0、prefill_sum 吸收差值）。报告对被修正引擎在数据口径脚注标注（queue=0 可能是被修正而非真无排队）；对比模式负 queue 修正比例差异 >20% 时挂醒目警示（修正差异不是排队差异，逐引擎看 queue+prefill 合计）
- 术语：「引擎」= 一个采集分组（`ip_port` 或 `ip_port.engine`），现场采集下 1 个 `ip:port` 端点 = 1 个 vLLM 服务实例（引擎数 = 实例数）；Prometheus 回采同一实例多 engine 时按 engine 细分。报告中的「引擎数」「最热引擎」等表述均指该分组单位
- 评级：**效应量固定档**（LIF ≤1.2/1.5/2.0、失衡比 max/min ≤1.3/1.7/2.2、队列与 TTFT 固定档）+ **CV 工程经验参考区间**（<0.05 极度均衡 / 0.05~0.15 健康均衡·生产合格线 / 0.15~0.30 轻度倾斜·建议队列深度/剩余 KV 感知路由 / >0.30 严重倾斜·考虑 Prefix Cache 感知、PD 分离、扩缩容）。CV 区间无业界统一标准（vLLM 等官方未发布 CV 目标），为工程经验总结；小样本时随机底噪 1/√N̄ 即可落入轻度倾斜区间（每引擎 44 个请求时底噪≈0.15），故样本护栏激活（N̄<200）时 CV 分级只展示、不参与横幅判定；Token CV 为参考值不评级（受请求长度构成影响）。**工业对照**：sgl-router cache_aware 的 balanced 模式用"负载比例（--affinity-load-factor，默认 2）+ 绝对差（--affinity-load-gap）"双阈值决定是否打破缓存亲和，口径为瞬时队列、与本报告窗口累计 LIF 不同，仅证明"尾部失衡容忍带 + 绝对差护栏"是工业通行做法。最终判据以业务影响（TTFT 相关性、排队、KV、抢占）为准，详见 `references/report_spec.md` §3
- **短窗 vs 长窗判读**：窗口判定只回答"采样时段内是否失衡"，长窗口/累计均衡只说明"路由无长期系统性偏置"。短窗（<30 分钟）发现失衡但长窗均衡 = 时段性现象（突发/相位流量），处置方向是突发应对（队列感知路由），不是调长期权重；窗口 <30 分钟且判定非均衡时报告自动挂「窗口范围说明」，A/B 对比两窗均短时提示用长时间窗（Prometheus 回采，相同 step）复核。判读顺序：样本护栏排除噪声 → 窗口内业务影响定即时处置 → 长窗口确认系统性偏置
- **对比模式跨数据集规则**：只比**分布形态与窗口增量**，不比累计绝对量（不同服务实例）；流量强度差 >1.5x 时报告自动警示「只看分布形态」；跨数据集对比时两份必须用**相同 step 与相同时长**（Prometheus 回采由 `--step` 决定点位密度）；两段 uptime（process_start）不一致时报告挂警示
- **累计口径可比性**：`process_start_time_seconds` 各引擎不一致 → 累计列不可比；一致 → 累计差异为长期真实差异，但**当前状态评估仍以窗口增量口径为准**——引擎会动态加入/退出分发，累计口径放大的是历史差异
- 若 CSV 质量可疑（大量 -1、节点缺失、时间跨度异常），先与用户确认是否重采（采集侧见 kptools-dplb-profiler），不要直接进报告
