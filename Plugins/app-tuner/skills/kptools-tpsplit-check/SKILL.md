---
name: kptools-tpsplit-check
description: >
  根据线上 vLLM-Ascend 部署的实测显存数据，测算「当前 TP 能否在单卡显存约束下继续切分成更多 DP」，并生成可行性分析报告（Markdown）。自动采集 npu-smi 单卡显存、模型 config/权重分片、vLLM 启动参数与显存 profiling 日志，按「全卡复制 / TP 切分 / EP 切分」三分类拆解权重，做候选 TP 的每卡容量校验与 DP 扩容收益测算。当用户提到以下场景时必须使用本技能（即使没有明说「可行性」或「报告」）：单卡显存够不够 / 还有多少余量、TP 还能不能切小、TP32 能不能改 TP8、能不能多开几个 DP 实例、扩容测算、显存预算、KV cache 还能放多少、EP 组不变只调 TP、160 卡怎么分、卡数怎么分配、跑不跑得下、MTP/激活占多少显存、  权重按 TP/EP 怎么分片、tpsplit 前的可行性检查。产出的报告与启动参数 diff 供人工或部署流程执行切分变更（本 skill 只读测算，不执行部署）。打点日志解析、TTFT 拆解出表归 kptools-tpsplit-log-analysis（本 skill 只做切分前的静态显存测算，不解析日志）。只要涉及推理实例的显存容量规划、TP/DP/EP 拓扑决策或单卡显存归因，都应该用这个 skill。
license: MulanPSL-2.0
compatibility: 远端 Linux NPU 环境（npu-smi、vLLM-Ascend 日志、docker exec）用于采集；分析脚本为纯 Python 3 标准库（无需 torch/numpy），可在远端或本地 Windows 运行；本地是 Windows 时不要直接执行 collect_env.sh，先上传到远端跑。连接方式优先 ssh-mcp-server，无 MCP 时回退本地 ssh/scp。
metadata:
  skill_type: tool_assisted
  category: analysis
  domain: tp_dp_memory_feasibility
  version: "0.2.0"
---

# kptools-tpsplit-check：TP → DP 扩容可行性测算（单卡显存约束）

评估当前集群 TP size 是否可以进一步切分，产出可行性分析报告（.md + .json）。报告结论与第 8 节启动参数 diff 供用户确认后人工执行（或走团队变更单/部署流程）；本 skill 只读测算，不执行部署。

## 这个 skill 回答三个问题

1. **现在单卡显存里装了什么？** 权重按「全卡复制 / TP 切分 / EP 切分」拆开，KV、激活、其他各自多少。
2. **在显存约束内，当前 TP 还能不能切小？** TP 切小 = 每 DP 卡数变少 = 同样卡数下能开更多 DP。
3. **切了之后收益多少、坑在哪？** DP 倍数、每 DP KV 容量变化、通信/带宽/PD 分离的影响。

不要一上来就「总权重 ÷ 卡数」——MoE + MLA 模型这样算会错得很离谱，见下面的三分类。

## 核心算式

```text
每卡权重(tp)   = 复制桶 + TP桶/tp + EP桶/ep_group + 额外桶(未归类,每卡复制)
KV 池/卡(tp)   = 单卡可用 − 每卡权重(tp) − 激活 − 其他
每 DP token 容量 = KV池/卡 ÷ 每 token 每卡 KV 字节
DP 数          = 总卡数 ÷ (tp × pp)
可行性         = KV池/卡 > 0 且 每DP token 容量 ≥ max_model_len（至少放得下 1 条满长请求）
```

参考量级（GLM5.2 W4A8，单卡 64G，引自用户提供的估算）：

```text
复制 3.98G | TP切分 9.60G | EP切分 362.35G（占四桶总量 382.33G 的 94.8%）
TP32: 3.98 + 9.60/32 + 362.35/32 = 15.6G
TP8 : 3.98 + 9.60/8  + 362.35/32 = 16.5G   (+0.9G，不是 ×4)
可用 54G = 权重 22.9G(主干16.5+MTP等6.4) + KV 26.8G + 激活 2.5G + 其他 1.8G
160 卡: 160/32 = 5 DP → 160/8 = 20 DP (×4)
```

