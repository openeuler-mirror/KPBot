---
name: vllm-eplb-optimization
description: 在 vLLM 的 P/D 分离部署里启用、切换或还原 MoE 专家负载均衡（EPLB），并验证它到底有没有收益。当用户提到 EPLB、专家负载均衡、dynamic EPLB / force EPLB、DYNAMIC_EPLB、enable_force_eplb、--eplb-config、num_redundant_experts、冗余专家、MoE 热点 rank、prefill TTFT 长尾、VLLM_USE_V2_MODEL_RUNNER、run_dp_template.sh 调参，或者问"EPLB 怎么开""EPLB 有没有效果""怎么验证 EPLB 收益"时，务必使用本技能——即使用户只说"开一下 EPLB"或"压测数据不理想，换一套配置"。
license: MulanPSL-2.0
metadata:
  skill_type: tool_assisted
  category: tuning
  domain: vllm_moe_eplb
  activation_mode: active
  side_effect_free: "false"
  version: "1.0.0"
---

# EPLB 优化（vLLM MoE 专家负载均衡）

## 它解决什么问题

MoE 的 router 逐 token 选 top-k 专家。流量集中在少数专家时（超长重复前缀、单一领域、单租户压测），
持有热专家的 rank 成为长尾，TTFT 被最慢的那个 rank 拖住，其余 rank 在等它。EPLB 给热专家多复制
几份副本，按实测负载把副本重新摊到各 rank，把长尾削平。

代价是真实的：副本占显存，重平衡要搬专家权重并伴随一次周期性抖动；而流量本身就均衡时，重排只有开销
没有收益。同一套配置在不均衡数据上实测收益 9.2%，在混合在线流量上反而劣化 26.8%。所以**"开了 EPLB"
不等于"优化了"，必须用不均衡流量验证**——这是本技能最重要的一条。

## 先判断是否真的不均衡

EPLB 只在"专家负载确实倾斜、且这个倾斜就是瓶颈"时才有收益。同一套配置在不均衡数据上 +9.2%，在接近
均衡的流量上 −26.8%：差距不来自参数，而来自"该不该开"。

所以当用户还没给出不均衡证据、或在问"要不要开 / 值不值得开"时，先走配套的 `vllm-eplb-analysis`。
它用 balancedness、per-engine 在途请求、per-engine TTFT、prompt token 四个独立指标交叉判定，输出
`imbalanced / borderline / balanced / inconclusive`。拿到 `verdict.json` 后这样对接：

| 分析结论 | 本技能动作 |
|---|---|
| `imbalanced` + `action=optimize_dynamic/optimize_with_cap` 且 `handoff.redundant_experts` 非空 | 用 `handoff` 的 `redundant_experts / window_size / step_interval` 执行 `--mode dynamic` |
| `imbalanced` + `action=free_memory_first`，或 `redundant_experts=null` | 先释放/测量显存，不要开 dynamic |
| `borderline` | 执行 `--mode force` 做上限探针，测完 `--mode off` 还原 |
| `balanced` / `inconclusive` | 不动模板，回到分析继续收证据 |

分析同时给出 `handoff.template_changes`（`env` / `unset_env` / `add_flags` / `additional_config`）。
只想按清单手工改时可以跳过本技能；需要确定性落盘（备份、幂等、写后目标态校验、`bash -n`）时，再用本技能
的脚本执行同一份参数。两种方式不要叠加——同一组 flag 加两次会让模板自相矛盾。

用户已经明确说"确认过不均衡，帮我开/切"时，跳过分析，直接按下一节的默认参数执行。

## 三种状态

三种状态互斥，切换时必须同时改环境变量和 serve 参数：

| 状态 | V2 runner | 环境变量 | serve 参数 | 定位 |
|---|---|---|---|---|
| `off` | `=1`（仅当模板原本开了 EPLB，否则不改 V2） | 无 `DYNAMIC_EPLB` | 无 eplb 参数 | 基线，用来做对照 |
| `dynamic` | `=1` | `export DYNAMIC_EPLB="true"` | `--enable-eplb` + 3 个 `--eplb-config.*` | **优化手段，唯一可上线形态** |
| `force` | `=0` | `unset DYNAMIC_EPLB` | 在 `--additional-config` JSON 里加 `"enable_force_eplb":true` | 均衡性能的**参照上限**，不可上线 |

`force` 不解决不均衡，它是把"不均衡"从测量里拿掉，回答"如果专家完全均衡还能快多少"。所以它只用于
给 `dynamic` 的剩余空间定标：TTFT 低者优，**收益 = `off − dynamic`**，距上限的剩余空间 =
`dynamic − force`（均为正值表示改善）。永远不要把 force 当作交付配置。

