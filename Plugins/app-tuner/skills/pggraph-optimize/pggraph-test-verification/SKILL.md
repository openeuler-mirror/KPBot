---
name: pggraph-test-verification
description: >
  pgGraph 0.1.8 的正确性与性能验证：pgrx `cargo pgrx test`/Rust 单测/Heavy 测试、最小冒烟 SQL、
  LDBC SNB SF1 与 Panama 的 cold/warmup/hot 基准、多连接 throughput、官方 LDBC Interactive v2
  Java driver（实现/驱动补丁、14 复杂读/7 短读/更新流、Schedule Audit）、QPS/延迟/TOTAL 口径、
  result_checksum 的真实含义与局限、SQL JOIN 独立 Oracle、图不变量对账、最短路径对照、
  A/B 同口径复测与噪声判定、结果归档。用于回答“功能有没有坏、性能是否真的变好、数字是否可信”；
  不适用编译部署（→ pggraph-build-deployment）、热点归因（→ pggraph-hotspot-analysis）、
  参数/源码优化实施（→ pggraph-performance-optimization）。触发词：pgGraph 测试、pgGraph 验证、
  pggraph_verify（站点自建包装器，仓库不提供——见 benchmark-harness 的契约说明）、
  LDBC benchmark、Panama、hot benchmark、throughput、QPS、result_checksum、
  正确性验证、SQL JOIN 对账、A/B 对比。
license: MulanPSL-2.0
compatibility: pgGraph 0.1.8；PostgreSQL 18.3、Rust 1.96.0、pgrx 0.19.1、aarch64/鲲鹏实测；PG 14–18 方法一致
metadata:
  skill_type: tool_assisted
  category: testing
  domain: graph_database
  version: "1.0.0"
---

# pgGraph 正确性与性能验证

## 简介

验证分两层，缺一不可：

1. **正确性**：扩展可用、单测/冒烟通过、查询结果与独立 Oracle 一致、数据完整。
2. **性能**：固定口径下测 QPS/延迟/资源，做基线 vs 候选的 A/B，判断收益是否超出噪声。

原则：**没有正确性的性能数字无效；没有同口径基线的“提升”不可信；checksum 一致只证明
“两次结果相同”，不证明“结果正确”。**

## 何时触发

- 编译/部署后确认服务可用、数据完整、查询语义正确。
- 改源码、调参数、换二进制、升级依赖后判断收益/回退。
- 建立或复现 LDBC/Panama 基线，对比部署拓扑/连接数/编译选项。
- 压测数字异常（QPS 上不去、CPU 不饱和、成功率下降、延迟长尾）——**只判数字
  是否可采信**（口径一致性/预热/轮数/噪声阈值），不做瓶颈归因（归因定位 →
  `pggraph-hotspot-analysis`）。
- 需要可归档的验证报告（功能证据 + 性能证据 + 口径 + 结论 + 限制）。

**不适用**：安装/编译/导数据 → `pggraph-build-deployment`；火焰图/热点归因 →
`pggraph-hotspot-analysis`；具体优化杠杆实施 → `pggraph-performance-optimization`。

> 交接说明：上述姊妹 skill 与本 skill 同属 pggraph-optimize 领域分组，由同一系列 PR 交付
> （源码位于 `Plugins/app-tuner/skills/pggraph-optimize/`，`install.sh` 会把五个成员平铺
> 安装到 `<tool>/skills/<name>/`，安装后入口即可用——该安装行为随系列第 1 个 PR 交付的
> `pggraph-optimize/DOMAIN.md`（install.sh 依赖它识别分组）合入后生效；此前 install.sh
> 对缺 DOMAIN.md 的领域分组按设计整组跳过，已合入成员暂不安装，属预期窗口期）。若当前环境找不到对应入口
> （未安装/未合入），把基准报告、checksum/对账结果连同待办交回维护者，由其安装对应
> skill 或人工继续。

## 验证矩阵

