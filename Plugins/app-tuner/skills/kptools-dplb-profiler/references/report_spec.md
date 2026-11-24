# report_spec：dp_load_metrics 口径与指标定义

本文件是 `generate_report.py` 解析逻辑与三个采集脚本输出格式的**唯一权威定义**。解释报告数字、排查口径分歧时以本文件为准。

## 1. CSV 格式定义

两种口径，报告端自动识别：表头含 `ttft_sum` → **v2/Prom（30 数据列）**；否则含 `ttft_avg` → **原版（20 数据列）**。

CSV 首两列固定为 `timestamp`（人类可读本地时间 `YYYY-MM-DD HH:MM:SS`，报告端兼容 epoch 秒与 ISO8601；时间取采样点本身而非采集时刻）与 `ip_port`（引擎标识，`ip:port`、`ip_port` 或 `ip:port.engine`，host/port/引擎分组按此解析）。缺失值统一填 `-1`，解析端视为缺失（绝不当 0）。

### 1.1 基础 20 列（两口径共有）

| # | 列名 | 来源指标 | 类型 | 说明 |
|---|------|---------|------|------|
| 1 | num_requests_running | vllm:num_requests_running | gauge | 运行中请求数 |
| 2 | num_requests_waiting | vllm:num_requests_waiting | gauge | 等待队列长度 |
| 3 | num_requests_swapped | vllm:num_requests_swapped | gauge | 换出请求数 |
| 4 | request_success_total | vllm:request_success_total（按 finished_reason 等标签求和） | counter | 成功请求累计 |
| 5 | request_failure_total | vllm:request_failure_total | counter | 失败请求累计，端点无此序列填 -1 |
| 6 | num_preemptions_total | vllm:num_preemptions_total | counter | 抢占累计 |
| 7 | prompt_tokens_total | vllm:prompt_tokens_total | counter | 输入 token 累计 |
| 8 | generation_tokens_total | vllm:generation_tokens_total | counter | 输出 token 累计 |
| 9 | request_validation_time_total | vllm:request_validation_time_total | counter | 请求校验耗时累计（秒），无则 -1 |
| 10 | kv_cache_usage_perc | vllm:gpu_cache_usage_perc（回退 vllm:kv_cache_usage_perc） | gauge | **0~1 比值**，非百分数 |
| 11 | cpu_cache_usage_perc | vllm:cpu_cache_usage_perc | gauge | 0~1 比值 |
| 12 | prefix_cache_hit_rate | 优先 vllm:prefix_cache_hits_total ÷ vllm:prefix_cache_queries_total（counter 累计比，queries=0 记 0）；回退旧版 gauge vllm:prefix_cache_hit_rate | gauge/派生 | 本地前缀缓存命中率，可能恒 0 |
| 13 | pending_prefix_cache_hit_rate | vllm:pending_prefix_cache_hit_rate | gauge | 无则 -1（vLLM v1 不导出） |
| 14 | external_prefix_cache_hit_rate | 优先 vllm:external_prefix_cache_hits_total ÷ vllm:external_prefix_cache_queries_total；回退旧版 gauge | gauge/派生 | 外部前缀缓存命中率 |
| 15 | process_start_time_seconds | process_start_time_seconds | gauge | 进程启动时间，用于 uptime 一致性检测 |
| 16 | ttft_avg | 派生：ttft_sum/ttft_count 采样点快照 | 派生 | 累计平均；count=0 填 -1 |
| 17 | queue_avg | 同上（vllm:request_queue_time_seconds） | 派生 | 累计平均；负 queue 已修正（见 1.3） |
| 18 | prefill_avg | 同上（vllm:prefill_time_seconds，回退 vllm:request_prefill_time_seconds） | 派生 | 累计平均；吸收负 queue 差值（见 1.3） |
| 19 | tpot_avg | 同上（vllm:request_time_per_output_token_seconds，回退 vllm:time_per_output_token_seconds） | 派生 | 累计平均 |
| 20 | e2e_avg | 同上（vllm:e2e_request_latency_seconds，回退 vllm:request_latency_seconds） | 派生 | 累计平均 |

### 1.2 v2/Prom 额外 10 列（30 列口径）

