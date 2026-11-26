# 迁移契约：范围、分型、处置与 patch 序列

本文件定义一次向量数据库优化迁移的输入、资产分型、处置枚举、依赖分层和交付物。版本差异审计见 `version-delta-audit.md`，门禁方法见 `equivalence-and-gates.md`。

## 目录

- [1. 输入与身份](#1-输入与身份)
- [2. 资产分型](#2-资产分型)
- [3. 处置枚举](#3-处置枚举)
- [4. 依赖分层与 patch 序列](#4-依赖分层与-patch-序列)
- [5. 追溯矩阵字段](#5-追溯矩阵字段)
- [6. 交付物布局](#6-交付物布局)
- [7. 停止条件](#7-停止条件)

## 1. 输入与身份

迁移开始前必须固定：

| 字段 | 说明 |
| --- | --- |
| `engine` | 迁移对象引擎（Faiss / DiskANN / 其他），决定 adapter 与专属 Skill 路由 |
| `source_version` / `source_commit` | 旧版本及精确 commit，含工作树是否 dirty |
| `target_version` / `target_commit` | 新版本及精确 commit |
| `source_opt_set` | 源优化集合：patch 文件、宏清单、非宏源码改动、结构使能、构建改动 |
| `target_baseline` | 目标版本未迁移基线：commit + 构建配置 + 冻结性能/recall |
| `feature_gates` | CMake option / `#ifdef` / 环境变量 / IO flag 的全集 |
| `algorithms` | 本次覆盖的算法族 |
| `paths` | `load`、`save` 构建路径中哪些在范围内 |
| `datasets` | 冻结数据集矩阵、metric、ground truth、查询参数 |
| `environment` | CPU/ISA、线程数、绑核、NUMA、编译器、`-march`、LTO、BLAS、OpenMP |
| `acceptance` | recall 门禁、性能门限、噪声地板、ABBA 轮次、per-dataset floor |

身份字段缺任意一项，不得开始正式迁移或输出收益结论。

## 2. 资产分型

每个源资产先归入以下一类，再决定处置：

| 类型 | 定义 | 迁移关注点 |
| --- | --- | --- |
| `kernel_library` | 独立 C/C++ 内核库 | 整库版本一致性、构建接入、符号可见性 |
| `non_macro_source` | 无宏、直接改源码的非宏项 | 与目标上游 diff、默认行为、kill-switch |
| `structural_enabler` | 给算法加字段/虚函数/分发/钩子的结构改动 | ABI、序列化、所有实例化点 |
| `macro_optimization` | `#ifdef <MACRO>` 门控的单点优化 | 作用点是否仍在、语义是否仍等价 |
| `build_only` | 只有 CMake option 或编译开关 | 目标版本是否还有源码作用点 |
| `dead_or_absorbed` | 目标版本无作用点，或被上游机制取代 | 删除或替换，不保留死定义 |
| `test_or_harness` | UT/ST seam、探针、dump 工具 | 是否仍能编译、是否能证明等价 |

分型时禁止只看宏名：必须打开目标 commit 确认宏包围块是否仍被编译、是否仍有调用者。

## 3. 处置枚举

| 处置 | 含义 | 允许条件 |
| --- | --- | --- |
| `KEEP` | 原样迁入 | 目标作用点存在且语义等价，门禁可复现 |
| `ADAPT` | 改写以适配目标 API/模板/类型 | 改写不改变数学语义，且有等价性证据 |
| `SPLIT` | 把一个机制拆成独立动机分别处理 | 能证明各动机独立，如资源生命周期 vs 存储密度 |
| `OPT_IN` | 默认关闭、显式开启 | 改变顺序/归约/tie-break/精度/布局，或收益依赖场景 |
| `REPLACE` | 用目标上游或新实现替代旧机制 | 替代物覆盖原动机，且差异有证据 |
| `DROP` | 不迁移 | 目标无作用点、死代码、负向且无新适用条件 |
| `DEFER` | 暂缓 | 证据不足、作用集不匹配、需新版本复测 |

处置不是永久结论。`DROP`/`DEFER` 必须写理由与新证据触发条件，避免后续 Agent 重复试错。

## 4. 依赖分层与 patch 序列

推荐四层，顺序不可随意打乱：

```text
target baseline
  └─ 01 base          内核库 + 句柄/分配器/序列化/接口字段（运行底座）
       └─ 02 structural  算法数据结构与分发使能
            └─ 03 common    跨算法共享宏
                 ├─ 04a algorithm-A 专属宏
                 └─ 04b algorithm-B 专属宏
```

patch 序列契约：

1. 每个 patch 只承载一个机制（或一个不可再分的底座整体），命名包含序号、宏名或机制名。
2. patch 必须顺序应用检查通过；全量应用后与源分支目标子树逐字节一致（如 `diff -r` 无差异），否则必须解释每一处残留差异。
3. 每个 patch 单独说明：依赖、宏、类别、文件清单、改动行数、目标作用点、回退方式。
4. `build_only` 与 `dead_or_absorbed` 单独标注，避免被误认为空补丁或被静默删除。
5. base 层可以很大（内核库整体），但不得把多个算法优化混入 base。
6. 每个 patch 应用后至少编译一次 feature-on 与 feature-off。

## 5. 追溯矩阵字段

每个机制一行，至少包含：

```text
mechanism_id | type | source_version@commit | source_file::symbol | target_version@commit
target_file::symbol | gate | disposition | equivalence_evidence | perf_evidence | verdict | notes
```

规则：

- 源码位置使用 `relative-path::symbol`，行号只作辅助并绑定 commit。
- 证据标记：`[source]`、`[experiment]`、`[inference]`、`[unverified]`。
- `perf_evidence` 必须写数据集、环境、轮次、同窗条件；缺一不可。
- verdict 只允许 `KEEP/ADAPT/SPLIT/OPT_IN/REPLACE/DROP/DEFER` + 门禁状态，不得写“应该有效”。

## 6. 交付物布局

```text
<run_dir>/
├── MIGRATION-MATRIX.md              # 追溯矩阵
├── SOURCE-TARGET-IDENTITY.json      # 版本、commit、构建、范围
├── patches/                         # 顺序 patch 与同名说明
├── G0-feature-off/                  # 等价性日志
├── G1-ut-st/                        # UT/ST 原始输出
├── G2-kernel-bitexact/              # 内核探针结果
├── G3-topk-dump/                    # 端到端 dump 与比对
├── G4-recall/                       # 每数据集 recall 对照
├── G5-perf/                         # ABBA、QPS、geomean、噪声说明
└── HANDOFF.md                       # verdict、open items、知识回写清单
```

原始日志不得只保留汇总；剔除规则、所有有效轮次和异常窗口必须保留。

## 7. 停止条件

- 身份字段或目标基线不完整；
- 目标版本作用点无法定位；
- feature-off 与上游不等价；
- patch 序列无法顺序应用或全量差异不可解释；
- A/B 配置、实际加载库或编译宏未核实；
- 任一正确性门禁失败且无位级证明。

以上任一成立时输出 blocker，不输出“迁移完成”或收益数字。