> 目标环境：本技能面向本站点内部的 vLLM 发行分支部署。`DYNAMIC_EPLB` 与 `enable_force_eplb`
> 是该分支的开关，上游 vLLM 不含这两个名字（上游 EPLB 的开关只有 `--enable-eplb` / `--eplb-config.*`，
> 且对设备后端有限制）。在上游 vLLM 上照搬 dynamic/force 会静默无效——force 探针测到的仍是原配置。
> 换环境前先确认分支，否则第三节的"上限"结论不成立。

## 硬性约束

这些约束都来自现场踩过的坑，脚本已经内建，手改时同样要守住。

先决条件：**没有不均衡证据不要开 `dynamic`**。判据见 `vllm-eplb-analysis`；只凭"别人都开了"或
单看一个 balancedness 数字就上，是最常见的负收益来源。

1. **只改 P（prefill）节点**的 `run_dp_template.sh`。D 节点、代理、mooncake 脚本一律不动。
   脚本用正向判据：`--kv-transfer-config` 的 JSON 里必须是 `"kv_role":"kv_producer"`；`kv_consumer`
   或没有角色标记的模板一律拒绝，不靠"没有 kv_consumer 就当 P"来猜。
2. `dynamic` 依赖 V2 model runner（`VLLM_USE_V2_MODEL_RUNNER=1`），`force` 依赖 V1（`=0`）。
   两者互斥，只改一半等于配置矛盾。
3. 已有 `--additional-config` 时必须**合并进那个 JSON**。出现第二个同名 flag 会让前一个静默失效、
   丢配置，而且 `bash -n` 查不出来。
4. 关闭 dynamic 的写法是 `unset DYNAMIC_EPLB`（unset 不接受 `=`）。
5. 改文件前留带时间戳的备份，出问题能立刻回退。
6. **不代重启服务、不代跑压测**。重启要按现场 IP/目录在 P/D/代理三处依次执行，把命令交给用户。
7. **只写用户指定的那个启动模板**：不读凭据、不访问网络、不改其他文件；写入走同目录临时文件 + 原子替换。

## 开工前先确认三件事

- **改哪个文件**：P（prefill）节点的 `run_dp_template.sh`。判据是脚本里的 `--kv-transfer-config` 带
  `"kv_role":"kv_producer"`；带 `"kv_consumer"` 的是 D 节点，EPLB 不配在那里。用户没给路径就直接问，
  不要猜着改。
- **用哪个模式**：用户说"开 EPLB / 优化 EPLB"默认 `dynamic`（唯一可上线形态）。只有用户明确要测
  "完全均衡的上限"、或要做 force 对比时才用 `force`。拿不准就问一句，别默默选。
- **改前是什么状态**：先跑 `--mode check`，避免在混合态上再叠一层改动。

## 怎么改：用脚本，不要手写 sed

`scripts/apply_eplb.py` 是确定性改写器。它做的判断（识别 P/D 节点、合并 JSON、保持幂等、写后
`bash -n` 并校验目标状态）靠肉眼和 sed 逐条确认既慢又容易漏，所以直接用它：

```bash
python3 <skill>/scripts/apply_eplb.py --template <P节点 run_dp_template.sh> --mode check    # 只读诊断
python3 <skill>/scripts/apply_eplb.py --template <...> --mode force --dry-run                # 看 diff 不写
python3 <skill>/scripts/apply_eplb.py --template <...> --mode dynamic --ep-size 16
python3 <skill>/scripts/apply_eplb.py --template <...> --mode off

# dynamic 调参（默认 16 / 1000 / 1000；--ep-size 为 dynamic 必填，用于校验冗余副本的整除性）
python3 <skill>/scripts/apply_eplb.py --template <...> --mode dynamic \
    --ep-size 16 --redundant-experts 16 --window-size 1000 --step-interval 1000
```

| 参数 | 默认 | 含义 |
|---|---|---|
| `--ep-size` | dynamic 必填 | EP rank 数；提供后校验/向上取整 `--redundant-experts`，避免写入非整数倍；`--mode dynamic` 未提供时直接拒绝写入 |
| `--redundant-experts` | 16 | 冗余专家副本数。越多越能削峰，但显存与重排搬运量同步上升（比例与显存换算见下） |
| `--window-size` | 1000 | 负载统计滑动窗口（步），决定判断"谁热"看多长的历史 |
| `--step-interval` | 1000 | 重平衡周期（步）。压测短于这个周期就永远不触发，收益会是 0 |

