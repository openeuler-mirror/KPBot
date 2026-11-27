---
name: mysql-app-config-optimization
description: >
  MySQL my.cnf 场景化配置优化子技能：连接/表缓存/PS、Buffer Pool（大小/实例/chunk）、
  redo/log buffer、binlog/durability（sync_binlog、flush_log_at_trx_commit）、刷脏/IO、
  自旋、AHI、读写 IO 线程、purge、thread pool、large pages/NUMA、doublewrite/原子写；
  覆盖 Point/RO vs RW/WO、8U32G 20t/64t、2C8G/8C16G/32C256G 容器矩阵，在线/重启/
  重编译分类，参数原理、风险、模板渲染与回滚。用于把热点证据落成可验证的 my.cnf 变更；
  不负责 OS/IRQ/大页（→ mysql-os-optimization）、源码补丁（→ mysql-source-optimization）、
  压测判定（→ mysql-test-verification）。触发词：my.cnf、buffer pool、innodb_log_file_size、
  table_open_cache、sync_binlog、AHI、spin、IO 线程、参数调优、配置模板、容器 8U32G。
license: MulanPSL-2.0
compatibility: MySQL 8.0.25/8.0.28；Percona Server 5.7/8.0 部分参数名不同；鲲鹏 920/920G + openEuler 实测
metadata:
  skill_type: tool_assisted
  category: tuning
  domain: relational_database
  version: "1.0.0"
---

# MySQL my.cnf 场景化配置优化

## 简介

把“参数=数值”变成“证据→目标→参数→验证→回滚”：

```
瓶颈（hotspot）→ 目标（TPS/延迟/CPU/IO）→ 选参数组 → 单变量/单组变更
  → 在线或重启 → MTR/冒烟 → 同口径 A/B → 保留/回滚
```

参数不是越多越好；**先算内存预算与并发，再套场景矩阵**。

## 何时触发

- 需要为 Point/RO/RW/WO 或容器规格生成/调整 my.cnf。
- Buffer Pool、redo、binlog、刷脏、IO 线程、自旋、AHI 需要按场景取值。
- 出现 OOM、写尾延迟、脏页积压、锁等待、PS 上限、fd 上限等参数相关问题。
- 需要判断参数是在线生效还是必须重启。
- 需要可回滚的配置变更记录。

**不适用**：IRQ/THP/大页/sysctl → `mysql-os-optimization`；补丁/编译 →
`mysql-source-optimization`；部署/初始化 → `mysql-build-deployment`；A/B 方法 →
`mysql-test-verification`。

## 变量约定

| 变量 | 含义 | 示例 |
| --- | --- | --- |
| `${SPEC}` | 规格 | `8u32g` / `32c256g` |
| `${MEM_GB}` | 内存 | 32 |
| `${DATASET}` | 数据集 | `20t_100w` / `64t_1000w` |
| `${SCENARIO}` | 场景 | `point` / `ro` / `rw` / `wo` |
| `${DATA_ROOT}` | 数据目录 | `/data/mysql` |
| `${MYSQL_HOME}` | 安装前缀 | `/opt/mysql-8.0.25` |
| `${SOCKET}` | socket | `${DATA_ROOT}/mysql.sock` |
| `${PORT}` | 端口 | 3306 |
| `${OUT_CNF}` | 输出配置 | `/etc/my.cnf` |
| `--write` / `--force` | 落盘开关 / 允许覆盖（自动备份、原子替换） | 默认 dry-run |
| `--disable-tls` | 显式关闭 TLS，仅限隔离压测 | 默认 `ssl=1` |
| `${LOGDIR}` / `${TMPDIR_DB}` / `${RUNDIR}` | 日志/临时/运行目录 | `/data/mysql/{log,tmp,run}` |

## 内存预算（先算再填）

```
buffer pool      ≈ MEM × 50%–70%
每连接/排序/临时  ≈ 连接数 × (sort/join/read buffers) + 临时表
redo log buffer  ≈ 1–3G（大事务/高并发）
binlog cache     ≈ 事务大小 × 并发（按需）
OS/其他          留 20%–30%
```

