# Faiss 性能优化子域

Faiss 查询路径性能优化的成研技能部分：编排与契约、算法与候选、SIMD 选型方法。本子域隶属 `skills/vectordb/` 域；深研维护的方法（8 个）与执行/特征（2 个）技能将随后续阶段并入。

## 成员

### 编排与契约（6）

| Skill | 职责 | 来源 |
| --- | --- | --- |
| [`faiss-optimize`](faiss-optimize/SKILL.md) | CP0–CP7 编排、五角色交接与实验状态管理 | 成研 |
| [`faiss-source-analysis`](faiss-source-analysis/SKILL.md) | 调用链、数据布局与内核分派取证 | 成研 |
| [`faiss-build`](faiss-build/SKILL.md) | 可复现构建、开关构建与产物身份 | 成研 |
| [`faiss-benchmark-baseline`](faiss-benchmark-baseline/SKILL.md) | 实验契约、基线冻结、漂移与复用 | 成研 |
| [`faiss-perf-profile`](faiss-perf-profile/SKILL.md) | 采样命令、工作负载矩阵与热点归因纪律 | 成研 |
| [`faiss-sra-test`](faiss-sra-test/SKILL.md) | sra_test 测试适配 | 成研 |

### 算法与候选（6）

| Skill | 职责 | 来源 |
| --- | --- | --- |
| [`faiss-algorithm-optimization`](faiss-algorithm-optimization/SKILL.md) | 算法路由、候选方向与适用条件 | 成研 |
| [`faiss-ivfpq-optimization`](faiss-ivfpq-optimization/SKILL.md) | IVFPQ 倒排扫描、PQ 查表、候选与精排 | 成研 |
| [`faiss-ivfpqfs-optimization`](faiss-ivfpqfs-optimization/SKILL.md) | FastScan 打包码字、量化 LUT、块扫描 | 成研 |
| [`faiss-hnsw-optimization`](faiss-hnsw-optimization/SKILL.md) | HNSW 图遍历、visited、候选容器与构建路径 | 成研 |
| [`faiss-rabitq-optimization`](faiss-rabitq-optimization/SKILL.md) | RaBitQ 位平面、解码归约、查询预处理与缓存 | 成研 |
| [`faiss-turboquant-optimization`](faiss-turboquant-optimization/SKILL.md) | TurboQuant MSE 查表、QJL 校正、预筛与精排 | 成研 |

### 方法（1）

| Skill | 职责 | 来源 |
| --- | --- | --- |
| [`faiss-simd-optimization`](faiss-simd-optimization/SKILL.md) | NEON/SVE 选型决策、实际启用证据、ARM 源码导航 | 成研 |

### 待并入（深研，10）

方法 8 个（`faiss-optimization-selection`、`faiss-simd-layer-port`、`faiss-pq-accumulate-kernel`、`faiss-pq-group-scan`、`faiss-lut-layout`、`faiss-scan-loop-overhead`、`faiss-memory-latency`、`faiss-early-exit`）与执行/特征 2 个（`faiss-benchmark-run`、`faiss-feature-capture`）将随后续阶段并入本子域。

## 路由

| 用户问题 | 技能 |
| --- | --- |
| 完整优化流程编排 | `faiss-optimize` |
| 算法选型与候选方向 | `faiss-algorithm-optimization` |
| 源码调查与调用链 | `faiss-source-analysis` |
| 构建、开关构建与产物身份 | `faiss-build` |
| 冻结基线与实验契约 | `faiss-benchmark-baseline` |
| 采样命令与热点归因 | `faiss-perf-profile` |
| sra_test 接入 | `faiss-sra-test` |
| 算法调用链、参数与热点 | 对应 `faiss-ivfpq / ivfpqfs / hnsw / rabitq / turboquant-optimization` |
| NEON/SVE 选型与启用证据 | `faiss-simd-optimization` |

## 登记纪律

- 新增成员只在本文件登记，不改插件 README 技能总表与 `kpbot-code-optimizer` 的全局路由表——共享行会让并行 PR 必然冲突。
- 子域内技能之间、对域外技能一律按技能名引用，不引用内部文件路径。
- 成员 skill 名保持全仓唯一（安装时按 basename 平铺）。

## Review

- owners: []（待指派，填写 GitCode 用户名）
- 评审范围：`Plugins/code-optimizer/skills/vectordb/faiss/**`
