---
name: kptools-tpsplit-report
description: 分析 TP 拆分（tpsplit）前后两套 TTFT 分阶段表格与打点日志，生成收益分析对比报告（Markdown + HTML），明确收益点并给出三级证据链（claim → 表格分位数 → 原始日志行）。当用户提到 TP 拆分前后对比、tpsplit 收益、收益分析、收益对比报告、收益归因、收益点、证据链、TTFT 对比、拆了之后效果怎么样、TP32 改 TP8 值不值、优化效果评估、before/after 数据对比、收益点要有日志证据时务必使用本技能，即使用户只说"帮我看下拆分赚了多少"。输入是两套本地数据目录（ttft_table.csv + 打点 .log + config.json），输出 .md/.html/.json 三件套。数据在远端时先让用户回传（本 skill 不做 SSH 采集）；只对比 TTFT 及其阶段拆解，吞吐/显存/长稳不在范围内要明说。
license: Proprietary
compatibility: 纯 Python 3 标准库，本地运行（Windows/Linux 均可），不连远端、不改引擎、只读输入目录。输入表格口径来自 kptools-tpsplit-log-analysis（解析）与 kptools-tpsplit-log-trace（打点部署）；字段与日志格式定义见 references/stage-glossary.md。
metadata:
  skill_type: tool_assisted
  category: analysis
  domain: ttft_benefit_analysis
  activation_mode: active
  side_effect_free: "true"
  version: "0.1.0"
---

# kptools-tpsplit-report：TP 拆分收益对比报告（表格 + 日志 → 收益点 + 证据链）

对比 TP 拆分前后两套数据（TTFT 分阶段表格 + 打点原始日志），产出固定结构的收益分析报告：总收益幅度、每个收益点归因到阶段、每个点带三级证据链，顺带暴露数据质量问题和回退风险。

## 这个 skill 回答四个问题

1. **拆了之后 TTFT 变好多少？** mean/P50/P90/P99 全分位数对比，不是只看均值。
2. **改善来自哪里？** 按阶段分解贡献度（Δ阶段 ÷ ΔTTFT），收益点是 `sched_qwait` 还是 `fwd_pure` 一目了然。
3. **证据在哪？** 每个收益点三级成链：结论 → 表格分位数 → 原始 BREAKDOWN 日志行（`文件:行号` + req_id 可复核）。表格是解析产物可能有 bug，日志才是 ground truth。
4. **有什么坑？** 守恒违例、input_len 分布混杂、低样本、回退阶段——报告自动检出，不要在有问题数据上硬下结论。

## 快速决策

1. 用户给了两套数据目录要对比 → 走完整流程（下节）
2. 只有表格没日志 → 照跑，报告自动降级（L3 缺失标「止步于表格层」），交付时向用户说明
3. 控制台/报告出现**数据质量告警** → 先向用户解释告警（守恒违例/混杂/低样本各自什么含义），给结论时以分层对比为准，建议是否补采
4. 数据在远端机器 → 本 skill 不做 SSH：让用户先用 kptools-tpsplit-log-analysis 出表并把表格+日志回传到本地目录
5. 用户要对比吞吐/显存/长稳 → 超出范围，明说（报告第 8 节也写明了未测项）
6. 只是演示/测试/冒烟 → 用 `scripts/synth_samples.py` 造仿真数据再跑
7. 用户追问"这个收益点的证据给我看" → 直接引用报告第 5 节对应证据链，必要时按链里的 req_id 去 grep 原始日志给更多行

## 输入约定

每套数据一个目录（before / after 各一），命名建议 `before_tp32ray` / `after_tp8dp` 这种带拓扑的：

```text
<side>_dir/
├── ttft_table.csv   # 每请求一行：ts,request_id,input_len,ttft_ms + 10 个阶段 _ms 列（必须）
├── p_engine.log     # 打点原始日志：[TTFT][BREAKDOWN] v3 等（可选，缺则 L3 降级）
├── proxy.log        # [LB_DISPATCH] 行（可选）
└── config.json      # label/tp_size/dp_size/model/deploy_note（可选，报告引用）
```

阶段字段、日志行格式、守恒式、TP 敏感性（哪个阶段预期怎么变、为什么）都在 `references/stage-glossary.md`——**解读收益成因前必读**，报告里的归因要和它的预期方向对得上，对不上要说明原因。

## 工作流程

### 第 1 步：确认输入

| 要确认的 | 缺省处理 |
| --- | --- |
| before / after 两个目录的路径 | 必须问清（哪边是拆前哪边是拆后，别反） |
| 输出前缀 `--out` | 用户没说就放到 workspace 下，起名 `<前缀>tpsplit_gain` |
| 阈值是否要调 | 默认值见下表；用户关注小量级阶段（如 kv_xfer）时调小 `--regress-ms` |