## 为什么必须分三桶

| 桶 | 典型参数 | 每卡量 | 随 TP 变小 |
| --- | --- | --- | --- |
| 全卡复制 | norm/bias、router gate + e_score_correction_bias、MLA 降维投影（`q_a_proj`/`kv_a_proj_with_mqa`） | 1 份 | **不变**（卡越多越浪费） |
| TP 切分 | attention（`q_b_proj`/`kv_b_proj`/`o_proj`/qkv）、embedding/lm_head、dense MLP | /tp | **变大** |
| EP 切分 | 路由专家（`experts.<n>.*`） | /ep_group | **不变**（跟 EP 组走） |

**MTP 层（`model.layers.<num_hidden_layers>.*`）与普通层同规则分桶**（attention→TP、experts→EP），不要整层按复制算——那会每卡凭空多算十几 GiB。只有日志校准差额和手工指定的未归类量才进「额外桶」（每卡复制，保守口径）。

W4A8 MoE 里 EP 桶占九成以上，所以 TP 减半时每卡权重只涨 TP 桶那点零头。**这也是本 skill 存在的意义**：报告要能用一页表格说服人「TP8 只多 0.9G，但 DP 能 ×4」。

## 关键约束：EP 组大小不变

vLLM 的 EP 世界大小 = `tp × dp`（`--enable-expert-parallel`）。所以「TP 32→8 而 EP 保持 32」只有在 **DP 从 1 变 4** 时才成立：

```text
ep_group = tp × dp_per_ep_group     # 单组 32 卡（supernode / 单机域）
dp_per_group = ep_group / tp        # 必须整除
集群 DP 数 = 总卡数 / (tp × pp)
```

专家权重按 `ep_group` 除，不按 tp。`ep_group` 默认按**实例 TP×DP** 推断（vLLM EP 世界大小 = tp × dp；采集端会输出 `EP_GROUP_SIZE`），缺 DP_SIZE 时标未知并告警、不拍默认值，可用 `--ep-group` 显式覆盖。

## 工作流程

### 第 1 步：确认输入

| 参数 | 说明 | 示例 |
| --- | --- | --- |
| 机器 / 容器 | 宿主机 SSH + 容器名（可为空=日志/模型在宿主机可见） | `192.0.2.13` / `vllm-ascend-glm52-p-gt` |
| vLLM 启动脚本 | **参数最可靠的来源**，`--script` 传入；脚本内 `$var` 自动展开 | `/data/scripts/start_prefill.sh` |
| 模型目录 | 完整权重目录（要 `config.json` + `*.safetensors`，读 header 即可，不加载权重） | `/data/models/GLM-5.2-W4A8` |
| vLLM 日志 | 含显存 profiling 的启动日志 | `/data/.../log/prefill.log` |
| 卡数 / EP 组 | 集群总卡数、EP 组卡数（默认当前 tp×dp） | `160` / `32` |
| 候选 TP | 默认取 `ep_group` 的约数 | `32,16,8` |
| 输出路径 | md + json | `D:\workspace\...\feasibility.md` |

缺模型目录或日志也能跑，但会退化成「只能报现状、不能扫候选 TP」。退化时必须在报告里写明。

### 第 2 步：线上采集（只读）

**采集一律在宿主机执行**——容器内 `npu-smi` 常返回空（单卡显存会全缺），且宿主机 `ps` 能看到所有容器的 vllm 进程（参数可能拿错实例），所以：**有启动脚本就给 `--script`**（脚本内 `$var` 自动展开），`ps` 仅兜底并在 facts 里标记 `PARAM_SOURCE=ps`。日志/模型只在容器内可见时加 `--docker`，脚本自动改用 `docker exec` 读取。

```bash
# 宿主机上（推荐）
bash collect_env.sh --log <LOG> --model <MODEL_DIR> --script <启动脚本> > facts.env
# 日志/模型在容器里时（采集仍在宿主机，读文件走 docker exec）
bash collect_env.sh --docker <C> --log <容器内LOG> --model <容器内模型目录> --script <启动脚本> > facts.env
```