| # | 列名 | 来源指标 | 说明 |
|---|------|---------|------|
| 21 | ttft_sum / ttft_count | vllm:time_to_first_token_seconds 的 _sum/_count | 窗口内平均 = Δsum/Δcount |
| 22 | queue_sum / queue_count | vllm:request_queue_time_seconds | 同上 |
| 23 | prefill_sum / prefill_count | vllm:prefill_time_seconds（回退 request_prefill_time_seconds） | 同上 |
| 24 | tpot_sum / tpot_count | vllm:request_time_per_output_token_seconds（回退 time_per_output_token_seconds） | 同上 |
| 25 | e2e_sum / e2e_count | vllm:e2e_request_latency_seconds（回退 request_latency_seconds） | 同上 |

原版 CSV = 首两列 + 20 基础列（共 22 列表头）；v2/Prom CSV = 首两列 + 30 数据列 + queue_fixed 标记列（共 33 列表头）。技能文档中「20 列 / 30 列」均指**数据列数**。

### 1.3 公共列口径

- **timestamp**：统一为人类可读本地时间 `YYYY-MM-DD HH:MM:SS`（三种采集路径一致）；报告端兼容 epoch 秒与 ISO8601（含时区），无法解析的行跳过并计入告警。
- **queue_fixed（v2/Prom 末列，可选）**：1 = 该采样点 queue_sum<0 被修正（已知 vLLM 导出 bug：queue 为负且 prefill 同步虚高）。修正口径：queue=0、prefill_sum += 原 queue_sum，三种采集路径一致；报告对被修正引擎在数据口径脚注标注。
- **缺失与未导出**：`-1` 为采集缺失兜底；全数据集 100% 缺失的列视为「该部署未导出」（如 vLLM v1 的 swapped / request_failure_total / request_validation_time_total / cpu_cache_usage_perc / pending_prefix_cache_hit_rate），不计入缺失率分母，报告脚注标注。

## 2. 指标定义

