# PostgreSQL（pgsql）优化域（app-tuner 侧）

PostgreSQL 17/18（重点 17.5/18.3，ARM aarch64/鲲鹏实测口径）从**架构理解 → 编译部署 →
正确性/性能验证 → 热点归因 → 配置/源码优化**的完整调优链路。域共五个成员，按系列 PR
交付（见下表状态列），各自独立可调用，组合起来覆盖一次完整的 PostgreSQL 性能优化闭环。

> `pgsql-optimize/` 本身是源码树中的领域分组（以本 DOMAIN.md 登记），不是独立 skill；
> `install.sh` 递归进入本分组后，把**当时已合入**的成员平铺安装到
> `<tool>/skills/<skill-name>/`，由 Claude Code / OpenCode 一层发现。

## 成员

| Skill | 状态 | 职责 | 典型入口 |
|-------|------|------|---------|
| [`pgsql-architecture`](pgsql-architecture/SKILL.md) | 已交付（随本 PR） | 一连接一进程模型与 fork 共享内存语义、SQL 引擎链路（计划缓存/执行器）、缓冲区管理器（BufferAlloc/dynahash/分区锁）、LWLock 与 Lock Hash Table、WAL/MVCC/提交链路、GUC 与源码路径（被动只读知识库） | “ReadBuffer 里面到底做了什么”“为什么每个连接一个进程”“BufMappingLock 锁的是什么” |
| [`pgsql-build-deployment`](pgsql-build-deployment/SKILL.md) | 待合入（系列 PR） | 源码编译（configure/make、LTO/LSE/outline-atomics）、contrib 扩展开发与安装（pg_buffer_stats 等自研观测扩展、补丁管理）、initdb/部署/GUC 模板、版本切换与二进制备份回滚；含只读 `preflight_check.sh` | “从零编译 PG”“装 contrib 扩展”“打补丁后怎么部署回滚” |
| [`pgsql-test-verification`](pgsql-test-verification/SKILL.md) | 待合入（系列 PR） | 只读 SQL 冒烟、源码 ST/UT 测试（src/test/**、t/*.pl）、sysbench 三负载基准与预热口径、pg_stat_statements/pg_buffercache/pg_buffer_stats 观测对账、A/B 同口径与噪声判定；含只读 `smoke_test.sh` | “优化有没有效果”“结果对不对”“三负载基线怎么跑” |
| [`pgsql-hotspot-analysis`](pgsql-hotspot-analysis/SKILL.md) | 待合入（系列 PR） | perf 火焰图/Topdown/等待事件采样/off-CPU 采集方法，ro/rw/wo 三负载热点画像（网络 epoll、计划缓存、buffer 淘汰刷脏链、WAL 提交自旋、LWLock 等待队列锁）；含 dry-run `collect_profile.sh` | “慢在哪”“为什么写负载 IPC 只有 0.23”“LWLockWaitListLock 是什么” |
| [`pgsql-performance-optimization`](pgsql-performance-optimization/SKILL.md) | 待合入（系列 PR） | 计划缓存/GUC 调参、NUM_BUFFER_PARTITIONS 分区数扩容（生日碰撞数学与补丁）、shared_buffers 元数据代价、HNSW/pgvector 检索路径优化与 CAS 竞争观测、WAL/提交参数、编译选项（LTO/LSE）；含 `verify_build_opts.sh` | “怎么调”“分区数为什么从 128 到 512”“shared_buffers 为什么不是越大越好” |

## 域内路由

```
只想理解系统/源码机制   → pgsql-architecture
要编译/装扩展/打补丁    → pgsql-build-deployment
要证明对/证明快         → pgsql-test-verification
要知道慢在哪/为什么慢   → pgsql-hotspot-analysis
要把证据变成改动        → pgsql-performance-optimization
```

一次典型的优化闭环：

```
architecture（理解进程/缓冲区/锁模型）
  → build-deployment（编译、起库、装观测扩展、打补丁）
  → test-verification（冒烟 + sysbench 三负载基线）
  → hotspot-analysis（perf/Topdown/等待事件归因）
  → performance-optimization（单变量优化：GUC/补丁/编译）
  → test-verification（同口径 A/B + 正确性门禁）
  → 保留或回滚
```

## 与插件内其他 skill 的边界

| 问题 | 去哪里 |
|------|--------|
| OS governor/THP/大页/irqbalance/ulimit | `kpbot-app-tuner` 的 `os-optimization` |
| BIOS SMT/C-State/NUMA/内存频率 | `bios-optimization` |
| CPU 绑核/NUMA 布局/线程亲和 | `cpu-affinity-optimization` |
| 通用 C/C++ 向量化、循环、汇编 | `kpbot-code-optimizer` |
| pgGraph（Rust/pgrx 图扩展）专属优化 | `pggraph-optimize` 域 |
| NebulaGraph 图数据库 | `nebula-optimize` 域 |
| PostgreSQL 本体（GUC/锁/缓冲区/源码补丁/扩展开发） | 本域 |
| 纯编译/部署/装扩展 | 本域 `pgsql-build-deployment` |
| 纯压测/正确性/A/B | 本域 `pgsql-test-verification` |

## 准入判据

新增成员须满足：

1. 与既有成员职责不重叠（新场景先在本表登记再动笔）；
2. 被动知识类 skill 只描述事实与机制，不含可执行步骤；
3. 主动类 skill 的可执行内容全部走“变量化路径 + 只读优先 + 破坏性操作显式确认”；
4. 实测数字必须带口径（版本/环境/负载/轮次），负结果与未验证候选明确区分；
5. 交付物通过 `pgsql-architecture/cases/skill-integrity/` 的完整性检查。

## Review owners

- 源码/机制表述（architecture/performance 的源码引文）：以目标版本源码为准
  （`src/backend/`、`src/include/`），检视时逐条核对函数名与行号口径；
- 脚本纪律：与 nebula/pggraph 家族同款（shebang、bash -n、退出码 2=用法/3=目录冲突、
  输出目录已存在即拒绝、凭据不落盘不进 argv）。
