---
name: pggraph-architecture
description: >
  pgGraph 0.1.8（PostgreSQL 14–18 的 Rust/pgrx 图扩展）源码架构与运行视图（只读知识库）：
  “源表为真相、图为派生状态”的模型，pgrx 后端进程与 thread_local Engine，前向/反向 CSR，
  NodeStore/ResolutionIndex/FilterIndex/RoaringBitmap，`.pggraph` 工件与只读 mmap，
  注册→构建→遍历→搜索→GQL/Cypher→最短路径→连通分量→同步/维护的完整链路，
  PostgreSQL 后端/后台 worker/触发器/SPI 交互，GUC 与关键源码路径。用于源码阅读、
  流程追踪、热点定位前的架构核对和优化影响面评估；不直接执行编译、部署、压测、改码或
  数据库变更（分别交给 pggraph-build-deployment / pggraph-test-verification /
  pggraph-hotspot-analysis / pggraph-performance-optimization）。
  触发词：pgGraph 架构、pgGraph 源码、graph.build、graph.traverse、graph.search、
  graph.gql、CSR、pgrx、ResolutionIndex、FilterIndex、.pggraph、反向 CSR、同步覆盖层。
license: MulanPSL-2.0
compatibility: pgGraph 0.1.8（PostgreSQL 18.3、Rust 1.96.0、pgrx 0.19.1 实测）；aarch64/鲲鹏与 x86_64 Linux 通用，PG 14–18 方法一致
metadata:
  skill_type: knowledge_base
  category: guideline
  domain: graph_database
  activation_mode: passive
  side_effect_free: "true"
  version: "1.0.0"
---

# pgGraph 0.1.8 架构与源码视图

> 定位：被动知识库。只回答“系统如何组织、请求如何流动、数据如何落盘、参数影响什么”，
> 不发起编译、部署、压测、改码或数据库变更。本文的百分比与瓶颈结论来自特定实测环境，
> 只用于建立假设，实际结论必须由目标环境证据确认。

## 何时使用

- 读源码前需要模块地图：某个函数属于 SQL facade、Engine、BFS、builder 还是 persistence。
- 追一条查询/构建/同步链路：从 SQL 函数到 CSR、`.pggraph`、SPI 源表。
- 分析热点前核对后端进程模型、线程本地状态、CSR 访问模式和内存归属。
- 评估优化影响面：改排序键、反向 CSR、FilterIndex、同步覆盖层会牵动哪些路径。
- 解释部署/一致性现象：`graph.status()` 为 0、工件复用、触发器同步、`PG018`。

**不适用**：具体编译命令、PG 安装、数据导入、压测执行、火焰图采集、代码补丁实施。
按 `references/` 索引进入对应专项，不要在本 skill 内执行业务动作。

> 交接说明：上述姊妹 skill 与本 skill 同属 pggraph-optimize 领域分组，由同一系列 PR 交付
> （源码位于 `Plugins/app-tuner/skills/pggraph-optimize/`，`install.sh` 会把五个成员平铺
> 安装到 `<tool>/skills/<name>/`，安装后入口即可用——该安装行为随系列第 1 个 PR 交付的
> `pggraph-optimize/DOMAIN.md`（install.sh 依赖它识别分组）合入后生效；此前 install.sh
> 对缺 DOMAIN.md 的领域分组按设计整组跳过，已合入成员暂不安装，属预期窗口期）。若当前环境找不到对应入口
> （未安装/未合入），不要凭空继续部署、压测或改码：把本 skill 的查询结论与出处连同
> 待办（部署需求、压测计划、候选改动）交回维护者，由其安装对应 skill 或人工继续。

## 一句话模型

**把 PostgreSQL 普通表里的行编译成一张内存 CSR 图，用 Rust 紧循环跑图算法，结果再翻译回表行。**
源表始终权威；图是派生状态，可随时从源表重建。

