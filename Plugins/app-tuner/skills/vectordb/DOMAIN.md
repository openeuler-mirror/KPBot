# 向量数据库基准域（app-tuner 侧）

VectorDBBench 等向量数据库基准环境的部署与复现：Python 3.11+ venv、源码固定版本安装、
数据库客户端 extras、离线 wheelhouse 与私有 pip 索引、CLI/current 链接和 `deploy.env` 验收。

与 `code-optimizer` 侧 `vectordb/` 分工：那边负责向量检索的查询路径、索引与算法优化；
本侧只负责基准环境的安装部署与可复现性，不执行 benchmark、不做数据库调优。

## 成员

| Skill | 职责 |
|---|---|
| [`vectordbbench-deploy`](vectordbbench-deploy/SKILL.md) | VectorDBBench 环境部署：默认固定版本 v2.0.0 并校验 commit，Python 3.11+ venv、源码 pip 安装、数据库客户端 extras、离线 wheelhouse、私有 pip 索引文件、CLI/current 链接与 `deploy.env`；含 evals 评测集与 tier-A 完整性用例 |

## 域内路由

- 安装/升级/回退 VectorDBBench 环境，或准备离线 wheelhouse、私有 pip 索引 → `vectordbbench-deploy`
- 实际 benchmark 运行、数据集下载、结果分析 → 本域不负责，部署完成后单独执行
- 向量检索查询路径/索引/算法优化 → `code-optimizer` 侧 `vectordb/`

## 准入判据

- 结论落在**向量数据库基准环境的部署、复现与验收**上；
- 固定版本与完整性校验可验证，凭据不进入 argv/日志；
- 变更可回退：保留旧 venv/备份链接，不自动删除用户数据；
- 提供 evals 评测集与 tier-A 完整性用例。

## Review

- owners: []（待指派，填写 GitCode 用户名）
- 评审范围：`Plugins/app-tuner/skills/vectordb/**`
