# pgGraph 优化域（app-tuner 侧）

pgGraph 0.1.8（PostgreSQL 的 Rust/pgrx 图扩展）从**架构理解 → 编译部署 → 正确性/性能验证 →
热点归因 → 配置/源码优化**的完整调优链路。域共五个成员，按系列 PR 交付（见下表状态列），
各自独立可调用，组合起来覆盖一次完整的 pgGraph 性能优化闭环。

> `pggraph-optimize/` 本身是源码树中的领域分组（以本 DOMAIN.md 登记），不是独立 skill；
> `install.sh` 递归进入本分组后，把**当时已合入**的成员平铺安装到
> `<tool>/skills/<skill-name>/`，由 Claude Code / OpenCode 一层发现。

## 成员

| Skill | 状态 | 职责 | 典型入口 |
|-------|------|------|---------|
| [`pggraph-architecture`](pggraph-architecture/SKILL.md) | 已交付（随本 PR） | 源表/派生图模型、pgrx 后端进程、CSR/反向 CSR、ResolutionIndex、FilterIndex、`.pggraph` 工件、同步覆盖层、查询/构建/搜索/GQL/连通分量链路、GUC 与源码路径（被动只读知识库） | “pgGraph 怎么把表变成图”“为什么每 backend 一份内存”“`.pggraph` 里有什么” |
| [`pggraph-build-deployment`](pggraph-build-deployment/SKILL.md) | 待合入（系列 PR） | PostgreSQL 14–18 + Rust 1.96 + pgrx 0.19.1 编译、cargo pgrx install、本地/容器部署、PG 参数与连接模型、LDBC/Panama 数据导入与注册、建图、备份恢复、版本切换与回滚；含只读 `preflight_check.sh` | “从零编译”“装扩展”“导入 LDBC”“graph.build 卡住” |
| [`pggraph-test-verification`](pggraph-test-verification/SKILL.md) | 待合入（系列 PR） | pgrx/Rust 单测、冒烟 SQL、LDBC/Panama hot/throughput 基准、result_checksum、SQL JOIN 独立 Oracle、图不变量对账、A/B 口径与噪声判定；含临时库隔离冒烟 `smoke_test.sh`（自建并清理一次性数据库，CREATEDB 权限，非只读采集） | “优化有没有效果”“结果对不对”“压测怎么跑”“checksum 一致就够吗” |
| [`pggraph-hotspot-analysis`](pggraph-hotspot-analysis/SKILL.md) | 待合入（系列 PR） | perf/flamegraph、off-CPU、Topdown、`pg_stat_activity`、`pg_stat_statements`、`EXPLAIN ANALYZE`、per-backend 内存与加载开销；component/traverse/search/path 场景热点，客户端 GIL 与缓存争用的判别；含 dry-run `collect_profile.sh` | “慢在哪”“为什么 96 并发 CPU 只有 40%”“为什么 component_stats 慢” |
| [`pggraph-performance-optimization`](pggraph-performance-optimization/SKILL.md) | 待合入（系列 PR） | 部署/连接/缓存/GUC、csr_readonly 与 mutable_overlay、Rust release/LTO/target-cpu、UnionFind/node_deleted/component_sizes/数值排序、hydrate/String/SPI、反向 CSR 共享与 checksum 跳过等候选及负结果；含 `verify_build_opts.sh` | “怎么调”“改哪里”“收益多少”“负收益怎么回滚” |

## 域内路由

```
只想理解系统            → pggraph-architecture
要装扩展/导数据/建图    → pggraph-build-deployment
要证明对/证明快         → pggraph-test-verification
要知道慢在哪/为什么慢   → pggraph-hotspot-analysis
要把证据变成改动        → pggraph-performance-optimization
```

一次典型的优化闭环：

```
architecture（理解源表/CSR/后端进程）
  → build-deployment（编译、起 PG、导数据、build）
  → test-verification（SQL JOIN Oracle + 基线 + checksum）
  → hotspot-analysis（perf/等待/内存归因）
  → performance-optimization（单变量优化）
  → test-verification（同口径 A/B + 正确性门禁）
  → 保留或回滚
```

数据路径在此闭环中分两条且**互不兼容**：基准/导入用 LDBC v1 `CsvBasic`
（build-deployment §1，`postgres` 库）；driver 验证用 v2
`composite-merged-fk`（test-verification，另建 `ldbcsnb` 库）。

## 与插件内其他 skill 的边界

| 问题 | 去哪里 |
|------|--------|
| OS governor/THP/大页/irqbalance/ulimit | `kpbot-app-tuner` 的 `os-optimization` |
| BIOS SMT/C-State/NUMA/内存频率 | `bios-optimization` |
| 网卡队列/RSS/RPS/中断 | `network-optimization` |
| CPU 绑核/NUMA 布局/线程亲和 | `cpu-affinity-optimization` |
| 通用 C/C++/Rust 向量化、循环、汇编 | `kpbot-code-optimizer` |
| PostgreSQL 通用调参/索引/执行计划 | 先用本域 `pggraph-performance-optimization` 的 PG 段，再回到通用 DB 方法 |
| pgGraph 专属 CSR/工件/注册/同步/源码改动 | 本域 `pggraph-performance-optimization` |
| 纯编译/部署/导数据 | 本域 `pggraph-build-deployment` |
| 纯压测/正确性/A/B | 本域 `pggraph-test-verification` |

## 准入判据

新增成员须满足：

- 结论落在 **pgGraph 应用自身**（架构、部署、验证、热点、配置/源码优化），
  不是通用 PostgreSQL 调优或通用 Rust 微架构理论。
- 有明确的输入/输出契约、停止条件、失败处理与回滚方式。
- 涉及变更的内容必须区分只读采集、dry-run、低风险在线变更、需重启变更、需重编译变更。
- 有可复现的验证入口（evals 或 skill 内 `cases/`）。
- 不写入个人环境绝对路径、私网 IP、凭据；示例统一用变量占位。
- 所有性能数字标注环境、数据集身份与轮次；不同文档/不同数据集的数字不可直接横比。

## 安装与校验

- `install.sh` 依赖本 DOMAIN.md 识别领域分组，随后把已合入的成员平铺安装（含本文件的一并合入）。
- 完整性校验（结构、链接、锚点、evals、敏感信息；位于 pggraph-architecture，随系列首 PR 交付）：
  `bash pggraph-architecture/cases/skill-integrity/run.sh`（缺失的姊妹 skill 记为 pending 而非失败）
- 所有成员均提供 `evals/evals.json`；变更后应重跑完整性用例与仓库测试。

## Review

- owners: []（待指派，填写 GitCode 用户名）
- 评审范围：`Plugins/app-tuner/skills/pggraph-optimize/**`
