# 干净基线消融实验报告：无现成答案的 TF 树上，skill 的价值还剩多少

> 历史案例：模型名、分支和提交号仅用于区分实验对象，不是当前环境前提。`${EXPERIMENT_ROOT}` 为用户提供的历史产物根目录，`${TF_SOURCE_DIR}`、`${SERVING_BUILD_DIR}`、`${TRANSCRIPT_DIR}` 分别为源码、构建与会话记录目录；未随 skill 打包的日志不可视为已核验或可访问的证据。
> **日期**：2026-09-08
> **背景**：第一轮消融（`${EXPERIMENT_ROOT}/ablation/REPORT.md`）被质疑效度——基线树（0e99f1bf4）内躺着 ANNC/KDNN 全套默认关闭的设施，agent 的收益主要来自"翻开关"而非真优化；任务书也含隐含引导。本轮按修正后的设计重做：**vanilla 上游 TF + KDNN 外部库 + 纯测量方法提示词**。

---

## 1. 实验设置（相对第一轮的修正）

| 维度 | 第一轮 | 本轮 |
|---|---|---|
| 基线 | 本仓 0e99f1bf4（树内含 ANNC 重写器、KDNN 集成、`enable_kdnn` 默认 true） | **vanilla 上游 TF v2.20.0**（git tag，零 KDNN/ANNC 代码；本仓历史中最早的树也已被 2/10 初始导入污染，无干净提交可用） |
| KDNN | 树内集成完毕 | **外部库**：源码 + bazel 构建描述（剔除 4 个 TF 胶水 adapter 头与 TF 耦合），独立构建验证通过，双方对等提供 |
| git 历史 | 无 | 无（git archive） |
| 提示词 | 含验证纪律、先测后优等隐含引导 | **纯测量方法**（环境资产/构建命令/A-B 口径/正确性约束/边界/交付格式），删掉全部优化思路 |
| skill 净化 | 当时通过 | 复检发现第一轮后增补含模型名（hmv）与精确实验数字——已清洗为定性表述；KPFused/ANNC 等仓内符号零残留 |
| agent 配置 | 默认 effort、主模型 | **low effort、Sonnet**（用户指定，为加速）——这是与第一轮的混杂变量，见 §7 |

公平性控制：三份 vanilla 树逐字节一致（checksum `dc3ff5868ec7`，仅含 3 处 serving 兼容补丁，逐行核验无优化内容）；共享基线二进制 md5 与两侧预热产物一致；任务书差异仅资产路径/端口/skill 段；NUMA 隔离 node0/node1；另有第三方平行实验占用 CPU 144-191 与端口 28600/28750，已通过改端口（28300/28400）与 CPU 错开规避。

## 2. 双方交付与优化点

| | skill 侧 | noskill 侧 |
|---|---|---|
| 代码改动 | **1 文件 +20 行**（`matmul_op_impl.h`：小 GEMM（m·k·n≤1.5M）单线程串行路由 + `TF_MATMUL_SERIAL_SMALL` 开关） | **319 行新头 `matmul_kdnn.h` + 14 行**（完整 KDNN GEMM 集成：BA4b prepack、指针+指纹校验缓存、TF 线程池适配、排除了并发 rehash 崩溃与缓冲区溢出两个 bug，默认关闭）+ `.bazelrc` 追加 `-O3` 全量重建 |
| 主要收益来源 | 线程配置扫描（24/48）+ 小 GEMM 串行路径 | 线程配置扫描（12/8）+ **-O3 重建**（adx +18.1%/presort +15.6%/cvr +6.6%，hmv 持平——同配置 A/B） |
| KDNN 评估 | 独立基准：单核 SVE 47 GF/s 但线程分区器几乎不并行、strided-B 疑似正确性 bug、prepack 仅 +7-20% → 负结果 | 完整集成后实测：本机 shape（M=59、400×400）prepack 后仍持平或慢于 Eigen（adx 80.7k vs 112k）→ 默认关闭 |
| 其他尝试（负结果） | OpenBLAS（更慢）、TF Serving batching（54k 弃用）、grappler AddV2-bias 融合（动态 batch Fill 形状推断失败，插桩验证后回退） | — |
| 机器上限标定 | adx 80% CPU 在 Eigen GEMM，已达 1024³ 实测峰值（656 GF/s）的 56% | — |
| token / 时长 / 工具调用 | 216,660 / 2h56m / 260 | 154,612 / 3h33m / 220 |
| 正确性 | 4 模型 × batch{59,1,8,128} 全过，max rel 1.8e-07 | 同口径全过，max rel 1.2e-07 |
| 环境备注 | 平行任务多次按进程名误杀其 server（改名二进制+重试规避） | 未受影响 |