`collect_env.sh` 只做读取：`npu-smi info`/`-t usages`（主表优先，老卡只有 `HBM Usage Rate(%)` 时输出百分比兜底；同时输出与卡号无关的 `NPU_TOTAL_MB` 等别名键，`--npu` 采非 0 卡时分析端照样能读）、`ps -eo args`（命令行只保留测算所需白名单参数，`--api-key` 等敏感项不落盘）、`config.json` 存在性、日志 grep（**双格式**：vLLM 标准 `Model weights take` 与 vLLM-Ascend `Actual usage: X GiB for weights`；数值一律从指标短语之后提取，行首时间戳不会污染结果）。**不改配置、不重启、不写目标目录**。首次使用先 `bash -n collect_env.sh` 查语法。

拿回 `facts.env` 后先扫一眼这几个键是否非 0：`NPU_TOTAL_MB`（或任意 `NPU<id>_TOTAL_MB`）、`TP_SIZE`、`DP_SIZE`、`LOG_MODEL_WEIGHTS_GIB`、`LOG_AVAILABLE_KV_GIB`、`LOG_DESIRED_TOTAL_GIB`、`MAX_MODEL_LEN`。缺任何一个都会在报告顶部触发「降级告警」并降级对应口径（缺 `MAX_MODEL_LEN` 时候选一律 unknown、缺 `DP_SIZE` 时不算 DP 基线倍数）；`PARAM_SOURCE=ps` 时优先补 `--script` 重采。

### 第 3 步：测算

纯 Python 标准库，远端（能访问模型目录）或本地（拿 facts + 权重 json）均可跑。只想先看测算口径与输入清单（不需要任何数据）：`python analyze_feasibility.py --dump-plan`，可单独运行。

```bash
python analyze_feasibility.py \
  --facts facts.env \
  --model-dir <MODEL_DIR> \
  --total-cards 160 --ep-group 32 \
  --tp-candidates 32,16,8 \
  --out feasibility.md --json feasibility.json
```

没有模型目录时用 `--weights-json` 直接给分桶，适合手工核算或复现别人给的表：

```bash
# 复现本 skill 参考量级（不需要模型目录）
python analyze_feasibility.py --facts examples/facts.env.example \
  --weights-json examples/glm52_weights.json \
  --total-cards 160 --ep-group 32 --tp-candidates 32,16,8 --out glm52.md
# -> 每卡 22.00/22.90 GiB，KV 26.80，DP 5 -> 20 (x4)
```

脚本内部顺序：读权重 header → 按名字分桶（MTP 层与普通层同规则）→ 用日志每卡权重（`Model weights take` 或 Ascend `Actual usage ... for weights`，双格式）**校准**分桶（差值记为「未归类」并按每卡复制回灌）→ 扫候选 TP → 容量校验 → DP 收益 → 出 md。

`--total-cards` 是**整个集群**的卡数（不是单个 EP 组）；和当前 `TP×DP×PP` 对不上时报告会报警。也可以不传，此时按当前 `tp×dp×pp` 估算。

**校准这一步不要跳过**：自动分桶是启发式，实测每卡权重和计算值差 >5% 时，差额就是漏掉的桶（MTP、embedding 副本、量化 scale 等）。报告里会单列一行，看到它就该用 `--overrides` 修正而不是硬信计算结果。

### 第 4 步：交付报告

固定结构（脚本已按此生成，手改时保持）：

```text
1. 结论（可行/不可行 + 推荐 TP/DP + 收益倍数）
2. 运行态事实（硬件/模型/部署/日志实测值 + 数据来源标注）
3. 权重三分类拆解（桶总量 + 每卡量 + 校准差额 + Top 张量表）
4. 候选 TP 试算表（tp / dp / 每卡权重 / KV池 / 每DP容量 / 可行性）
5. 单卡容量校验（分项相加 vs 可用，逐 TP）
6. DP 扩容收益（每DP卡数 / DP数 / 组内DP / 集群KV总量）
7. 风险与前提（每DP KV容量、带宽、EP all-to-all、PD 分离、KV 传输、碎片）
8. 建议启动参数 diff（只生成文本，不执行）
9. 假设与未测项
```