- 8U32G 基线 buffer pool 22G；2C8G 4G；8C16G 8G；32C256G 128G。
- 容器 `--memory` 必须大于 buffer pool + 余量；否则 OOM/被 kill。
- `innodb_buffer_pool_instances` 不是越多越好：小 pool 配 16 个实例会增加管理开销。

## 参数分组与原理

### 1. 连接与元数据

| 参数 | 原理 | 取值建议 |
| --- | --- | --- |
| `max_connections` | 连接线程上限 | ≥并发峰值，留 20% |
| `back_log` | accept 队列 | 2048–4000 |
| `thread_cache_size` | 空闲线程复用 | 接近并发峰值 |
| `table_open_cache` | 打开表缓存 | 表多/并发高：16000–500000 |
| `table_open_cache_instances` | 分片降争用 | 16–64 |
| `max_prepared_stmt_count` | PS 上限 | 并发×语句数，1280000–4194304（MySQL 8.0 上限 4194304） |
| `open_files_limit` | FD 上限 | ≥表数+连接，102400+ |

### 2. Buffer Pool

| 参数 | 原理 | 取值建议 |
| --- | --- | --- |
| `innodb_buffer_pool_size` | 数据页缓存 | 内存 50%–70% |
| `innodb_buffer_pool_instances` | 分片降 mutex | 8–16（大 pool） |
| `innodb_buffer_pool_chunk_size` | 分配粒度 | 默认 128M；在线调整需对齐 |
| `innodb_old_blocks_pct/time` | LRU 冷热 | 默认通常够用 |
| `innodb_change_buffer_max_size` | 二级索引延迟合并 | 写多读少 25；读多可降 |

### 3. redo / binlog / durability

| 参数 | 原理 | 取值建议 |
| --- | --- | --- |
| `innodb_log_file_size` | redo 单文件 | 1–4G（8U32G 基线 2048M；32C256G 取 4G 上限） |
| `innodb_log_files_in_group` | redo 文件数 | 4–8（8.0.30 前） |
| `innodb_log_buffer_size` | redo 内存缓冲 | 1–3G |
| `innodb_flush_log_at_trx_commit` | 提交 redo | 1（durable）/2/0 |
| `sync_binlog` | binlog fsync | 1（durable）/N/0 |
| `binlog_format` | 日志格式 | ROW（默认） |
| `binlog_row_image` | 行镜像 | FULL/MINIMAL |
| `binlog_group_commit_sync_delay` | 组提交等待 | 0–1000us |
| `binlog_group_commit_sync_no_delay_count` | 批量阈值 | 按并发 |

### 4. 刷脏/IO

| 参数 | 原理 | 取值建议 |
| --- | --- | --- |
| `innodb_io_capacity` | 后台 IO 基线 | NVMe 20000–30000 |
| `innodb_io_capacity_max` | 峰值 IO | 本模板 40000（1.33× baseline）；通常 1.5–2×，按盘实测可低于该倍数 |
| `innodb_max_dirty_pages_pct` | 脏页高水位 | 75–90 |
| `innodb_max_dirty_pages_pct_lwm` | 预刷低水位 | 0–10 |
| `innodb_lru_scan_depth` | LRU 扫描 | 1024–9000 |
| `innodb_page_cleaners` | 刷脏线程 | 4–16 |
| `innodb_flush_neighbors` | 邻页刷 | SSD 0 |
| `innodb_flush_method` | IO 方式 | O_DIRECT |
| `innodb_doublewrite` | 页原子性 | 原子写确认后可 0 |

### 5. 并发/自旋