- **引擎（engine）**：一个采集分组（`ip_port` 或 `ip_port.engine`）。现场采集（v2/PD 脚本）下 1 个 `ip:port/metrics` 端点 = 1 个 vLLM 服务实例，此时引擎数 = 实例数；Prometheus 回采按 `(instance, engine)` 分组，同一实例暴露多 engine 序列时按 engine 细分，引擎数可大于实例数。报告中的「引擎数」「最热引擎」等表述均指该分组单位。
- **CV（变异系数）**：总体标准差 / 均值。σ 用总体方差（除以 N）。参与样本为各引擎的有效值（缺失 -1 剔除）；有效引擎 <2 或均值 ≤0 → 不计算（显示 n/a）。
- **失衡比**：max / min（min > 0；min ≤0 时显示 n/a）。
- **失衡差（max-min）**：最热与最闲引擎窗口增量的绝对差（请求数）。工业路由器普遍以"比例阈值 + 绝对差护栏"双条件做均衡决策（绝对差防低负载时比例失真），故与失衡比并列展示；绝对量纲跨流量强度不可比，不设固定评级档。
- **排队 LIF**：各引擎 waiting 窗口均值（毛刺引擎剔除）的 max/mean，全零或均值≤0 → n/a。与 sgl-router 等工业路由器的排队感知均衡同源，但路由器作用于瞬时队列深度、本指标为窗口均值，仅作对照参考。
- **LIF（尾部失衡因子）**：max / mean，衡量最热个体相对平均的偏离倍数。与 CV 互补：CV 描述整体离散度、对尾部不敏感，LIF 直指最热点——最热引擎承载 LIF 倍均值流量，将最热引擎 (1−1/LIF) 的窗口增量迁出即可回到均值（报告建议语据此生成）。引擎级 LIF 基于各引擎请求 Δ；主机级 LIF 基于主机 Δ 之和（主机 ≥2 才计算）。LIF ≤1 为理论上限外异常（不可能），显示 n/a 的条件与 CV 相同（有效样本 <2 或均值 ≤0）。LIF 受引擎数影响（LIF ≤ n），跨不同规模池对比时以 CV 为主判据、LIF 辅助判断尾部。
- **窗口增量（Δ）**：counter 类 = 末段有效值 − 首段有效值，按段求和；**counter 回落视为疑似重启**，在该点切段，重启点前后的增量弃用（不跨重启相减）。同一引擎多次重启全部切段处理。
- **窗口平均时延（v2）**：hist 列窗口平均 = Δsum / Δcount（Δ 按上条规则）。Δcount ≤ 0 → n/a。
- **累计平均时延（原版）**：直接使用 *_avg 列的窗口均值，**口径为累计平均而非窗口平均**，报告自动挂口径警示，且该维度不参与计分卡。
- **主机间 / 主机内**：host = ip_port 去掉端口与引擎后缀。主机级负载 = 该主机全部引擎 Δ 之和。主机间 CV = 各主机 Δ 的 CV；主机内端口级 CV = 各主机内部引擎 Δ 的 CV（单引擎主机不参与），报告取按主机数加权均值与最大值。
- **方差分解**：总体方差 = 主机间 + 主机内（全方差定律：between = Σ_h (n_h/N)(μ_h−μ)²；within = Σ_h (n_h/N)σ_h²）。报告给出两者占比，用于判定失衡主要来源。
- **等待队列指标**：每引擎 waiting 的窗口均值 / 峰值 / 非零占比（>0 样本数 / 有效样本数）；**零星毛刺判定：非零占比 < 5% 的引擎降级为「毛刺」，不参与队列维度计分与集中度**，仍在明细中列出并绘图。
- **排队集中度**：**非毛刺引擎** waiting 的时间累计值（Σ_t w_e(t)）中最大者占非毛刺合计的比例——毛刺引擎不进分子分母（一次瞬时排队尖峰不应被报成"集中度 100%"），其占全窗口 waiting 的比例在报告第八节单列展示。0 = 完全分散，接近 1 = 排队集中于单一引擎。非毛刺引擎全零窗口 → n/a。
- **TTFT 与负载相关性**：各引擎（请求占比, 窗口平均 TTFT）对的 Pearson 相关系数 r。有效引擎 <3 或任一侧方差为 0 → n/a。|r|≤0.3 弱、0.3~0.7 中等、>0.7 强。
- **时延分解（vLLM 官方口径）**：请求时延由事件链 arrival_time → QUEUED → SCHEDULED → 首个 NEW_TOKENS 构成（vLLM V1 metrics 设计文档「Interval Calculations」）。TTFT ≈ 输入处理 + Queue（QUEUED→SCHEDULED）+ Prefill（SCHEDULED→首个 NEW_TOKENS），首 token 在预填充末产生；抢占会重置 Prefill 间隔并计入 TTFT。据此可用 Queue/Prefill 定位 TTFT 差异来源：Queue 差异大 = 过载型（分流不均），Prefill 差异大 = 序列长度或算力差异。
- **流量强度差**：对比模式 A/B 总请求数之比，>1.5 或 <1/1.5 自动挂警示（对比结论只看分布形态与窗口增量）。
- **累计口径可比性**：process_start_time_seconds 各引擎不一致 → 累计列不可比，挂警示；一致 → 累计差异为长期真实差异，但当前状态评估仍以窗口增量口径为准。

## 3. 计分卡评级规则

**背景：不存在权威的行业阈值标准。** vLLM 官方 DP 部署按各引擎 running/waiting 队列与 KV cache 状态路由，不设定 CV 目标；NIST 统计手册只将 CV 定义为离散度度量之一；文献中 LIF 无标准阈值；Jain 公平性指数虽有界但其推荐线同样是工程经验。因此本报告采用两类规则并在报告中显式标注：

### 3.1 评级构成：效应量固定档 + CV 工程经验分级

**效应量固定档**（与样本量无关，直接对应"要不要动手"）：

| 维度 | 指标 | 优 | 良 | 中 | 差 |
|------|------|----|----|----|----|
| 尾部失衡 LIF | 引擎级请求 Δ 的 max/mean | ≤1.2 | ≤1.5 | ≤2.0 | >2.0 |
| 失衡比 max/min | 引擎级请求 Δ 的 max/min | ≤1.3 | ≤1.7 | ≤2.2 | >2.2 |
| 等待队列分散 | waiting CV（毛刺引擎剔除；全零→n/a） | ≤0.3 | ≤0.6 | ≤1.0 | >1.0 |
| TTFT 一致性 | 窗口平均 TTFT 的 CV（仅 v2 计分） | ≤0.10 | ≤0.20 | ≤0.40 | >0.40 |

**CV 工程经验参考区间**（请求分配 / 主机间 / 主机内三行；**无业界统一标准**，vLLM 等官方未发布 CV 目标，为生产运维经验总结）：