报告里的数字**一律带单位 GiB 和来源标记**（`实测` / `日志推算` / `假设` / `计算`），否则别人无法复核。用户认可结论后，按第 8 节参数 diff 人工执行或走团队变更单部署（本 skill 只读，不代执行）。

## 常见坑

- **采集在宿主机做**：容器内 `npu-smi` 常返回空（显存键全缺）；宿主机 `ps` 能看到所有容器进程（TP/max_model_len 可能拿错实例）→ `--script` 优先，`ps` 兜底会标记 `PARAM_SOURCE=ps` 并在报告里告警。
- **MLA 的 KV 是每卡复制**：`kv_lora_rank` 存在时 KV cache 不随 TP 切分，TP↓ 不减小每卡 KV，但 DP↑ 会让**集群 KV 总量按 DP 数增长**；MHA 模型则相反（KV 随 TP 切）。脚本会自动判定，报告里两种结论不一样，别套错。
- **日志格式有两种，都要认**：vLLM 标准 `Model weights take X GiB` 与 vLLM-Ascend `Actual usage: X GiB for weights`（KV 是 `Actual usage: X GiB for kv cache`）。`Model weights take` 是**每卡值**（device profiling 结果），拿它和「总权重」比会差 tp 倍。
- **W4A8 的权重文件是打包后的字节**：直接读 safetensors header 的 nbytes 就是真实占用，不要按 bf16 反推。
- **MTP 层按普通层规则分桶**：`model.layers.<num_hidden_layers>.*` 名字和普通层一样，attention→TP、experts→EP；整层按复制算每卡会虚增十几 GiB。若模型目录缺失，靠日志校准差额兜底。
- **可用显存 ≠ 总显存**：优先读日志 `Desired usage ... total`（vLLM 自己的预算口径）；其次「日志权重+日志KV+Total反推开销」；`总显存×util−保留` 只是兜底推算（可能多算 2-3 GiB）。优先用「`Total usage − 每卡权重 − 日志KV`」反推非 KV 开销，不要拍激活值。
- **每 token KV 实测反推优先**：`KV池÷token数` 或 `Maximum concurrency × max_model_len`；config 公式（`(kv_lora_rank+rope)×2×层数`）会漏 DSA indexer 等附加缓存，偏低约 8%，只能兜底。
- **老卡 npu-smi 只有 HBM Usage Rate(%)**：采集脚本主表优先、MB 行校验（总量 ≥1024MB 才认，防止把 78% 当 78MB），拿不到 MB 时输出百分比，分析走日志口径。
- **PD 分离**：P 和 D 的 `max_model_len`/KV 需求不同，要分别算两次再合并结论；D 侧还有 KV 接收 buffer。
- **只读**：本 skill 不改配置、不重启引擎。生成的启动参数 diff 由用户自己确认后人工执行（或走团队部署流程），本 skill 不代执行。
- **与日志解析 skill 的边界**：本 skill 只做切分前的静态显存测算；打点日志解析、TTFT 拆解出表归 `kptools-tpsplit-log-analysis`，日志解析类请求不要发给本 skill（两者的 tpsplit 触发词同域，靠「测算/显存」vs「解析/出表」区分）。

## 文件

- `scripts/collect_env.sh`：只读采集，输出 KEY=VALUE（首次使用先 `bash -n collect_env.sh` 查语法）
- `scripts/analyze_feasibility.py`：分桶 + 测算 + 报告（`--selftest` 复现参考量级，报告写临时目录不弄脏工作区；`--dump-plan` 可单独查看口径与输入清单）
- `examples/facts.env.example` / `examples/glm52_weights.json` / `examples/glm52_report.md`：GLM5.2 全流程样例（样例报告只此一份，运行产物不入库）
- `cases/skill-contract/`：tier-A 契约用例（跑 `--selftest` 并断言退出码 / 通过数 / 工作区干净，产物在 `results/` 不入库）
- `references/weight-classification.md`：张量名 → 桶的完整规则表与例外
- `references/memory-budget.md`：显存预算各项口径、KV 公式、可用值来源