（双侧改动规模反转耐人寻味：第一轮 skill 走"外科手术启用"路线；本轮树上没有可启用的东西，skill 侧同样收敛到最小改动，而 noskill 侧完成了本轮唯一的深度工程——319 行 KDNN 集成。）

## 3. 终态头对头（同节点 node2、48 核、交替 4 轮/方、batch 59 / 并发 4，各自最终配置）

| 模型 | vanilla 基线(16/16) | skill 终态(24/48) | noskill 终态(12/8+O3) | skill/基线 | noskill/基线 | skill/noskill |
|---|---:|---:|---:|---:|---:|---:|
| adx | 132,401 | 134,422 | 122,386 | +1.5% | **−7.6%** | **+9.8%** |
| cvr_slave | 379,099 | 380,526 | 394,586 | +0.4% | +4.1% | −3.6% |
| hmv | 272,632 | 274,761 | 287,236 | +0.8% | +5.4% | −4.3% |
| presort | 173,034 | 169,058 | 180,944 | −2.3% | +4.6% | −6.6% |
| **几何平均** | — | — | — | **+0.1%** | **+1.5%** | **−1.4%** |

p99（并发 4）：skill 在 adx 最优（2036µs）；noskill 在 cvr/hmv/presort 最优（758/1004/1590µs）。

要点：
1. **双方在正确配置的 vanilla 基线之上，头对头增益都只有 ±2% 量级**——第一轮的 +15~18% 几何平均收益完全消失。
2. **agent 的"线程调优收益"大部分不可迁移**：基线 16/16 在 48 核节点本身就接近最优；skill 的 24/48 与 noskill 的 12/8 只在各自测量环境内占优，换节点/负载后 noskill 的 12/8 在 adx 上反而 −7.6%。
3. 双方各自 A/B（各自环境内）：skill +1.2~2.2%（O vs B，B/O 各 4-6 轮）；noskill −2.3~+18.1%（-O3 效应为主，n=3）——与头对头互相印证（增益为真但小）。

## 4. 防作弊审计（事后全量 transcript 扫描）

- **skill 侧**：796 处 `loadtime-fork/tensorflow_jd_version` 引用经逐条甄别，**全部是会话 cwd 元数据字段**（agent 继承主会话目录），工具调用中零命中；对方工作区仅在 `ls` 实验根目录时见到名字。**零违规**。
- **noskill 侧**：全部工具调用扫描（含 Read/Grep/Glob/Bash 路径参数），排除合法的共享 bazel 缓存路径后**零违规**；曾发现第三方实验占用其端口并正确地未触碰。
- **代码改动真实性**：双侧 diff 与各自报告声称一致（skill 1 文件；nosckill KDNN 集成 + O3），无引入外部产物。
- **数字真实性**：双侧终态 A/B 数字由主 agent 从原始 perf_analyzer 日志独立复提，逐位吻合（noskill hmv 报告 +0.9% vs 复提 −2.3%，n=3 噪声内，均判"持平"）。

## 5. 与第一轮的对照：skill 价值的变化

| | 第一轮（脏基线：树内现成开关） | 本轮（干净基线：无任何可翻的开关） |
|---|---|---|
| skill 几何平均提升 | +17.9% | **+0.1%（头对头 vs 基线）** |
| noskill 几何平均提升 | +15.4% | +1.5% |
| skill 相对 noskill | +2.5pp（且快 13%） | **−1.4pp（且更慢、更贵：多 40% token）** |
| 收益的来源 | 翻树内开关（ANNC 融合、KDNN 路由修复） | 线程调优（不可迁移）+ -O3 + 小 GEMM 串行 |
| KDNN 路线 | 树内集成直接启用即有收益 | 双方独立评估均为负结果（分区器不并行 / shape 不适配） |

**核心结论**：第一轮测到的 skill 价值，绝大部分是**"知道这棵树上哪里藏着开关"的价值**——那是经验带宽的体现，但可被"树里没有开关"一票归零。在干净基线上、低 effort 配置下，skill 与无 skill 的差距落入噪声区（±1.5pp），方向甚至反转。