| CV 范围 | 等级 | 系统表现与风险 | 工程建议 |
|---------|------|----------------|----------|
| <0.05 | 极度均衡（理想） | 算力/KV 显存对齐，长尾抖动很小 | 维持策略；多见于离线批、请求长度统一场景 |
| 0.05~0.15 | 健康均衡（生产合格线） | 动态波动正常，无单点瓶颈，P99 稳定 | 在线推理推荐区间，一般不用干预 |
| 0.15~0.30 | 轻度倾斜（亚健康） | 部分引擎短时排队、KV 偏高，部分闲置 | 检查轮询/QPS 路由缺陷；改队列深度/剩余 KV 感知路由 |
| >0.30 | 严重倾斜（告警） | 热点+饥饿节点，抢占/OOM 风险，P99 陡增 | 升级调度：Prefix Cache 感知、PD 分离、扩缩容 |

**Token 负载 CV 为参考值不评级**：token 分布受请求长度构成影响，不单纯反映路由质量。

**工业对照**：SGLang sgl-router 的 `cache_aware` 策略以"比例阈值 + 绝对差"双条件决定是否打破前缀缓存亲和——`--affinity-mode balanced` 下仅当优选引擎负载同时超过 *备选 × `--affinity-load-factor`*（默认 2）与 *备选 + `--affinity-load-gap`*（绝对差）才切换；早期版本的 `balance_abs_threshold` / `balance_rel_threshold`（约 1.1~1.2）参数已在现行版本移除。本报告的 LIF / 失衡比 / 失衡差与之同思路，但路由器负载口径为瞬时等待 prefill token 或 running requests，本报告为窗口累计请求，阈值不能直接互套。vLLM 官方未发布类似的 CV/LIF 阈值标准。

**与噪声的关系**：CV 大小受窗口请求量随机波动影响（底噪 1/√N̄），小样本时纯噪声即可落入轻度倾斜区间（每引擎 44 个请求时底噪≈0.15）。因此样本护栏激活（N̄<200）时 CV 分级**只展示、不参与横幅判定**；是否干预以效应量与业务影响为准。CV÷噪声归一化不采用：它只回答"能否与随机波动区分开"，不回答"差异是否重要"，且零假设（独立等概率随机分配）与真实路由（哈希/最少负载，有系统性偏置）不符，并非检验统计量。

### 3.2 小样本护栏（不参与评级）

当 N̄ < 200 时，计分卡下挂「样本护栏」提示：纯随机分发的 CV 底噪 ≈ 1/√N̄；若请求 CV > 底噪，按方差分解给出"约 (底噪/CV)² 的方差可能来自随机波动"，并提示以 LIF/失衡比等效应量与业务影响为准、必要时扩大采样时长复测。N̄ ≥ 200 时不展示噪声信息（大样本下噪声对判读无实质影响）。对比模式对 A、B 两池分别判断、分别提示；**A/B 任一池护栏激活时，对比横幅与综合总结同样不作胜负判定**（CV 差值可能来自随机波动，与单池横幅口径一致）。

### 3.3 优先看业务影响

档位只用于快速分诊。判断"是否需要干预"更权威的依据是业务影响：TTFT 是否随负载恶化（r）、排队是否集中、KV 利用率是否接近上限、是否出现抢占。报告第八节与横幅据此给出结论。

## 4. 结论规则

