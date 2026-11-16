# 迁移清单与交付模板

复制本文件到 `run_dir` 后逐项填写，作为迁移证据的骨架。未填项不得用“应该”“大概”替代。

> 可复用模板：`templates/SOURCE-TARGET-IDENTITY.json`、`templates/MIGRATION-MATRIX.md`、`templates/HANDOFF.md`。
> 辅助脚本：`scripts/scan_version_delta.py`、`scripts/check_compile_flags.py`、`scripts/compare_topk.py`。

## 1. 身份卡

```text
engine / adapter:
source_version / commit / dirty:
target_version / commit / dirty:
target_baseline commit + build:
source_opt_set（patch/宏/非宏）:
algorithms:
paths（load / save）:
datasets + metric + ground truth:
environment（CPU/ISA/线程/绑核/NUMA/编译器/-march/LTO/BLAS/OpenMP）:
acceptance（recall 门禁/性能门限/噪声地板/ABBA 轮次/per-dataset floor；DiskANN 另含 IOs/Comps/Hops）:
```

## 2. 追溯矩阵

| mechanism_id | type | source@commit | source 位置 | target@commit | target 位置 | gate | disposition | 等价证据 | 性能证据 | verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |  |  |  |  |  |

填写规则：

- `type` 取 `kernel_library / non_macro_source / structural_enabler / macro_optimization / build_only / dead_or_absorbed / test_or_harness`。
- `disposition` 取 `KEEP / ADAPT / SPLIT / OPT_IN / REPLACE / DROP / DEFER`。
- 位置写 `path::symbol`，行号仅作辅助并绑定 commit。
- `DROP`/`DEFER` 必须在 verdict 后写理由与新证据触发条件。

## 3. Patch 序列

| 序号 | 名称 | 依赖 | 宏/开关 | 文件数 | 增/删 | 目标作用点 | 回退 | 编译 off/on |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 01 |  |  |  |  |  |  |  |  |

检查：

- [ ] 每个 patch 只承载一个机制。
- [ ] 按序 `git apply --check` 全部通过。
- [ ] 全量应用后与源分支目标子树 `diff -r` 无差异，或差异逐条解释。
- [ ] 每个 patch off/on 各编译一次。
- [ ] 两个 CMake-only / 死宏已单独标注。

## 4. 正确性清单

- [ ] G0：feature-off 与目标上游 top-k 逐字节一致。
- [ ] G0：`compile_commands.json` 中相关 TU 的 `-D` 与 `-march` 一致。
- [ ] G1：UT feature-on 通过。
- [ ] G1：UT feature-off 通过。
- [ ] G1：ST 覆盖边界、并发、fallback、save/load。
- [ ] G2：内核探针覆盖全部维度范围与 block 档，mismatch=0。
- [ ] G2：分块/归约顺序差异已定位或排除。
- [ ] G3：同索引同查询 top-k 标签逐字节一致。
- [ ] G3：基线库 @ 迁移索引 / 迁移库 @ 迁移索引 / 基线库 @ 基线索引三组交叉完成。
- [ ] G3：搜索库与构建索引的差异已分开归因。
- [ ] G4：每数据集 recall@k ≥ 冻结基线。
- [ ] G4：任何下降都已判 REJECTED 或有位级证明。
- [ ] 范围声明：`load` / `save` 覆盖情况明确。

## 5. 性能清单

- [ ] 目标基线独立冻结，未拿源版本基线代替。
- [ ] 同机、同窗口、同核集/NUMA、同索引、同参数。
- [ ] ABBA/BABA 多轮紧贴交错，记录每轮库 SHA256。
- [ ] 记录背景负载与异常窗口，保留所有有效轮次。
- [ ] 噪声地板已按 `equivalence-and-gates.md` G5 口径用同配置 A/A 自对照（A 对 A、B 对 B，采集条件与 ABBA 一致）实测标定，未用全 OFF 对全 ON 差值充当噪声。
- [ ] 报告每数据集 QPS、Recall、geomean、per-dataset floor。
- [ ] 内核段/宿主段已分开计时或隔离回放。
- [ ] 未把跨版本绝对 QPS 相减当迁移收益。
- [ ] 未跨绑核/跨场次拼接数字。
- [ ] 收益数字带 `[experiment]`/`[inference]`/`[unverified]`。

## 6. 交付模板

```markdown
# <source> → <target> 优化迁移报告

## 1. 优化摘要
- 源/目标版本与 commit、范围、门禁
- 保留/改写/opt-in/删除机制数量与一句话结论
- 收益 geomean 与每数据集最低值（带证据标记）

## 2. 迁移内容
- 按层：base / structural / common / algorithm
- 每机制：来源、目标作用点、处置、宏、文件
- 拒绝/删除项与理由

## 3. 平台属性
- 逐机制：ARM 特有 / 通用 / 混合，依据

## 4. 优化等级 L0–L5
- 逐机制判定与理由

## 5. 是否开箱即用
- 默认生效 / 需宏 / 需 opt-in / 需部署配置

## 6. 预期收益与风险
- 每数据集 QPS/Recall、geomean、噪声说明
- recall 漂移、作用集不匹配、未归属缺口、open items

## 7. 验证建议
- 待补 UT/ST、ABBA、阈值校准、范围扩展
```

## 7. 交接清单

- [ ] `MIGRATION-MATRIX.md` 已归档。
- [ ] patch 序列与同名说明已归档。
- [ ] G0–G5 原始日志与产物路径已记录。
- [ ] blocked / open items 已列出触发条件。
- [ ] 对应算法 Skill 的 experiments reference 已回写目标版本新事实。
- [ ] 目标版本基线已更新到对应引擎的 benchmark-baseline Skill（若需要）。
- [ ] 未把 provisional / uncontested 结论写成 verified。
