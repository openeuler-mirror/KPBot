---
name: pgsql-hotspot-analysis
description: >
  PostgreSQL 17/18 热点分析与 profiling：perf 火焰图（on-CPU，多 PID 逗号口径）与
  LTO 帧名归因伪影、ARM PMU Topdown（IPC/Retiring/原子串行化/L3_Bound）、等待事件
  采样（BufferMapping/WALSync 等与机制对应）、off-CPU 与锁等待判别（on-CPU 火焰图
  看不到锁等待）、sysbench 三负载（ro/rw/wo）画像与 MySQL 分层对照、
  LWLockWaitListLock/WAL 提交自旋/buffer 淘汰刷脏链/epoll 等待等竞争机制归因；
  含 dry-run 默认的 collect_profile.sh。触发词：PG 热点、PG 火焰图、PG topdown、
  perf postgres、等待事件采样、LWLockWaitListLock、性能归因。
license: MulanPSL-2.0
compatibility: PostgreSQL 17.5、ARM aarch64 PMU（topdown）实测口径；x86_64 方法一致（指标名随平台变化）
metadata:
  skill_type: analysis
  category: guideline
  domain: relational_database
  activation_mode: manual
  side_effect_free: "true"
  version: "1.0.0"
---

# PostgreSQL 17/18 热点分析

> 定位：只读采集与归因（collect_profile.sh 默认 dry-run，--execute 才落盘采集）。
> 本 skill 不改配置/源码（→ pgsql-performance-optimization），不做正确性判定
> （→ pgsql-test-verification）。百分比结论全部携带口径行，只用于建立假设。

## 何时使用

- 慢/吞吐不达标：要定位是 CPU 计算、锁等待、buffer 刷脏、WAL 提交还是客户端；
- 优化前建立证据基线（火焰图 + topdown + 等待事件三方互证）；
- 优化后归因验证（热点是否迁移/消失）。

## 变量表（脚本与正文统一引用）

| 变量 | 含义 | 默认值 |
|------|------|--------|
| `${PG_BIN}` / `${PGHOST}` / `${PG_PORT}` / `${PGUSER}` / `${DBNAME}` | psql/实例连接 | `/opt/pgsql/bin` / `127.0.0.1` / `5432` / `postgres` / `sbtest` |
| `--pid`（collect_profile.sh） | 目标 postgres PID（backend/后台进程） | 必填（或多实例逗号列表） |
| `--pg-pattern` | pgrep 匹配模式（如 `postgres: postgres sbtest`） | 空=只用 --pid |
| `--duration` | 采样窗口秒 | 20（1-600） |
| `--out` | 输出目录 | `./pgsql_profile_<ts>_<pid>`（已存在即拒绝） |

## 采集纪律

1. **先预热再采样**，窗口覆盖稳态（`--report-interval` 分段 TPS 与均值偏差
   >10% 的窗口作废）；
2. **阻塞睡眠不进 on-CPU 栈；忙自旋在栈里**：锁等待两种形态——睡眠等待在
   off-CPU/等待采样兑现（火焰图正常但吞吐差先查这个）；忙自旋/CAS 轮询仍在
   CPU 上、**可见于 on-CPU 火焰图**（LWLockWaitListLock/WaitXLogInsertionsToFinish
   帧高占比即是）。两种形态对应不同的采样方法，勿用一条规则概括；
3. **多实例逗号口径**：perf/pidstat 的 -p 都是逗号分隔 PID 列表，`pgrep`/`pidof`
   输出空格分隔必须先 `tr ' ' ','`；
4. **LTO 归因伪影**：LTO 构建的火焰图帧名带 `.lto_priv`、exec_* 内联进
   PostgresMain、顶层符号（如 GetCommandTagEnum）可能是归因伪影——解读前先确认
   构建口径（pg_config --configure）。

## 参考索引（渐进披露）

| 文件 | 内容 |
|------|------|
| `references/profiling-playbook.md` | perf/火焰图/topdown/等待事件采样/off-CPU 的命令与判读 |
| `references/workload-portraits.md` | ro/rw/wo 三负载实测画像与分层对照（口径行） |
| `references/contention-mechanisms.md` | 六大竞争机制到症状/证据的映射表 |

## 输出契约

归因结论至少给出：

1. **三方证据**：火焰图（哪个函数栈）+ topdown（IPC/Retiring/串行化/bound 方向）
   + 等待事件（谁在等谁）；
2. **机制归属**：映射到 `references/contention-mechanisms.md` 的哪一条（含
   函数链与锁名）；
3. **口径行**：版本/构建（含 LTO 与否）/负载/窗口/归一化方式；
4. **交接物**：perf.data、folded 栈、等待采样表、env.txt（脚本自动落盘）。

## 维护约定

- 画像数字与新采集结果不混系列（不同环境/轮次各自成基线）；
- 机制名词与 pgsql-architecture 严格同口径。

## 交接说明

姊妹 skill：`pgsql-architecture`（机制知识库）、`pgsql-build-deployment`（编译
部署）、`pgsql-test-verification`（测试验证）、`pgsql-performance-optimization`
（性能优化）。同属 pgsql-optimize 领域分组，由同一系列 PR 交付（源码位于
`Plugins/app-tuner/skills/pgsql-optimize/`，`install.sh` 会把五个成员平铺安装到
`<tool>/skills/<name>/`，安装后入口即可用——该安装行为随分组 DOMAIN.md 合入后
生效；此前 install.sh 对缺 DOMAIN.md 的领域分组按设计整组跳过，入口暂不可用属
预期窗口期）。找不到姊妹入口时把证据包（perf.data/folded、topdown、等待采样、
窗口与结论）连同待办交回维护者。