| 参数 | 原理 | Point/RO | RW/WO |
| --- | --- | --- | --- |
| `innodb_spin_wait_delay` | 自旋间隔 | 20 | 30 |
| `innodb_sync_spin_loops` | 自旋次数 | 100 | 110 |
| `innodb_spin_wait_pause_multiplier` | pause 倍数 | 5 | 5 |
| `innodb_thread_concurrency` | 并发上限 | 0 | 0 |
| `innodb_read_io_threads` | 读 IO 线程 | 25–35 | 20 |
| `innodb_write_io_threads` | 写 IO 线程 | 5 | 15 |
| `innodb_purge_threads` | purge | 1 | 4–16 |

### 6. 其他

| 参数 | 说明 |
| --- | --- |
| `innodb_adaptive_hash_index` | 高 CPU 读多可 OFF；场景化 A/B |
| `performance_schema` | 压测 OFF；定位时开 |
| `large_pages` | 需大页与 buffer pool 匹配 |
| `innodb_numa_interleave` | NUMA 交错；需评估 |
| `transaction_isolation` | RC 可降锁；业务兼容性 |
| `skip_name_resolve` | 避免 DNS 反解 |
| `innodb_page_size` | 仅初始化时确定；变更需新建实例并迁移数据 |
| `lower_case_table_names` | 仅初始化时确定 |

## 场景矩阵（8U32G / 64t 基线）

| 参数 | Point/RO | RW/WO |
| --- | --- | --- |
| `innodb_buffer_pool_size` | 22G | 22G |
| `innodb_buffer_pool_instances` | 16 | 16 |
| `innodb_log_file_size` | 2048M | 2048M（写大可增） |
| `innodb_log_buffer_size` | 2G | 2G |
| `innodb_io_capacity` | 30000 | 30000 |
| `innodb_max_dirty_pages_pct` | 90 | 90 |
| `innodb_spin_wait_delay` | 20 | 30 |
| `innodb_sync_spin_loops` | 100 | 110 |
| `innodb_write_io_threads` | 5 | 15 |
| `innodb_read_io_threads` | 25 | 20 |
| `innodb_purge_threads` | 1 | 4 |
| `table_open_cache` | 500000 | 16000 |
| `max_prepared_stmt_count` | 4194304 | 4194304 |
| `innodb_adaptive_hash_index` | OFF（高压读多） | 按 A/B |

>
> 20t/100w 与 64t/1000w 的差异主要在 dataset/表缓存/工作集；不要只按表数改 buffer pool。

## 容器规格矩阵

| 参数 | 2C8G | 8C16G | 32C256G |
| --- | --- | --- | --- |
| buffer pool | 4G | 8G | 128G |
| instances | 4 | 8 | 8 |
| log file | 1G | 2G | 4G |
| page cleaners | 4 | 8 | 8 |
| read/write io threads | 1/1 | 4/4 | 16/16 |
| purge threads | 1 | 4 | 16 |
| max_connections | 1200 | 4000 | 48000 |
| table_open_cache | 1024 | 2048 | 2048 |
| table_open_cache_instances | 4 | 16 | 16 |
| thread_pool_size（Percona） | 2 | 8 | 32 |

**规格/场景覆盖规则**（与 `scripts/render_mycnf.sh` 一致）：规格矩阵是各规格的硬基线；
上面的场景矩阵只在 `8u32g`/64t 基线上覆盖读/写线程与表缓存（Point/RO 为 25/5、500000/64，
RW/WO 为 20/15、16000/16），其它规格的 Point/RO 不覆盖线程与表缓存（保留规格表取值，
`2c8g/ro` = 读/写 1/1、表缓存 1024），其它规格的 RW/WO 同样沿用规格表；
RW/WO 的 `innodb_purge_threads` 提升到至少 4 且不降低规格基线（`8u32g/rw` = 4，
`32c256g/rw` 仍为 16）。逐组合最终值见
`references/config-matrix.md` §8。

容器还常见：`transaction_isolation=READ-COMMITTED`、`binlog_format=ROW`、
`sync_binlog=1`、`flush_log_at_trx_commit=1`、`innodb_doublewrite=1`、`skip_name_resolve=ON`。
TLS 默认保持开启（`ssl=1`）；`ssl=0` 只在显式选择的隔离压测中使用——要求 TLS 的远端客户端会连接失败，
允许降级的客户端可能明文传输。

