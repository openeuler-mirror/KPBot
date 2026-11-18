# 搜推（SRA）域

搜索 / 推荐 / 广告链路的业务层性能优化技能。

本域建议把 `skills/` 顶层平铺的 skill 按领域归并、并在域目录放一份 `DOMAIN.md` 登记成员与 reviewer。
约定见 `docs/STANDARDS.md` 的「领域分组」：含 `SKILL.md` 的目录是 skill，含 `DOMAIN.md` 的目录是领域分组（可再嵌一层子域），
安装时一律展开为 `<tool>/skills/<skill>/`，Claude Code 与 OpenCode 侧的可见性与平铺时完全一致。

## 成员

| Skill | 职责 |
|---|---|
| [`sra-dag-tuning`](sra-dag-tuning/SKILL.md) | 搜推 DAG 全栈优化方法总表：业务/DAG（1.1–1.24）→ 基础库 → 编译器 → OS → 芯片/BIOS → 通用，共 6 层 60 条方法，每条按 使用场景 / 使能方法 / 预期收益 组织；自带 `cases/recall-topk` 评测容器 |

后续计划（尚未落地，另行 PR）：向量检索层（Faiss / DiskANN / VSAG / 通用 CP0–CP7）独立成 `skills/vectordb/`，
embedding 侧 skill（`parallel-unique-pytorch`、`kunpeng-llama-cpp-tuning`）归入本域。

## 成员登记纪律

- **新增成员只在本文件登记，不要改 `skills/kpbot-code-optimizer/SKILL.md` 的全局路由表。**
  全局路由表是跨域共享行，每个新 skill 都往里加一行会让并行 PR 必然冲突；域内登记后，冲突范围收敛到本域。
- 目录约定：`skills/<域>/<skill>/SKILL.md`，子域再深一层 `skills/<域>/<子域>/<skill>/SKILL.md`。

## Review

- owners: []（待指派，填写 GitCode 用户名）
- 评审范围：`Plugins/code-optimizer/skills/sra/**`
- 判定口径：路径即归属——改动落在本域目录内由本域 reviewer 评审；跨域改动（`install.sh`、`docs/STANDARDS.md`、`kpbot-code-optimizer` 路由入口）另行指定。
