# 向量数据库域（app-tuner 侧）

向量数据库扩展与基准环境的部署、复现与验收：PostgreSQL + pgvector 的安装、源码构建、
扩展创建/升级、离线源码与 `deploy.env` 可追溯记录。

与 `code-optimizer` 侧 `vectordb/` 分工：那边负责向量检索的查询路径、索引与算法优化；
本侧只负责环境安装部署与可复现性，不执行 benchmark、不做数据库调优。

## 成员

| Skill | 职责 |
|---|---|
| [`pgvector-deploy`](pgvector-deploy/SKILL.md) | pgvector 环境部署：默认 master 并记录实际 commit，覆盖 PostgreSQL 安装/初始化/启动、源码构建、vector 扩展创建/升级、离线源码、远程数据库限制与超级用户校验；构建目录 root 私有 0700、路径拒绝 . 与 .. 段、数据库密码不落 argv/日志；含 evals 评测集与 tier-A 完整性用例 |

## 域内路由

- 安装/升级/回退 PostgreSQL + pgvector 环境，或准备离线源码 → `pgvector-deploy`
- 向量检索性能调优、索引/查询路径优化 → 本域不负责，转 `code-optimizer` 侧 `vectordb/`
- 实际基准测试运行、数据集下载、结果分析 → 本域不负责，部署完成后单独执行

## 准入判据

- 结论落在**向量数据库环境的部署、复现与验收**上；
- 版本与来源可追溯，敏感凭据不进入 argv/日志；
- 变更可回退：保留旧版本与备份，不自动删除用户数据；
- 提供 evals 评测集与 tier-A 完整性用例。

## Review

- owners: []（待指派，填写 GitCode 用户名）
- 评审范围：`Plugins/app-tuner/skills/vectordb/**`
