---
name: pgsql-architecture
description: >
  PostgreSQL 17/18（重点 17.5/18.3）源码架构与运行视图（只读知识库）：一连接一进程与
  fork 共享内存语义、PostgresMain 命令循环与 epoll 等待、解析/计划缓存/执行器链路、
  缓冲区管理器（BufferAlloc→BufTableLookup dynahash→PinBuffer CAS、clock-sweep 淘汰、
  NUM_BUFFER_PARTITIONS 分区锁）、LWLock 体系与 Lock Hash Table 重量级锁/fastpath、
  WAL 插入与提交刷盘链路、MVCC 与 heap_page_prune、GUC 与源码路径。用于源码阅读、
  流程追踪、热点定位前的架构核对和优化影响面评估；不直接执行编译、部署、压测、改码或
  数据库变更（分别交给 pgsql-build-deployment / pgsql-test-verification /
  pgsql-hotspot-analysis / pgsql-performance-optimization）。
  触发词：PostgreSQL 架构、PG 源码、ReadBuffer、BufferAlloc、BufTableLookup、
  dynahash、NUM_BUFFER_PARTITIONS、LWLock、Lock Hash Table、XLogInsertRecord、
  一连接一进程、共享内存、计划缓存、clock-sweep。
license: MulanPSL-2.0
compatibility: PostgreSQL 17.5 与 18.3（官方 tag REL_17_5 / REL_18_3）源码核对；aarch64/鲲鹏与 x86_64 Linux 通用
metadata:
  skill_type: knowledge_base
  category: guideline
  domain: relational_database
  activation_mode: passive
  side_effect_free: "true"
  version: "1.0.0"
---

# PostgreSQL 17/18 架构与源码视图

> 定位：被动知识库。只回答“系统如何组织、请求如何流动、数据如何落盘、参数影响什么”，
> 不发起编译、部署、压测、改码或数据库变更。本文的百分比与瓶颈结论来自特定实测环境
> （见各表口径行），只用于建立假设，实际结论必须由目标环境证据确认。
> 源码锚点：PostgreSQL 官方仓库 tag `REL_17_5` / `REL_18_3`，行号按 17.5 记述、
> 17/18 差异处单独标注。

## 何时使用

- 读 PG 源码/火焰图时需要确认某函数在链路上的位置与职责（如 `BufTableLookup` 在
  ReadBuffer 的哪一步）；
- 优化前评估影响面（改 `NUM_BUFFER_PARTITIONS` 影响哪些锁、哪些路径）；
- 排障时把现象映射到机制（`BufferMapping` 等待事件对应哪把锁、哪个数据结构）；
- 向他人解释 PG 机制（fork 共享内存语义、计划缓存行为、LWLock 分区）。

不适用：要动手编译/部署（→ pgsql-build-deployment）、要跑压测/对账（→
pgsql-test-verification）、要采集热点（→ pgsql-hotspot-analysis）、要改参数/源码（→
pgsql-performance-optimization）。

## 一句话模型

**一连接一进程 + 共享内存三件套（缓冲池/WAL/锁表）**：每个客户端连接对应一个
postgres backend 进程（postmaster fork 而来）；所有数据页进共享缓冲池、所有变更先写
WAL、所有重量级锁记录在共享 Lock Hash Table；backend 之间靠 LWLock（自旋/睡眠）与
buffer pin 协作，无线程级共享堆。

## 关键事实（先记住这些）

1. **一连接一进程**：postmaster `fork()` 出 backend；fork 后子进程的 static 变量
   （如 `MyProc`、`LockMethodLocalHash`）各自独立一份，共享段（shmem）经
   `PGSharedMemoryCreate` 在 postmaster 启动时创建、子进程经继承映射——fork 不复制
   共享段内容，只复制映射关系。`MyProc` 指向共享段里的 ProcGlobal 槽位，因此每个
   backend 的 `static MyProc *` 值不同、数据在共享内存。
2. **需要等待的读命令路径多一次 epoll**（PG16+ 标准行为，`src/backend/libpq/be-secure.c`）：
   客户端 socket 被 `pg_set_noblock` 设为非阻塞；数据已到时首次 `recv()` 即成功，
   仅在需等待时走 `recv()→EAGAIN→WaitEventSetWait→epoll_pwait→重试 recv`——
   epoll 次数与“需等待的读取”成正比，按命令数估算是上界口径（实测 ro 负载 6.4%
   CPU，口径见 references/sql-engine-and-execution.md）。
