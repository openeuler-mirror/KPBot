---
name: vector-db-optimization-migration
description: 面向向量数据库/向量检索库任意跨版本优化迁移的通用能力：把源版本的 SIMD 内核、宏门控优化、结构使能、构建与数据布局改动，按依赖分层迁入目标版本，并完成功能等价、recall 门禁、同窗 ABBA 性能与 UT/ST 验收。用于规划 Faiss、DiskANN、Milvus 等向量引擎 X→Y 迁移、拆分迁移 patch 序列、判断机制应保留/改写/拆分/opt-in/替换/删除、定位 API/ABI/内存布局差异，或解释迁移后收益不达预期、复核旧版本已 accepted 的优化在新版本是否仍成立；不负责单个算法的新优化候选设计（走对应引擎/算法 Skill）、不负责冻结基线建立（走对应引擎 benchmark Skill）、不负责源码调用链普查（走对应引擎 source-analysis Skill）、不负责测试框架构建运行（走对应引擎测试 Skill）。
---

# 向量数据库优化迁移

跨版本迁移的目标不是“把旧代码贴过去”，而是把源版本已验证的优化机制在目标版本上重新证明：**机制仍然成立、路径仍然等价、收益仍然可归因**。本 Skill 对引擎保持中立，面向任意向量数据库/向量检索库的版本对与任意算法族，提供通用的迁移分型、依赖排序、等价性门禁和性能验收方法；引擎专属细节按 `references/engine-routing.md` 按需加载。

> 版本隔离：迁移结论必须绑定 `引擎 + 源版本 commit + 目标版本 commit + 构建配置 + 算法/数据集/路径范围`。跨版本、跨 commit 的绝对 QPS 不可直接比较；所有源码位置以对应引擎的 source-analysis Skill 在当前 commit 重新打开的结果为准，不沿用历史报告的行号。
>
> 引擎隔离：不同引擎的索引结构、序列化、构建系统与测试框架不同，门禁方法和 patch 分层可复用，但作用点、ABI 风险位置与命令不可跨引擎套用。

## 触发场景
- 把任意旧版本向量引擎的优化迁入新版本（相邻大版本、LTS 升级、分支回主线等），引擎包括但不限于 Faiss、DiskANN、Milvus、Qdrant、Weaviate、pgvector。
- 为一次迁移拆分可顺序应用、可逐条回退的 patch 序列。
- 判断某个旧宏/旧机制在新版本应 `KEEP / ADAPT / SPLIT / OPT_IN / REPLACE / DROP / DEFER`。
- 分析迁移后收益低于预期、recall 漂移、ABI 崩溃、性能回归或“优化已移植但没效果”。
- 为新版本复核旧版本已 accepted 的宏或内核是否仍然成立。

不负责：新优化候选的算法设计与热点假设（走对应引擎/算法的 Optimization Skill）；冻结基线建立与结果对比口径（走对应引擎的 benchmark-baseline Skill）；源码调用链普查（走对应引擎的 source-analysis Skill）；测试框架构建运行（走对应引擎的测试 Skill）。

## 引擎路由

先识别迁移对象所属引擎，再加载对应 adapter；未命中时只使用本文件的通用流程，并把引擎差异登记为 open item，不得套用其他引擎的作用点或命令。

| 引擎 | Adapter | 优先路由的专属能力 | 迁移备注 |
| --- | --- | --- | --- |
| Faiss | `references/engines/faiss.md` | `faiss-source-analysis`、`faiss-benchmark-baseline`、`faiss-sra-test`、`faiss-perf-profile`、对应算法 Optimization Skill | 识别到 `faiss/`、`libfaiss`、`Index*`、`IndexIVF*`、`IndexHNSW` 等即按 Faiss 路由 |
| DiskANN | `references/engines/diskann.md` | `diskann-source-analysis`、`diskann-benchmark-baseline`、`diskann-perf-profile`、`diskann-*-optimization` | 识别到 `diskann/`、`libdiskann`、`Vamana`、`GraphHeader`、`sector`、`io_uring` 等即按 DiskANN 路由 |
| Milvus / 其他 | 暂无 adapter | 通用 source-analysis / benchmark / 测试能力；必要时新建 adapter | 不得直接复用 Faiss/DiskANN 的类名、门禁命令与构建假设 |

Faiss 专属 Skill 未安装时，本 Skill 仍可给出通用迁移计划，但必须把“缺少源码定位/冻结基线/测试框架”列为 blocker 或 `DEFER`，不得凭记忆补行号或收益。

## 资源路由