## 6. KDNN 外部库的独立发现（本轮最有工程价值的结果）

双方独立尝试集成提供的 KDNN 库，**双双失败于收益端**——而同一库在人工 fork（完整集成：线程池直通、NEON f32 generator 路由、并发首调修复）上对同样模型有 +12~40% 收益（第一轮 §2.4.2/§2.7）。三方对照说明：

1. **GEMM 库的收益高度依赖集成工程细节**（线程池适配方式、generator 选型、prepack 的命中条件与失效保护），库本体好 ≠ 接上就快。
2. 库头文件自身暴露了 prepack API（`GetDesiredWeiLayout(enablePrepack)`）——"权重预打包"这条路线对无 skill 侧同样是可发现的，两 agent 都独立走到了这一步；真正的壁垒在后面的工程深度。
3. skill 的 kernel_integration.md 参考虽有"门控/adapter/prepack/JIT"的完整模式清单，但在 low effort 下未能转化为可用的集成（skill 侧甚至没有尝试接入，只做了独立基准后放弃）。

## 7. 效度威胁

1. **effort/模型混杂（最大威胁）**：本轮为 low effort + Sonnet（用户指定加速），第一轮为默认 effort。skill 在本轮的失效不能干净归因于"干净基线"——低 effort 也可能让 agent 无力消化 6 份参考文档并完成深度集成。严格结论应为：**干净基线 × 低成本 agent 的组合下 skill 无可测价值**。
2. 头对头各 agent 沿用自己的线程配置（终态口径），线程配置节点相关（noskill 12/8 在 adx 上的 −7.6% 部分是配置 × 环境交互）。
3. 共享机器噪声：第三方平行实验（CPU 144-191）全程在跑，skill 侧 server 曾被其误杀数次；4 轮/方中位数可抗部分漂移。
4. 单次实验、无重复；n=3~6 轮/侧。
5. KDNN 库为 fork 全量源码（剔胶水），其 BUILD/头文件可能隐含集成线索（如 prepack API），这本身是"提供库"的必然代价。

## 8. 结论

1. 在**无现成答案的 vanilla TF + 外部 GEMM 库**上，本轮（low effort）两 agent 的真实优化空间收敛到线程调优与编译选项，头对头增益 ±2% 以内，**skill 与无 skill 差距落入噪声（−1.4pp 几何平均，noskill 略优）**。
2. 第一轮 skill +2.5pp 的优势本质是**经验先验对"发现树内休眠资产"的加速**——它依赖树里有资产可翻。这份价值在陌生代码库上是否仍成立，本轮（受 effort 混杂限制）给出的是否定方向的证据。
3. 本轮真正的壁垒在**深度集成工程**（KDNN 库 → 有收益的路径），双方 low effort 下都没跨过去；人工 fork 跨过去花了完整的 8 月冲刺。skill 若要有不可替代价值，其参考文档需要把"如何正确集成一个 GEMM 库"写到可执行深度，而非模式清单。
4. 方法论教训（与第一轮一致并强化）：**"X 比 Y 快 N%"的结论 = 代码库状态 × agent 配置 × 测量环境的函数**，三个变量任一变化都可能反转结论——skill 的价值声明必须限定在"与 skill 沉淀时相似的代码库形态"上。

---

## 附：产物索引

- 本报告：`${EXPERIMENT_ROOT}/clean/REPORT.md`（副本：`clean_ablation_report.md`）
- 双侧报告：`results/{skill,noskill}/REPORT.md`；原始数据：`results/*/perf/`、`logs/`、`scripts/`
- 头对头：`h2h/{results,logs,summary.json}`、`run_h2h_clean.sh`；首轮（32 核误配）数据保留于同目录（时间戳早于重跑）
- 基线与工作区：`baseline-bin/`、`pristine/tf`、`ws-{skill,noskill}/tf`、`serving-{baseline,skill,noskill}`
- KDNN 外部库：`kdnn_lib/{kdnn,README.md}`（独立构建验证：`kdnn_lib/ws_test`）
- 任务书：`brief-{skill,noskill}.md`（差异仅资产/端口/skill 段）
- Agent transcript（防作弊审计依据）：本会话 `subagents/` 目录