| 层级 | 手段 | 依赖 | 能回答什么 |
|------|------|------|-----------|
| 构建 | `graph.so`/控制文件/SQL、SHA256、`pg_extension` | 编译产物 | 装的东西是否可信 |
| 单测 | `cargo pgrx test pg18` | PG 测试实例 | pgrx SQL 接口/模块是否回归 |
| Rust 单测 | `cargo test --no-default-features --features pg18` | Rust 工具链 | 纯 Rust 逻辑是否回归 |
| Heavy | `tests/heavy/run_release_gate.sh` 等 | 完整环境 | 崩溃恢复/并发/备份/ACL 等 |
| 冒烟 | 最小表 + 注册 + build + traverse/search | 运行实例 | 端到端是否可用 |
| 独立 Oracle | SQL JOIN / 递归 CTE / 不变量 | 源表 | 图引擎结果是否正确 |
| 微基准 | hot benchmark（单连接） | 图已建 | 单查询延迟与稳定性 |
| 系统压测 | throughput（多连接，`--mp`） | 图已建 + 客户端 | QPS/扩展性/排队 |
| 判定 | A/B 同口径、多轮、噪声阈值 | 基线 + 候选 | 收益是否真实、是否保留 |

## 工作流

```
① 构建/扩展验证 → ② 冒烟 → ③ 独立 Oracle 对账
  → ④ hot 基线（多轮）→ ⑤ throughput 容量扫描
  → ⑥ 候选复测（同口径）→ ⑦ 判定与归档
```

## 参考索引

| 文件 | 何时读 |
|------|--------|
| `references/functional-verification.md` | 单测/冒烟、SQL JOIN Oracle、不变量、checksum 局限、最短路径对照 |
| `references/benchmark-harness.md` | hot/throughput 机制、LDBC/Panama 查询、参数、结果字段、客户端 GIL、容量扫描 |
| `references/ldbc-driver-verification.md` | 官方 LDBC SNB Interactive v2 driver 的补丁、构建、参数、加载、运行与审计指标 |
| `references/ab-protocol.md` | A/B 口径、噪声、多轮、绑核、报告模板、保留/回滚判据 |
| `scripts/smoke_test.sh` | 隔离的最小冒烟（临时数据库，完成后删除） |

## 最小冒烟

```bash
PGSQL_PREFIX=/opt/pgsql/18.3 PGHOST=localhost PGPORT=6666 PGUSER=postgres \
  bash scripts/smoke_test.sh
```

脚本在临时数据库中建表/注册/build/traverse/search，最后按 CREATED 门控只清理本次创建的库；不会修改既有数据。

## 关键纪律

1. **先固定身份**：pgGraph 版本/commit、`.so` SHA256、PG/pgrx/Rust、编译参数、数据集、
   图工件、当前图、连接数、绑核。
2. **先 Oracle 后计时**：正确性不通过，不看性能。
3. **单变量**：参数、编译选项、源码补丁分开验证；不要一次改多个。
4. **同口径**：同一数据集/规模、同一 build 模式、同一 PG 配置、同一查询 SQL/limit/排序、
   同一连接模式、同一预热。
5. **多轮**：至少 3 轮；报告 median 与离散度，不只报最好一轮。
6. **收益 <5% 视为噪声**；负收益必须回滚并记录。
7. **不用 throughput 验证正确性**：它不生成 checksum，只记录耗时/是否报错。
8. **checksum 不是 Oracle**：原始实现可能有 bug；必须用 SQL JOIN/不变量/已知答案交叉验证。

## 输出契约

验证报告至少包含：

1. **结论**：正确/错误、收益/回退/噪声、是否保留。
2. **身份**：版本、`.so` SHA256、编译参数、数据集与规模、build 输出。
3. **正确性证据**：单测/冒烟结果、SQL JOIN 对账、不变量、checksum 比较。
4. **性能证据**：每查询 median/p95、TOTAL、QPS、失败数、轮次、离散度。
5. **口径与限制**：hot/throughput、连接数、`--mp`、绑核、是否 LIMIT、是否排除 GQL 超限。
6. **原始产物**：`report.json`、`sql_manifest`、日志路径。

## 维护约定

- 数据集下载记录 URL/SHA256；不同数据集/规模的数字不得横比。
- 路径/端口/用户变量化；凭据不进 argv/文件；不写个人 home、挂载盘、私网 IP。
- GQL row cap 等安全限制要注明，不要当环境故障。
- 结果目录按 `run_id` 归档；不要覆盖历史基线。

## 完整性校验

用例位于姊妹 skill `pggraph-architecture/cases/skill-integrity/`（随系列首 PR 交付）：

```bash
# 源码树中，且 pggraph-architecture 已合入（系列首 PR）时可执行：
bash ../pggraph-architecture/cases/skill-integrity/run.sh
# 缺失的姊妹 skill 记为 pending 而非失败；退出码 0 = 已合入成员全部通过
```

若环境中不存在该脚本（架构 skill 未合入/安装平铺布局），跳过本节，不要把
"No such file" 误报为校验失败。