```
普通 PG 表（节点表 / 边表 / 属性表）
  │  graph.add_table / add_edge / add_filter_column（只写 catalog）
  ▼
graph.build()  ── SPI 读源表 ──▶ NodeStore / ResolutionIndex / FilterIndex / 租户位图
  │                              临时 spool 排序 ──▶ 前向 CSR + 反向 CSR
  ▼
Engine（每个 PostgreSQL 后端进程一份，thread_local RefCell）
  ├─ NodeStore（SoA：active bits、table_oid、主键）
  ├─ EdgeStore（前向 CSR；mmap 或 owned）
  ├─ reverse_edge_store（从正向 CSR 派生，后端堆）
  ├─ FilterIndex（类型化过滤列）
  ├─ ResolutionStore + delta（(table_oid, pk) → node_idx）
  ├─ edge_buffer / TxGraphDelta（同步/事务覆盖层）
  └─ tenant_membership（RoaringBitmap）
  │
  ├─ graph.traverse / shortest_path / components：Rust 紧循环，热路径不回 SQL
  ├─ graph.search：对源表构造 SQL 谓词，返回并核验图坐标
  └─ graph.gql / cypher：解析→绑定→逻辑/物理计划→CSR 执行→JSONB
  │
  ▼ 可选 hydrate=true：SPI 回查源表
SQL 行 / JSONB
```

## 关键事实（先记住这些）

1. **扩展名是 `graph`，发行名是 pgGraph**：`CREATE EXTENSION graph`；
   源码在仓库顶层 `graph/`（无 contrib 层），Rust 在 `graph/src/`。
2. **每个 PostgreSQL 后端进程一个 Engine**：`thread_local! { static ENGINE: RefCell<Engine> }`，
   后端之间没有共享 Rust 堆。跨后端共享靠 `.pggraph` 工件 + OS page cache 的只读 mmap。
3. **前向 CSR 可 mmap 共享，反向 CSR 后端私有**：加载时从正向 CSR 重建反向 CSR，
   这是每 backend 私有内存和并发扩展性的关键来源（源码 `engine.rs` 的 `EdgeStore::reversed`）。
4. **FilterIndex / edge_type_registry 加载时反序列化到后端堆**：bincode 载荷不直接 mmap，
   因此也会计入每 backend 私有内存。
5. **CSR 热路径不回 SQL**：邻居是 `targets[offsets[i]..offsets[i+1]]` 连续切片；
   只有入口 `resolve`、出口 hydrate、`graph.search` 和 GQL 写才碰 SPI。
6. **图算法只认 `node_idx: u32`**：主键字符串只在入口/出口翻译；ResolutionIndex 用
   `(table_oid, pk_hash)` 二分/哈希查找，delta 覆盖建图后插入。
7. **边类型是 `u8`**：0 保留为无类型，最多 254 个用户标签；双向注册会写正向+反向两条边。
8. **图工件是自研二进制**：`$PGDATA/<graph.data_dir>/<graph_id>/main.pggraph`，
   128B header + 12 section + CRC32，tmp+fsync+原子 rename，加载后 mmap 只读并校验。
9. **构建是派生状态**：`graph.build()` 从源表全量重建；`graph.reset()` 只清引擎/工件，
   不动源表。`graph.vacuum()/maintenance()` 重建并清理投影代次。
10. **同步靠触发器 + 覆盖层**：源表 DML 写 `graph._sync_log`，查询前 `apply_sync` 回放到
    `edge_buffer`；`mutable_overlay` 可发布持久投影段，`csr_readonly` 的覆盖层只在后端本地。
11. **`graph.search()` 不是向量检索**：它是 contains/exact/prefix/token 的 SQL 谓词搜索，
    再在 Rust 里二次核验，`score` 当前固定 1.0。
12. **`graph.status()` 可能显示 0 节点/边但遍历可用**：status 报告后端内存中的构建元数据，
    不代表 mmap 工件状态；用一次 traverse 验证功能。

## 源码地图（0.1.8）