> 这三个默认值沿用本站点 runbook。vLLM 官方默认是 `num_redundant_experts=0`、`window_size=1000`、
> `step_interval=3000`：照搬官方默认时压测更容易"什么都没发生"，也是常见的假负收益来源。

### 冗余专家比例怎么定

副本是收益来源，也是成本来源，两个数字要一起看：

```
ratio            = num_redundant_experts / num_logical_experts
per_rank_extra   = num_redundant_experts / ep_size            （必须整除）
physical_per_rank= (num_logical_experts + num_redundant_experts) / ep_size
显存增量（每 rank）= per_rank_extra × 单副本成本
```

`num_redundant_experts` 必须是 `ep_size` 的整数倍——vLLM 自动取值时也是向上取整到 `ep_size` 的倍数，
手填一个非倍数会得到与你预期不同的实际值。因此 `--mode dynamic` 必须提供 `--ep-size`（缺省直接拒绝写入），
脚本据此把非整数倍向上取整并显式提示，绝不把未经整除校验的值落盘。

单副本成本按 `NUM_MOE_LAYERS × BYTES_PER_EXPERT` 估算——这是"每个 rank 多一个专家槽位"的成本，
等价于全局增加 `ep_size` 个冗余专家；DeepSeekV3 的参考值约 **2.4 GiB**。因此每 rank 增量 =
`(redundant / ep_size) × 2.4 GiB`，不要再用官方公式的 `÷ EP` 版本，否则会少算 `ep_size` 倍。

经验带（不是硬规则，最终以 force 上限 + 实测收益为准）：

| 比例 | 含义 | 建议 |
|---|---|---|
| < 1% | 几乎没有多余副本，削峰能力接近 0 | 要么不开，要么至少给到 2%~5% |
| 2% ~ 12% | 常规区间 | 从这里起步 |
| 12% ~ 15% | 偏高，KV cache 被明显挤占 | 确认收益后再加 |
| > 15% | 搬运量与显存代价都大 | 只在热点极稳定时短期使用 |

以 384 logical、EP=16 为例：16 个冗余 = 4.17%，每 rank 多 1 个专家、+2.4 GiB；32 个冗余 = 8.33%，
每 rank 多 2 个、+4.8 GiB。

定值顺序：

1. 优先用 `vllm-eplb-analysis` 的 `handoff.redundant_experts`——它按失衡强度给 16/32，并已按显存余量
   做过 cap；
2. 用脚本校验整除、比例和显存是否真的放得下：
   ```bash
   python3 <skill>/scripts/recommend_redundancy.py --logical-experts 384 --ep-size 16 \
       --redundant-experts 32 --bytes-per-expert-gib 2.4 --free-gib 3
   ```
3. 放不下时按脚本给出的 `max_affordable_redundant_experts` 收敛，或先降 KV 压力；不要硬顶。

`--strict` 在"显存不够"或"比例不在 recommended 带（2%~12%）"时返回 2，适合放到部署前检查里。单副本成本
不确定时，先用小值启动一次，对比两次启动日志里的 `Available KV cache memory` 反推。

两个常见错误：为了"更均衡"盲目翻倍副本（KV cache 变少，长上下文先崩）；把比例算成"相对物理专家数"
而不是相对 logical 专家数。

脚本保证：只接受 `kv_role=kv_producer` 的 P 节点模板；拒绝没有 `vllm serve` 的脚本；没有
`--enable-expert-parallel` 时拒绝开 dynamic（EP 是 EPLB 的前提）；EPLB 参数与其它参数写在同一行时
拒绝改写（避免误删 `--port` 之类相邻参数；布尔开关 `--eplb-config.log_balancedness` 后跟另一个 flag
同样按同行处理拒绝）；对三个参数做域校验（`window/step >= 1`、`redundant > 0`，且 dynamic 必须给
`--ep-size` 并据其校验整除、非整数倍向上取整）；`--mode off` 只按**有效配置**（忽略注释）判断
模板是否开过 EPLB，仅有注释提及 `DYNAMIC_EPLB` 的 V1 模板不会被翻转 V2 runner；符号链接先解析到真实文件；备份为
`run_dp_template.sh.bak-eplb-<mode>-<YYYYmmdd-HHMMSS>`；写入用唯一临时文件 + 原子替换；重复执行同一
模式不再改动；写后 `bash -n` 失败自动回滚，`bash` 不可用时明确标注"未验证"；最后按目标状态逐项校验。

改完只需把重启命令交给用户，示意（按现场替换 IP/目录）：

```
# P 节点
docker exec -it <container> bash
cd <P脚本目录> && bash start_mooncake.sh && bash start_p0.sh
# D 节点
docker exec -it <container> bash
cd <D脚本目录> && bash start_d0.sh
# 代理（P 节点上，端口 12800）
cd <P脚本目录> && bash start_proxy.sh
```