1. 总体判定：横幅判定由**效应量行**（LIF、失衡比、队列、TTFT，始终参与）与 **CV 分级行**（请求/主机间/主机内，样本护栏未激活时参与）共同驱动——任一达到「差 / 严重倾斜」→ **显著失衡**；存在「中 / 轻度倾斜」→ **存在倾斜**；全部优/良/均衡 → **基本均衡**。Token CV 参考行不参与。报告横幅在判定后附一句关键依据：请求 CV/尾部 LIF、失衡主要来源（主机间占比或主机内占比，主机间时点名最重主机）、最热引擎请求占比与倍数、TTFT 相关性（≥0.7 标注过载型）、排队集中度（>0.5 时点名）。
2. 主机内失衡占比 >60% 且主机内端口 CV >0.10 → 结论指向「同主机端口间分流偏斜」，建议检查哈希键/一致性哈希环/端口级亲和配置。
3. 主机间失衡占比 >60% 且主机间 CV >0.10 → 指向「主机间调度权重不均」，建议核对负载入口的节点权重与健康检查。
4. 排队集中度 >0.5 → 指出排队集中的引擎（列出 top1 及其占比；top1 与集中度均基于非毛刺引擎，毛刺引擎不参与该判定），建议核查该引擎慢节点/长序列聚集。
5. TTFT 相关 r > 0.5 且请求 CV >0.10 → 结论「高负载引擎时延恶化（过载型 TTFT）」。
6. LIF > 1.5 → 生成尾部失衡建议：列出最热引擎承载倍数与迁载比例 (1−1/LIF)，指向路由权重/溢出配置。
7. **窗口范围语义（短窗 vs 长窗）**：窗口判定只回答"采样时段内是否失衡"，不回答"是否存在长期系统性偏置"。窗口短于 30 分钟且判定非均衡时，报告第九节自动挂「窗口范围说明」：突发/阶段性流量与小样本随机波动都会推高短窗 CV；若更长时间窗（Prometheus 回采）或累计口径显示均衡，说明路由无长期偏置，短窗失衡属于**时段性现象**——但它对窗口内用户体验是真实的（时延与排队按即时发生计），处置方向是突发应对（队列感知路由、突发吸收）而非调整长期权重。判读顺序：先过样本护栏（排除纯噪声）→ 看窗口内业务影响（TTFT 相关性、排队、KV）决定是否需要即时处理 → 再用长窗口确认是否存在系统性偏置。对比模式两窗口均较短时，第九节同样提示"短窗 A/B 对比易被突发主导，建议长时间窗复核"。
8. 对比模式：以请求 CV 为主判据（Δ≥0.02 才认为有实质差异），**但 A/B 任一池样本护栏激活（N̄<200）时不作胜负判定**——横幅与综合总结降级为"差异可能来自随机波动，以效应量与业务影响为准"（与单池 verdict 口径一致）；以「三、请求分配对比」给出每引擎 A/B 请求增量与请求增量占比（按占比变化幅度降序）；对比模式「四、失衡结构对比」以 A/B 双列卡片（主要失衡来源、主机间/主机内占比）与主机级对照表呈现结构差异；随后依次输出「五、Token 与缓存命中对比」（输出 token 增量 + 外部缓存命中）「六、TTFT 与时延对比」（TTFT/Queue/Prefill/TPOT/E2E 每格 A/B 对照）「七、窗口内分流对比」（引擎 / A、B 增量（蓝/橙条形）/ 窗口速率 / running 均值对照表）「八、等待队列对比」，底部输出「九、综合总结」，逐项列出可比指标（请求 CV、LIF、失衡比、最热引擎请求占比、主机间/主机内 CV、TTFT 一致性 CV、排队集中度）的 A/B 数值、改善幅度与占优方，并给出总体结论；流量强度差 >1.5x 时附「只看分布形态」提示。所有报告的每个数据板块末尾附一行「说明」小字，介绍该板块指标含义与阅读方法（不替读者下结论）；表格中请求占比、累计请求数（灰条，长度=累计占比）、输出 token、waiting 累计占比等关键数值列带内嵌条形图（浅灰轨道 + 比例长条 + 数字标注，waiting 系列为红色）。条形列宽随表的列数自适应：3~4 列宽表 40%、7 列密表 18%、双条形列各 16%（单池请求分配表蓝=窗口占比/灰=累计占比，对比版蓝/橙双色各一列），避免条形列过宽导致其他列头换行。
9. 口径警示不改变计分，但「TTFT 一致性」在原版口径下不计分、只展示并挂警示。

## 5. 口径一致性检测（报告第一节「数据口径」）

单池报告第一节「数据口径」与对比模式同字段：数据文件、采集时间窗口、窗口时长、指标列数（v2 30 列 / 原版 20 列）、引擎数、累计请求数（从进程启动开始计数）、窗口内新增请求数、窗口流量强度（req/s）；采样点数与步长并入表下质量检查小字（「采样 N 点 × Ns」）。质量检查小字还包含：各引擎缺失率（最高值及引擎）、疑似重启清单、process_start_time_seconds 一致性（累计口径是否可比）；表下另附「该部署未导出的列（全缺失，不计入缺失率）」与「负 queue 修正」脚注（如存在）。存在缺失率 >30% 或疑似重启时另挂醒目警示块。对比模式在此基础上额外包含两文件对照、相同引擎数与采样点数×步长说明，并附带引擎集合/步长时长/流量强度差/**两段 uptime 不一致**/**负 queue 修正比例差异 >20%** 警示（后者提示逐引擎比较 queue/prefill 时以 queue+prefill 合计为准）。