预检：目录存在、`ttft_table.csv` 行数 > 0、（有 .log 时）日志里 grep 得到 `TTFT][BREAKDOWN`。缺文件能跑但要告诉用户会降级。

### 第 2 步：跑分析

```bash
python scripts/analyze_tpsplit_report.py \
    --before <before_dir> --after <after_dir> \
    --out <输出前缀>
```

**不要手算指标、不要手写报告章节**——脚本输出可复现且口径统一，三个产物一次生成：`.md`（评审用）、`.html`（图表 + 可折叠证据链，给汇报用）、`.json`（下游程序读）。控制台摘要会打印 top 收益/回退点和数据质量判定，直接用来向用户汇报第一句话。

### 第 3 步：检查告警并解读

- 数据质量 OK → 直接给结论：总收益幅度 + 主要收益点（按贡献度）+ 回退点是否可接受
- 有告警 → 先解释告警再给结论；混杂告警（input_len 分布不一致）时**以第 7 节分层对比为准**，别拿原始均值差说事
- 归因解释用 glossary 的 TP 敏感性：如 qwait 降 → DP 分流了 burst；fwd_pure 降 → 小 TP 通信开销少。用户问"为什么"时能落地到机制，而不是复读数字

### 第 4 步：交付

给用户三个文件路径 + 一段话摘要（结论 → 收益点 → 风险 → 未测项）。提醒：收益结论只覆盖 TTFT；吞吐/显存/长稳要另行验证（报告第 8 节原话）。

## 判定阈值（默认值）

| 判定 | 规则 | 为什么 |
| --- | --- | --- |
| 收益点 | Δms ≤ −10 且 Δ% ≤ −15 | 绝对值滤掉毫秒级噪声，相对值抓机制性变化，双条件都要过 |
| 回退点 | Δms ≥ +5 或 Δ% ≥ +30 | 用「或」：小量级大百分比（如 kv_xfer）也是机制信号，值得点名 |
| 低样本 | 单侧 < 100 条 | 分位数在小样本上不稳，P99 无意义 |
| 守恒违例 | 单侧 > 5% 行 | ttft ≠ Σ阶段 = 解析 bug 或打点丢字段，整份结论打折扣 |
| 混杂 | input_len 桶占比 L1 > 0.15 | 两侧请求长度分布不同时，均值差里混着长度效应 |

## 常见坑

- **均值改善 ≠ 普遍改善**：qwait 的 burst 尾部（P99）和常态（P50）可能差好几倍，收益要分位数+分层一起看再下结论。
- **守恒违例先怀疑管道，别怀疑用户**：多半是 log-analysis 解析或打点字段缺失；报告只标比例，定位要回到 log-trace 的第 4 节环境变量排查。
- **`kv_xfer` 的口径**：`defer_finalize` 下 engine 侧 load_kv 恒 0 属已知设计，真实值靠日志回填——对比它时先确认两侧都是回填值。
- **两侧压测参数不同**（QPS、并发、输出长度）：排队类阶段（qwait/fe_inputq）的对比直接不成立，要重采而不是解释。
- **Windows 控制台打印中文可能乱码**（GBK 控制台）：产物文件本身 UTF-8 正常，让用户直接开文件，别对着控制台校对。
- **前后目录放反**是最低级也最致命的错：跑完先看结论方向是否符合常识（拆分后 qwait 应该降），怪异就检查 --before/--after。
- **GB 级日志**：脚本会把全部 BREAKDOWN/LB_DISPATCH/KVXFER/DRAFT 行读入内存建证据索引（每请求约 5 行）；日志超大时先在数据目录 `grep -E "TTFT\]\[BREAKDOWN|LB_DISPATCH|TTFT_KVXFER|TTFT_DRAFT|start_load_kv" *.log > p_engine.slim.log` 预筛再跑。

## 文件

- `scripts/analyze_tpsplit_report.py`：主分析（输入两套目录 → md/json/html 三件套）
- `scripts/synth_samples.py`：仿真数据生成（clean / quality-trap / tables-only 三种场景，演示与测试用）
- `references/stage-glossary.md`：阶段定义、守恒式、日志行格式、TP 敏感性表、证据链三级结构
- `evals/run_checks.py`：脚本行为回归（三场景分类、质量告警、降级模式、错误路径、证据链回溯、确定性）
- `evals/evals.json` / `evals/eval_queries.json`：端到端评测集与触发评测集（正负例）
- `cases/skill-contract/`：tier-A 契约用例（frontmatter/资源完整性/内容卫生/脚本回归）