- 迁移范围、资产分型、处置枚举、依赖分层、patch 序列契约与产物 → `references/migration-contract.md`
- 引擎识别与专属能力路由 → `references/engine-routing.md`
- 版本差异审计清单（构建/宏、ABI、API、数据结构、分配器、序列化等） → `references/version-delta-audit.md`
- 等价性门禁 G0–G5、位级探针、top-k dump、recall 零下降、同窗 ABBA 与噪声地板 → `references/equivalence-and-gates.md`
- 迁移清单、patch 分册、验收与交付模板 → `references/migration-checklist.md`
- 30 分钟端到端最小迁移示例 → `references/quickstart.md`
- 已去敏的迁移案例与处置映射 → `references/known-cases.md`
- 失败模式、假阳性与“症状 → 首选排查” → `references/failure-modes.md`
- 公共头/类布局/虚函数/序列化的 API/ABI 分诊 → `references/api-abi-triage.md`
- 方案性输出契约 → `references/optimization-output-format.md`

## 工具与模板

模板位于 `templates/`，脚本位于 `scripts/`。脚本只做取证与辅助判定，不能替代 G0–G5 的人工结论；脚本中的引擎路径、库名与参数由 adapter 提供。

- 身份卡模板 → `templates/SOURCE-TARGET-IDENTITY.json`
- 追溯矩阵模板 → `templates/MIGRATION-MATRIX.md`
- 交接单模板 → `templates/HANDOFF.md`
- G2 内核位级探针骨架（dlopen 双库）→ `templates/kernel_probe.cpp`
- 版本差异扫描（CMake option / 宏 / 公共头；适用于类 CMake 构建的引擎）→ `scripts/scan_version_delta.py --source <src> --target <dst> --json <out>`
- 编译宏与 flags 一致性（按参数顺序解析 `-D`/`-U`，识别 `-march=val` 与 `-march val` 两种形态；uniform flag 缺失也算分裂）→ `scripts/check_compile_flags.py --compile-commands <json> --path-filter <相关 TU 子串> --require-macro NAME=VALUE --require-flag=-march=...`（默认缺失或分裂即退出 1）
- top-k dump 等价性对比（G0/G3）→ `scripts/compare_topk.py --baseline <dump> --candidate <dump> --k 10`（默认严格：查询集、条数、顺序、距离全等；`--allow-order-change` 仅供 OPT_IN 机制复核；输出 `overlap@k` 是与基线的重合率，G4 recall 必须用冻结 ground truth 分别计算，不得用该值替代）

## 迁移流程

1. **固定身份与范围**：记录引擎、源/目标版本 commit、工作树干净状态、源优化集合（patch/宏清单）、目标基线构建、算法与数据集、`load/save`（或引擎等价路径）、验收门禁。目标版本必须单独冻结一份“未迁移”基线，不得拿源版本基线充当目标基线。
2. **识别引擎并加载 adapter**：按 `references/engine-routing.md` 判定引擎与可用专属 Skill；adapter 决定源码定位方式、基线口径、测试入口、库名与高风险作用点。引擎无法判定时停止并请求确认，不得猜用其他引擎的类名或命令。
3. **建立迁移追溯矩阵**：逐项列出 `机制 → 源文件/符号/宏 → 目标版本是否存在/API 是否变化 → 处置 → 证据`。处置枚举与必填字段见 `references/migration-contract.md`；差异审计先查 `references/version-delta-audit.md`，再用对应引擎的 source-analysis Skill 在当前 commit 复核。
4. **分层排序**：按 `底座（内核库/句柄/分配器）→ 结构使能（算法数据结构与分发）→ 跨算法共享宏 → 算法专属宏` 排序；每个机制一个 patch，保留原始 `#ifdef` 语义，保证 feature-off 时与目标上游逐位一致。
5. **移植与改写**：先解决构建/ABI/API 差异（CMake public defines、`-march`、新增成员与虚函数全局一致），再移植热路径。风险机制默认 `OPT_IN`（量化、重排、布局改变等），不得默认打开。
6. **正确性门禁**：依次执行 G0 feature-off 等价、G1 UT/ST、G2 内核位级探针、G3 端到端 top-k dump、G4 recall 零下降。任一失败即停止，不得进入性能矩阵。方法见 `references/equivalence-and-gates.md`，引擎入口见 adapter。
7. **性能门禁**：同机、同窗口、同核集/NUMA、同索引做交错 ABBA；报告每数据集 QPS、geomean、per-dataset floor 与噪声地板；把“迁移收益”和“目标版本更重底座导致的差异”分开归因。禁止跨版本绝对值、跨绑核或跨场次直接比较。
8. **裁决与交接**：按 `KEEP / ADAPT / SPLIT / OPT_IN / REPLACE / DROP / DEFER` 给出每机制 verdict；归档 patch、原始日志、矩阵与 open items；把目标版本的新事实写回对应引擎/算法 Skill 的 experiments reference，并注明 `[experiment]`/`[inference]`/`[unverified]` 与适用范围。

