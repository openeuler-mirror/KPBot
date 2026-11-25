---
name: training-inference-host-insights
description: Research incremental training/inference community developments and DeepSeek, GLM, and Kimi Agent ecosystems; turn primary-source evidence into prioritized Host/CPU, memory, cache, storage, and network opportunities with Chinese Markdown and offline HTML reports. Use for recurring community insight reports or scoped Host opportunity research, not ordinary model Q&A or live server tuning.
---

# 训推社区Host洞察skill

英文名：**Training & Inference Host Insights**。调用名：`training-inference-host-insights`。

把社区增量转成可执行的 Host 侧技术判断：**发生了什么 → 实现机制 → 工作负载与部署条件 → 可测价值 → 风险/成熟度 → 最小实验**。中文输出；Markdown 是唯一事实源，HTML 是派生阅读视图。

## 范围与输入

默认续接既有“推理社区洞察归档”任务，覆盖六个社区及三家模型生态。具体清单见 [研究范围](references/research-scope.md)，完整周期研究前读取；用户明确缩小范围时按其要求执行并列出未覆盖项。

- 核心社区：vLLM、SGLang、vLLM Ascend、NVIDIA TensorRT-LLM、Mooncake、3FS。
- 模型生态：DeepSeek、GLM（智谱/Z.ai）、Kimi（Moonshot/月之暗面），各自检查模型发布、Harness、记忆、沙箱、Agent 配套。
- Host：CPU、内存/NUMA、调度、协议与预处理、KV 元数据与数据搬运、缓存/存储、通信与网络、异构重叠、可观测性。
- “训推”包含与上述链路直接相关的训练数据供给、checkpoint、RL rollout、权重更新与训练/推理闭环；名称不意味着自动扩大为所有训练框架新闻。仅在有本期一手证据时纳入这些方向。

优先从当前任务、Vault 指令、上期报告及运行记忆推导 Vault、时间窗和交付位置；缺少可推导信息才询问。默认时区 `Asia/Shanghai`。不要把本技能安装日期、某次历史版本或自动化 ID 固化为所有用户的水位。

## 周期连续性

读取 [归档与连续性](references/archive-contract.md)，用于正式周期报告、自动化运行和历史报告增补。

1. 先读运行记忆和最近成功报告，区分调度执行时间、报告生成时间、内容截止时间。正式起点是**上次成功报告的生成时间**，终点为本次研究启动时刻；无历史则最近 7 天。
2. 补上期截止至生成时刻的制作尾窗，单列补漏并按 SHA/tag/事件 ID 去重；不把扩展范围或配置修改当新报告水位。
3. 新范围必要背景标“窗口外背景”。日期精度不足、公告修改或网页缓存冲突时保留不确定性，不捏造时分和首次发布日。
4. 专题补充不推进正式水位；发现后续日期报告时先核是否完整成功，不能仅采用文件 mtime。

## 采集与核验

依次建立问题/证据矩阵、采集一手证据、核重点变化，然后综合判断。不要先写结论再找链接。

- 每个核心社区检查官方仓库的 release、commit、重要 PR、issue/discussion 和文档。REST、官方网页或可用连接器均可；核到官方入口，记录实际查询、分页边界、时间与失败。
- 优先核 `published_at`、`merged_at`、默认分支/最终 SHA、release 是否包含该改动。commit authored time、merge time 和 release time 不能互相替代。
- release 是本期事件时可以概述累计交付，但不要把其中旧 PR 重计为本期合入。拒绝/关闭未合入的提案、回退与重新合入须说明最终状态。
- 对会改变推荐的实现细节，核最终代码/配置/测试或明确降级为作者陈述。机器人 PR 摘要、正文原型及旧 review 不替代最终 diff。
- issue/discussion 里的故障是用户报告，明确版本、作者、是否复现/官方确认；不直接宣称全版本缺陷或已解决。
- 厂商贡献至少核 NVIDIA、Intel、AMD，列作者、组织证据、状态、影响和直接链接。用户名、硬件标签、仓库归属不证明雇主；证据不足写“单位待验证”。
- 记录每个关键事实的紧邻链接，并在末尾按项目汇总。来源必须是实际检查过的页面，不能把计划检查写成已检索。
- 用事实/经验/假设/待验证标注判断；另标官方/第三方已验证适配/第三方自述/待验证关系。第三方代码可定位但未运行时不要声称已实测兼容。
- 无可靠新增时明确写“本周期未发现经一手来源证实的新增动态/新增支持”，同时列已查来源、覆盖程度与缺口；访问失败不能当作“0 更新”。

证据台账至少保留：项目、事件 ID/SHA/tag、时间与精度、状态、变化、关系/组织证据、直接 URL、核验层次、限制。提交数量说明是否含 merge 历史，不当作功能数量或贡献排行榜。

## Host 洞察与方案

只提出有证据链的机会，不强凑条数。把已合入优化作为基线，可新做方向应是缺失能力、验收、跨层瓶颈或明确未解决问题。

每项包含：

- 优先级与判断类型、相关事件和机制、目标工作负载、CPU/设备/软件条件。
- 可测价值、实现复杂度、依赖、正确性/容量/性能风险、限制和最小实验。
- 验收指标与失败/回退条件；未知收益写待测，不发明百分比。

区分容量可运行性、加载/初始化速度、kernel 微基准、服务延迟/吞吐与 Agent 任务完成时间。性能数字须保留模型、版本、精度、硬件、输入/输出长度、并发、缓存状态、指标单位和测试次数；缺项标未披露。不做异口径横向排名，不把单次结果、尚未包含最终 patch 的测试或宣传倍数推广为通用收益。

至少给一个相关端到端场景：请求/数据表示 → CPU 调度与元数据 → Host/设备移动和所有权 → 输出/持久化 → 正确性检查 → 失败恢复。共享内存尤其核 NUMA 物理放置、注册/映射、owner 和消费者生命周期；RSS 总和不等于唯一物理内存。

## 交付与验收

读取 [报告结构](references/report-contract.md)。默认生成中文 Markdown 正文、同源单文件 HTML、带 YAML 的证据台账，更新已有索引与双链。用户另指定格式/目的地时遵从。

- 无实质增量仍生成简短报告，保留六社区、三家×五类的检查结果和缺口。
- HTML 自包含 CSS、无外部 JavaScript，桌面/手机、浅/深色均可读；目录可用、宽表内部滚动，外链有 `target="_blank" rel="noopener noreferrer"`。
- 检查 YAML、MD/HTML 事实一致、时间、链接/锚点、旧文件未覆盖；实际渲染桌面与窄屏并查看正文表格。无法渲染时如实说明。
- 完成后先验证产物和索引，再更新成功运行水位与下期来源/未决清单。网络、权限或证据不足按实际范围记录，禁止把部分检索伪装全量。
- 最终回复提供绝对路径、覆盖窗、主要来源、3–5项发现、最高优先级 Host 机会、存储与三家模型生态变化及缺口。

调用本技能不自动创建/修改计划任务，不自动 commit/push、发消息、发布、运行收费 API 或实机性能实验；这些按当前用户授权执行。本技能不依赖某个特定连接器或其他技能才能工作。