| 模块 | 路径 | 关键内容 |
|------|------|----------|
| SQL facade | `graph/src/sql_facade/` | `admin.rs`、`traversal.rs`、`search.rs`、`components.rs`、`runtime.rs`、`gql.rs`、`cypher.rs`、`discovery.rs`（GQL/Cypher 链路见 lifecycle §4） |
| 构建入口 | `graph/src/sql_build.rs` | `graph.build()` 的 SQL 入口（execute_build/execute_build_with_mode） |
| 引擎 | `graph/src/engine.rs` | Engine 所有者、resolve、traverse、connected_components |
| 节点/边 | `graph/src/node_store.rs`、`edge_store.rs` | SoA 节点元数据、CSR 平行数组、`reversed()` |
| 遍历 | `graph/src/bfs.rs` | BFS/DFS 主循环、`candidate_allowed`、路径重建 |
| 路径 | `graph/src/path_finder.rs` | 双向 BFS、单侧 BFS、Dijkstra |
| 连通分量 | `graph/src/connected_components.rs` | UnionFind、分量统计 |
| 搜索 | `graph/src/sql_search.rs` | 源表谓词、`SearchValueMatcher` 二次核验 |
| 解析/绑定 | `graph/src/gql/`、`query/` | lexer/parser/ast、semantics、logical/physical plan、execute |
| 持久化 | `graph/src/persistence.rs` | `.pggraph` 读写、mmap、CRC、section 校验 |
| 构建 | `graph/src/builder.rs` | 三阶段构建、spool、排序、OOM 预检 |
| 同步 | `graph/src/sync.rs`、`sql_sync.rs`、`projection/` | `_sync_log`、overlay、投影段、tx_delta |
| 注册/校验 | `graph/src/catalog.rs`、`catalog/validate.rs` | `_registered_tables/edges/filter_columns` |
| 类型/安全 | `graph/src/types.rs`、`safety.rs` | 行类型、错误码、`with_panic_boundary` |
| SQL 测试 | `graph/src/pg_tests/` | `#[pg_test]` 模块 |
| 基准 | `sandbox/` | `run_benchmarks.sh/.py`、LDBC/Panama 转换、hot/cold 报告 |

## 参考索引（渐进披露）

| 文件 | 何时读 |
|------|--------|
| `references/overview-and-source-map.md` | 需要模块职责、数据模型、注册项、源码定位与版本核对时 |
| `references/lifecycle-and-flows.md` | 追构建/遍历/搜索/GQL/最短路径/同步的全链路与中间状态时 |
| `references/storage-csr-artifact.md` | 分析 CSR、反向 CSR、`.pggraph` 布局、mmap/CRC、内存归属时 |
| `references/guc-and-data-model.md` | 查 GUC、catalog 表、过滤/租户/多图、错误码与一致性语义时 |

## 输出契约

回答架构问题时给出四段，避免只给结论：

1. **链路**：SQL 入口 → Rust 模块/函数 → 数据结构/工件（标出 SPI、mmap、file、thread_local 边界）。
2. **证据**：源码路径与函数名，必要时给 `文件:函数`；文档结论标注“实测/经验/官方”。
3. **影响面**：改动会触及哪些后端进程、线程本地状态、工件格式、同步语义或 SQL 接口。
4. **验证入口**：指向对应 skill（部署/测试/热点/优化），不在此处执行。

## 维护约定

- 以 pgGraph 0.1.8 源码为真源；版本差异先核对 `graph/Cargo.toml`、`graph/src/pg_tests/` 与 SQL 脚本。
- 百分比、延迟、内存、节点/边规模等易漂移信息标“实测基线”，不写成通用规格。
- 源资料中 LDBC 规模存在不同口径（约 3.18M 节点/34.5M 边 与 约 100K 节点/500K 边），
  不得混用；一律以 `metadata.json`、`graph.status()`、`report.json` 的实际记录为准。
- 路径一律用 `${PGGraph_SRC}`、`${PGDATA}`、`${PGSQL_PREFIX}` 等变量，不写个人环境绝对路径。

## 完整性校验

校验**当前已合入**的 pgGraph skill 的结构/链接/锚点/evals/敏感信息（链接含 skill 内
「references/ 或 scripts/ 前缀」相对路径与跨 skill 的 `../pggraph-<skill>/<path>` 相对路径——姊妹 skill
未合入时该类引用记 pending；姊妹 skill 随系列 PR
合入；缺失成员在 result.json 的 `pending` 列表记录、不算失败，skills_checked 只统计
树内实际存在的成员、全部合入后为 5）：

```bash
# 在 skill 根目录（pggraph-architecture/）下运行；产物 cases/skill-integrity/results/
# result.json；退出码 0 = 已合入成员全部通过
bash cases/skill-integrity/run.sh
```