等 `log/prefill_*.log`、`log/decode_*.log` 显示就绪后再经代理发请求。

## 怎么验证收益

用户开了 EPLB 之后问"有没有用"，**不要拿现成的混合在线流量去压**——那种流量接近均衡，EPLB 大概率
变慢，结论会被误读成"负优化"。正确做法是造一份把不均衡放大到极致的流量。

**第一步：造数据集**

```bash
python3 <skill>/scripts/gen_imbalanced_dataset.py --out imbalanced_100x100k.jsonl \
    --num-requests 100 --target-tokens 100000 --model dsv41
```

原理是 MoE 对重复内容会反复命中同一批专家：每条请求带约 100k token 的重复长前缀，专家命中高度集中，
热点 rank 被拉长，`dynamic` 才有收益空间。输出是 dumpjsonl（每行一个 OpenAI chat 请求），配
`benchmark_serving.py --dataset-name dumpjsonl` 使用。

**第二步：三组对照，每组 4 轮**

| 组 | 配置 | 回答什么问题 |
|---|---|---|
| 基线 | `off` | 起点 |
| 优化 | `dynamic` | 实际能拿到多少 |
| 上限 | `force` | 均衡后还能再多多少 |

**开 EPLB 后的第 1 轮必须是预热离群值**（引擎在做专家统计与首次重平衡，实测 8.9s～28.7s），
算进均值会直接毁掉结论。每组跑 4 轮、剔除第 1 轮、取后 3 轮 mean TTFT 均值，基线组同口径。

**第三步：判读**

- 收益 = `off − dynamic`（TTFT 下降为正）；距上限的剩余空间 = `dynamic − force`。
  相对收益 = `(off − dynamic) / off`；不要写成 `dynamic − off`，否则方向相反。
- 若 dynamic 在人为不均衡的数据上仍无收益，先查 `window_size` / `step_interval` 是否大到压测期内
  根本没触发重平衡，而不是先怀疑 EPLB 本身。
- 在接近均衡的流量上 dynamic 变慢是**预期行为**，不能据此判定部署失败，也不能据此上线。

参考结果（32 并发，vllm 0.32.0，DeepSeek-V4.1-Flash P/D，剔首轮后 3 轮均值）：

| 数据集 | off | dynamic | force |
|---|---|---|---|
| 100 条 ~100k 重复长前缀（不均衡） | 3691.3 ms | 3350.6 ms（**收益 9.2%**） | 3265.8 ms（上限 11.5%） |
| 230 请求在线混合形态（接近均衡） | 3036.6 ms | 3850.3 ms（劣化 26.8%，预期内） | 3005.6 ms（持平） |

## 排查

| 现象 | 原因与处理 |
|---|---|
| `no vllm serve command found` | 模板不是 vLLM 启动脚本，或 serve 命令被别名/函数包了一层 |
| `no --enable-expert-parallel` | dynamic 必须以 EP 为锚点，先确认 EP 配置存在 |
| `no --additional-config block found`（force） | 模板缺该块；手工在 serve 参数里加一个（且只加一个）再跑 |
| force 下 `--additional-config` 数量不是 1 | 模板里本来就有重复 flag，先合并 |
| dynamic 无收益 | 数据集是不是不均衡的？`window_size/step_interval` 是不是大到没触发？第 1 轮剔掉了吗？ |
| 只想看当前状态 | `--mode check`，只读不改 |
| 想回退 | `--mode off`（只移除 EPLB 配置；模板从未开过 EPLB 时不会改动 V2 runner）；更彻底就用备份覆盖 |
| 报"EPLB 参数没有独占一行" | 模板把 `--enable-eplb`/`--eplb-config.*` 与其它参数写在同一行；布尔开关后跟另一 flag 也算同行；先拆成独立行，避免误删 |
| dynamic 写入的参数与预期不符 | `--ep-size` 为 dynamic 必填；未提供会被拒绝写入，提供后非整数倍会取整并提示 |
| force 探针"没有效果" | 确认运行的是本站点分支：上游 vLLM 不认识 `enable_force_eplb`，探针会静默失效 |
| `--mode check` 报"不明确/混合" | 标记互相矛盾（如 `DYNAMIC_EPLB` 不是 `"true"`、缺 eplb 参数）；用 off/dynamic 之一修正 |

## 参考资料

- `references/eplb-algorithm.md`：机制层说明（副本分配、跨 rank 放置目标、权重搬运、窗口与周期、
  参数映射），不涉及任何具体实现的源码或内部路径。调参取舍拿不准、用户追问原理时再读。