## 迁移决策速查

| 目标版本情况 | 推荐处置 | 通用例子 |
| --- | --- | --- |
| 源码作用点存在、语义等价 | `KEEP` | 可直接复用的距离/查表/SIMD 内核 |
| 有作用点但 API/类型/模板变化 | `ADAPT` | 抽象类层次、模板分发、接口签名变化 |
| 一个旧机制混合了多个动机 | `SPLIT` | 池化被上游取代但存储密度仍需保留 |
| 改变顺序/归约/tie-break，收益需保留 | `OPT_IN` | 顶点重排、量化存储、分块归约 |
| 机制被上游等价实现取代 | `REPLACE` | 用上游生命周期/回收机制替代旧池化 |
| 编译开关存在但无源码作用点 | `DROP` | 目标版本无作用点的死定义 |
| 新版本证据不足或作用集不匹配 | `DEFER` | 只在特定 metric/维度成立的宏，先关闭待复测 |

## 硬约束

- **目标基线独立冻结**：迁移收益相对目标版本未迁移基线；源版本基线与目标版本基线不可互换，跨版本绝对 QPS 不得写入收益结论。
- **一次一个机制**：一个 patch/候选只承载一个可归因机制；组合收益另开实验。不得把“整包移植 + 多个宏 + 参数调整”合成一个不可归因的 delta。
- **源版本 accepted ≠ 目标版本 accepted**：旧版本 accepted 的宏/内核必须在目标版本重新做单变量门禁；新版本可能把单开中性或负向的机制暴露出来。
- **recall 零下降**：任一数据集 recall@k 低于冻结基线（含 1e-6 级微差）即不合格，除非给出位级一致证明或明确的 opt-in 适用条件并通过门禁。不得用 benchmark recall 不变替代 UT/ST。
- **feature-off 逐位一致**：所有迁移机制必须保留关闭路径；关闭后与目标上游在相同构建下行为逐位一致，作为回归对照。
- **ABI 全局一致**：给公共索引类增加数据成员或给公共虚接口增加虚函数时，所有链接进目标库的翻译单元必须看到同一组宏与 `-march`；通过 CMake public defines 全项目传播，不得逐文件局部开宏。
- **门禁前先证伪配置**：确认 A/B 两臂的 config/index 路径、实际加载的动态库、编译宏、`-march` 与 `-flto` 状态。历史案例中 A/B 臂 config 误指向基线索引，导致整轮结论作废。
- **LTO 位码不可当证据**：带 `-flto` 时 `.o` 为位码，静态反汇编找不到调用点不代表未生效；用运行时探针、`compile_commands.json`、链接后库反汇编或 sampled symbol 证明。
- **不凭源码声明热点**：迁移后热核必须用对应引擎的 perf-profile Skill 采样确认；旧版本的热点占比不得直接套到新版本。
- **显式声明路径范围**：`load` 与 `save`（或引擎等价路径）可能行为不同；只验证 `load` 时不得对 `save` 做等价承诺。
- **引擎事实优先**：类名、文件路径、序列化格式、测试入口必须来自当前引擎源码或 adapter；不得把其他引擎的经验写成当前引擎事实。

## 输出契约

面向用户的迁移结果至少包含：

1. `引擎 + 源/目标版本 + commit + 构建身份 + 范围（算法/load-save/数据集）`；
2. 追溯矩阵（机制、源位置、目标位置、处置、宏、证据）；
3. 顺序 patch 序列（每个 patch 的依赖、宏、作用点、文件清单）；
4. G0–G4 正确性结果与原始产物路径；
5. G5 性能表（每数据集 QPS/Recall/geomean/per-dataset floor/噪声说明）；
6. 每机制 verdict 与 open items；
7. 对下游引擎/算法 Skill 的知识回写清单。

方案性输出沿用 `references/optimization-output-format.md` 的 7 段契约（优化摘要 / 优化内容 / 平台属性 / L0–L5 等级 / 是否开箱即用 / 预期收益与风险 / 验证建议），收益数字必须带证据标记。

## 停止条件

出现以下任一情况立即停止并报告 blocker，不生成“迁移完成”结论：

- 引擎、目标 commit 或源优化集合无法固定；
- 引擎未识别且无法获得对应 adapter，导致源码定位、基线或测试入口不可信；
- feature-off 与目标上游不等价；
- UT/ST 失败、ABI 崩溃或位级探针 mismatch；
- recall 零下降门禁失败且无位级证明；
- A/B 配置指向未核实，或性能采集跨窗口/跨核集无法配对；
- patch 序列不能按序应用，或全量应用后与源分支目标差异无法解释。