3. **计划缓存默认不是“解析一次就永远复用”**：`GetCachedPlan` 命中后仍可能走
   `BuildCachedPlan→standard_planner`（custom plan）；generic plan 生效需满足重复
   执行次数阈值等条件。实测读负载中重建计划路径占据可观 CPU 采样占比（系列数字见
   sql-engine-and-execution.md，CPU 采样口径）；调用频率结论须另以同窗口
   Δplans/Δcalls 支撑，两类证据不可互相冒充。
4. **ReadBuffer 的锁路径**：`ReadBuffer→PinBufferForBlock→BufferAlloc`（
   `src/backend/storage/buffer/bufmgr.c`）＝ `BufTableHashCode` → 取
   `BufMappingPartitionLock` 分区（分区数 = `NUM_BUFFER_PARTITIONS`，编译期常量，
   `src/include/storage/lwlock.h`）→ `LWLockAcquire(SHARED)` → `BufTableLookup`
   （dynahash HTAB）→ `PinBuffer`（CAS 增加 refcount/usagecount）→ 释放分区锁。
   页已在池中时始终走 found 路径，不发生 I/O。
5. **LWLock 分三类锁位**：目标锁本体（如 WALInsertLock）、**等待队列自旋锁**
   `LWLockWaitListLock`（排队/唤醒都要先抢它）、buffer 头自旋 `LockBufHdr`。
   高并发提交下等待队列自旋锁本身成为共享热点行（写负载实测 32.3% CPU；帧内为
   原子检查+短暂自旋混合，长等待走信号量睡眠——见架构参考 §4.1 注）。
6. **重量级锁走共享 Lock Hash Table**：LOCK 条目与 PROCLOCK 条目都在共享 dynahash
   表中，按 locktag 哈希分到 `NUM_LOCK_PARTITIONS`（=1<<`LOG2_NUM_LOCK_PARTITIONS`，
   `src/include/storage/lock.h`）个分区锁；表锁/页锁等**关系级**条目同表管理。
   普通**行锁**不在此表——记在堆元组头，等待表现为对方事务 ID。fastpath 只覆盖
   特定弱模式的**关系锁**（本地数组，不碰共享表），冲突升级才进共享表排队。
7. **写路径三大块**：WAL 记录组装插入（`XLogInsertRecord`）、提交刷盘
   （`XLogFlush`；`WaitXLogInsertionsToFinish` 为原子检查+自旋与 `LWLockWaitForVar`
   信号量睡眠的混合，勿把整段当自旋）、DML 执行（`ExecModifyTable`：UPDATE 含
   索引维护 `_bt_doinsert` 与页满时 `heap_page_prune` 清 HOT 死链）。
8. **后台进程分工**：bgwriter（批量刷脏）、checkpointer（checkpoint 合并 fsync）、
   walwriter（提前刷 WAL）；它们消化不及时时，查询进程会在读路径同步刷脏 victim 页
   （`FlushBuffer→mdwritev`）并排队 fsync（`ForwardSyncRequest`）。

## 源码地图（REL_17_5）

| 模块 | 路径 | 关键内容 |
|------|------|----------|
| 命令循环 | `src/backend/tcop/postgres.c` | PostgresMain、exec_* 消息处理、门户（Portal）生命周期 |
| 计划缓存 | `src/backend/utils/cache/plancache.c` | GetCachedPlan、BuildCachedPlan、generic/custom plan 选择 |
| 执行器 | `src/backend/executor/` | PortalRun、ExecScan/ExecSort/ExecAgg/ExecModifyTable、表达式解释器 |
| 缓冲区管理 | `src/backend/storage/buffer/bufmgr.c`、`buf_table.c`、`freelist.c` | ReadBuffer/BufferAlloc/BufTableLookup/PinBuffer、clock-sweep（StrategyGetBuffer/GetVictimBuffer）、刷脏 FlushBuffer |
| 缓冲区头 | `src/backend/storage/buffer/`、`src/include/storage/buf_internals.h` | BufferDesc 状态字、LockBufHdr 自旋、BUFFER_MAPPING_LWLOCK_OFFSET |
| LWLock | `src/backend/storage/lmgr/lwlock.c`、`src/include/storage/lwlock.h` | LWLockAcquire/Release、LWLockWaitListLock、NUM_BUFFER_PARTITIONS |
| 重量级锁 | `src/backend/storage/lmgr/lock.c` | LockAcquireExtended、LockMethodLocalHash/fastpath、Lock Hash Table 分区 |
| 哈希表 | `src/backend/utils/hash/dynahash.c` | HTAB、Directory/Segment 结构、hash_search_with_hash_value |
| WAL | `src/backend/access/transam/xloginsert.c`、`xlog.c` | XLogInsertRecord、XLogFlush、WaitXLogInsertionsToFinish |
| 堆/索引 | `src/backend/access/heap/`、`src/backend/access/nbtree/` | heap_page_prune、_bt_doinsert/_bt_compare |
| 网络 | `src/backend/libpq/` | be-secure.c 非阻塞 socket、pq_recvbuf、WaitEventSetWait |