## 在线 / 重启 / 重编译

| 类别 | 例子 | 注意 |
| --- | --- | --- |
| 动态全局 | `max_connections`、`io_capacity`、`spin_wait_delay`、AHI | `SET GLOBAL` 后 `SHOW VARIABLES` 复核 |
| 动态但需谨慎 | buffer pool 大小（chunk 对齐）、AHI | 在线调整可能抖动 |
| 需重启 | `innodb_log_file_size`、`innodb_doublewrite`、部分 instances | 备份 + 窗口 + 复验 |
| 仅初始化可设 | `innodb_page_size`、`lower_case_table_names` | 初始化时随 datadir 固定；改值重启会因页大小与表空间不匹配而无法打开，须新建实例并迁移数据 |
| 需重编译 | 补丁、`-march`、PSI | 见 mysql-source-optimization |

## 配置落地流程

1. 复制/渲染模板：`scripts/render_mycnf.sh`（默认 dry-run）；确认 `max_prepared_stmt_count ≤ 4194304`
   且 `ssl=1`（除非显式隔离压测）。脚本拒绝取值中的换行/控制字符。
2. 与现网 my.cnf diff；确认没有 `!include`/命令行覆盖。
3. 备份 `my.cnf.bak.<ts>`；记录改前值。`--write` 在同目录临时文件写完后校验再原子替换，
   保留备份与目标权限，失败不会截断目标文件。
4. validate：`mysqld --defaults-file="${OUT_CNF}" --validate-config`（MySQL 8.0.16+）校验语法与取值，
   失败即停止；再按在线/重启分类执行（`SET GLOBAL` / 窗口重启）。`innodb_page_size` 仅初始化可设。
5. `SHOW VARIABLES` 回读；MTR/冒烟通过后四场景 A/B；只保留有增益的组。
6. 失败按备份回滚并重启复验。

## 风险与回滚

| 变更 | 风险 | 回滚 |
| --- | --- | --- |
| buffer pool 增大 | OOM、页回收 | 恢复原值 + 重启 |
| log file/文件数 | 恢复时间、启动失败 | 恢复原配置 + 重启 |
| durability 降低 | 丢数据 | 恢复 1/1 并声明 |
| io_capacity 过大 | IO 竞争/尾延迟 | 恢复原值 |
| dirty 水位过高 | 尾延迟/恢复慢 | 恢复原值 |
| AHI 切换 | 短时抖动/负收益 | `SET GLOBAL` 恢复 |
| 原子写/关 doublewrite | 数据损坏 | 恢复 doublewrite + 校验 |

高风险（durability、数据格式、原子写）必须用户确认与备份。

## 常见问题

| 症状 | 可能参数 | 处置 |
| --- | --- | --- |
| 启动 OOM | buffer pool 过大 | 降到内存 50%–70%，检查 cgroup |
| `Too many connections` | max_connections/back_log | 增大并查泄漏 |
| PS 1461 | max_prepared_stmt_count | 增大或降并发 |
| fd 不足 | open_files_limit/ulimit | 同步 systemd LimitNOFILE |
| 写 P99 高 | durability/io_capacity/dirty/doublewrite | 按证据单变量调 |
| 脏页积压 | io_capacity/page cleaners/dirty | 提高刷脏能力，勿盲目拉高 dirty |
| 读延迟抖动 | AHI/THP/IRQ/buffer pool | 先排除系统层 |
| 参数不生效 | include/命令行/需重启 | 查 `SHOW VARIABLES` 与启动参数 |

## 参考资料与脚本

| 文件 | 何时读 |
| --- | --- |
| `references/config-matrix.md` | 参数原理、场景/规格矩阵、在线/重启、风险与验证 |
| `scripts/render_mycnf.sh` | dry-run 渲染 my.cnf（按规格/场景/路径）；`--write` 同目录临时文件校验后原子替换并保留备份/权限 |