> 核对方法：`git -C ${PG_SRC} rev-parse REL_17_5` 应解析为真实 tag；函数存在性用
> `git grep -n "BufferAlloc(" REL_17_5 -- src/backend/storage/buffer/bufmgr.c` 复核。

## 参考索引（渐进披露）

| 文件 | 内容 |
|------|------|
| `references/overview-and-process-model.md` | 进程/共享内存模型、fork 语义、后台进程、内存上下文 |
| `references/sql-engine-and-execution.md` | 命令循环、网络路径（epoll）、解析/计划缓存/执行器链路 |
| `references/buffer-manager-and-locks.md` | BufferAlloc 全路径、dynahash 结构、LWLock 体系、Lock Hash Table 与 fastpath |
| `references/wal-mvcc-and-commit.md` | WAL 插入/提交刷盘、MVCC 快照、HOT 剪枝、GUC 影响面 |

## 输出契约

回答机制类问题时至少给出：

1. **函数链**（如 `ReadBuffer→PinBufferForBlock→BufferAlloc→…`）与源码位置
   （文件:函数，版本标注）；
2. **锁与数据结构**（哪把锁、保护什么、谁竞争）；
3. **口径**（结论若来自实测百分比，注明负载与环境口径行）；
4. **不执行**任何数据库变更命令（本 skill 被动只读）。

## 维护约定

- 行号与函数名按 REL_17_5/REL_18_3 核对，版本差异单独标注（如“18 起 xxx”）；
- 素材中带百分比的结论，转述时必须携带口径（负载/环境/归一化方式）；
- 新增机制条目先在 `references/` 找到归属文件再落笔，避免口径漂移。

## 完整性校验

校验**当前已合入**的 pgsql skill 的结构/链接/锚点/evals/敏感信息（链接含 skill 内
「references/ 或 scripts/ 前缀」相对路径与跨 skill 相对路径，按**文件相对语义**解析——
skill 根文件（SKILL.md）用 `../pgsql-<skill>/<path>`，references/ 内文件用
`../../pgsql-<skill>/<path>`——姊妹 skill
未合入时该类引用记 pending；姊妹 skill 随系列 PR
合入；缺失成员在 result.json 的 `pending` 列表记录、不算失败，skills_checked 只统计
树内实际存在的成员、全部合入后为 5）：

```bash
# 在 skill 根目录（pgsql-architecture/）下运行；产物 cases/skill-integrity/results/
# result.json；退出码 0 = 已合入成员全部通过
bash cases/skill-integrity/run.sh
```

## 交接说明

姊妹 skill：`pgsql-build-deployment`（编译部署）、`pgsql-test-verification`（测试
验证）、`pgsql-hotspot-analysis`（热点分析）、`pgsql-performance-optimization`（性能
优化）。同属 pgsql-optimize 领域分组，由同一系列 PR 交付（源码位于
`Plugins/app-tuner/skills/pgsql-optimize/`，`install.sh` 会把五个成员平铺安装到
`<tool>/skills/<name>/`，安装后入口即可用——该安装行为随本分组 DOMAIN.md（已随本 PR
交付）合入后生效）。若当前环境找不到对应入口（未安装/未合入），不要凭空继续部署、
压测或改码：把本 skill 已产出的结论与待办交回维护者处理。
