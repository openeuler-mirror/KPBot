---
name: mariadb-test-verification
description: >
  MariaDB 10.11.16（兼容 10.3.x 案例）的正确性与性能验证：构建验证、mysql-test-run（mtr）
  单元/回归/suite、复制与 Galera 一致性、数据装载对账、mariabackup 可恢复性、sysbench
  场景（oltp_read_only/point_select/write_only/update_non_index/read_write）、TPC-C
  （TPCCRunner 五类事务与固定占比）、指标口径（TPS/QPS/p50/p99/错误率/CPU/IO）、
  同口径 A/B 复测与噪声判定、结果归档。用于验证“功能是否正确、性能是否真的变好”；
  不适用编译部署（→ mariadb-build-deployment）、热点归因（→ mariadb-hotspot-analysis）、
  参数/源码优化实施（→ mariadb-performance-optimization）。
  触发词：MariaDB 测试、MariaDB 验证、mtr、sysbench、TPC-C、TPCCRunner、QPS、TPS、
  p99、复制一致性、数据对账、A/B 对比、正确性验证。
license: MulanPSL-2.0
compatibility: MariaDB 10.11.16 源码与二进制；10.3.x + TPCCRunner 定制版案例（需业务方提供）；aarch64/x86_64 Linux
metadata:
  skill_type: tool_assisted
  category: testing
  domain: relational_database
  version: "1.0.0"
---
# MariaDB 正确性与性能验证

## 简介

验证分两层：正确性（能启动、mtr 通过、查询/对账/备份/复制正确）与性能（固定口径的
TPS/QPS/延迟/CPU，做基线 vs 候选 A/B）。
原则：**没有正确性的性能数字无效；没有同口径基线的“提升”不可信。**

## 何时触发

- 编译/部署后确认服务可用、数据完整、查询结果正确；源码/参数/二进制/依赖变更后判断收益或回退。
- 建立或复现 sysbench / TPC-C 基线，对比不同部署拓扑/绑核。
- 压测数字异常——**只判数字是否可采信**（口径/预热/轮数/噪声阈值），不做瓶颈归因
  （归因 → `mariadb-hotspot-analysis`）。
- 需要可归档的验证报告（功能证据 + 性能证据 + 口径 + 结论）。

**不适用**：安装/编译/装载本身 → `mariadb-build-deployment`；火焰图与热点归因 →
`mariadb-hotspot-analysis`；优化杠杆实施 → `mariadb-performance-optimization`。

## 验证矩阵与流程

逐层手段/依赖/门禁见 `references/functional-verification.md` §1–§8：构建（版本/md5/`ldd`/启动）、
mtr（源码改动必跑相关 suite，需 Debug 构建）、SQL 冒烟/对账、复制（位点/GTID/
`SHOW SLAVE STATUS\G`）、备份（`--prepare` + 隔离实例 `--copy-back` + 计数对账，10.11 无
`--verify`）、系统压测、A/B 判定。流程：①构建验证 → ②功能冒烟 → ③mtr（按改动范围）→
④数据/复制/备份对账 → ⑤系统基线（多轮）→ ⑥候选复测（同口径）→ ⑦判定归档；任一失败
先修正确性，停止性能测试。

## 功能验证

### 构建与启动

```
"${MARIADB_HOME}/bin/mariadbd" --version
md5sum "${MARIADB_HOME}/bin/mariadbd"
ldd "${MARIADB_HOME}/bin/mariadbd" | grep -Ei 'jemalloc|tcmalloc|ssl|crypto' || true

mariadb --socket="${SOCKET}" -e "SELECT VERSION(), @@version_comment;"
mariadb --socket="${SOCKET}" -e "SHOW ENGINES;" | grep -i innodb
```

`ldd` 是链接检查，不是下载物完整性校验；sha256/GPG 校验须在编译前完成。

### mtr（mysql-test-run）

mtr 是正确性门禁；源码/补丁改动后按范围跑相关 suite。完整 suite 选择表、运行参数与失败
复现见 `references/functional-verification.md` §2（本文件不重复，避免两处命令漂移）。最小调用：

```
cd "${SRC_ROOT}/mysql-test"
perl mariadb-test-run.pl --suite=innodb --parallel=8 --retry=0   # 按改动范围选 suite
```

纪律：独立 datadir/端口，不与生产实例混跑，失败看 `${SRC_ROOT}/mysql-test/var/log/`；
解析/优化器/复制改动必须跑对应 suite，不只跑一个用例；`--force` 适合摸底，门禁判定不带。

### SQL 冒烟与数据对账

`scripts/smoke_test.sh` 是只读冒烟：版本/引擎/字符集（时区仅 info，不作门禁）、库表存在性/
行数抽样/主键等值 `EXPLAIN`、`@@tx_isolation`/`@@read_only` 配置查询、复制状态；范围/排序/
聚合查询手工补充（references §3）。提交/回滚语义**不在脚本内**：退出 0 不等于事务语义通过；
须在隔离库建本轮唯一库（重名即停）核对 COMMIT 后可见与 ROLLBACK 后不可见，只清理本轮
确认创建的库，绝不复用/删除既有库。

```
MARIADB_BIN="${MARIADB_HOME}/bin/mariadb" SOCKET="${SOCKET}" \
  DB=bench SAMPLE_TABLE=sbtest1 EXPECT_MIN_ROWS=1000 bash scripts/smoke_test.sh
```

写入类验证（INSERT/UPDATE/DELETE/DDL）必须在隔离库或临时实例，不能在生产库跑。对账：
装载后行数与期望一致、抽样类型/日期/字符串长度/NULL 比例、索引 `EXPLAIN` 命中；详见
references §3/§4。

### 复制与备份

```
SHOW SLAVE STATUS\G          -- Slave_IO_Running / Slave_SQL_Running / Seconds_Behind_Master
SHOW STATUS LIKE 'wsrep_%';  -- Galera：wsrep_ready / wsrep_cluster_status / wsrep_local_recv_queue
```

`\G` 纵向输出后按行解析（未配置从库走 info 分支，健康从库不再误报）。MariaDB 口径用
`Gtid_IO_Pos`/`@@gtid_slave_pos`，没有 `Retrieved_Gtid_Set`/`Executed_Gtid_Set`（MySQL 列名）。
复制一致性：主从关键表计数比对，条件允许时用块级校验；备份可恢复性：`--prepare` 成功 +
隔离实例 `--copy-back` + 计数对账，缺一不可。完整命令见 references §5/§6。

## 性能验证

场景、工具链、指标口径与日志解析见 `references/benchmark-harness.md` §2/§3/§5/§6：sysbench
（oltp_read_only/point_select/write_only/update_non_index/read_write）、TPCCRunner（create/
loader/index/master/slave 五步，多 slave 用不同 warehouse 区间，`userCount ≤ poolSize`）、
p99 须由 `--percentile=99` 或保留的延迟直方图得出，区间 p95 不得反推。`numactl` 必须直接包
`mariadbd`（不能套在 systemctl 客户端上），systemd drop-in 方案同节给出。

TPC-C 占比（测试方案固定，禁止修改，两腿一致）：

```
newOrder=1  payment=1  orderStatus=45  delivery=43  stockLevel=10
思考时间全 0；warmup 10min，run 40min（可缩短但两腿一致）
```

> 上述占比是家族测试方案的偏斜口径（Order-Status/Delivery 为主），不是 TPC-C 标准规格的
> 45/43/4/4/4；字段名与数值按测试方案原样保留，两腿必须一致，不要混比。

A/B 纪律：同一数据快照/配置/并发/时长，只改一个变量；每腿 ≥3 轮，波动 >5% 视为无效重测；
两腿记录 md5/commit，数据目录、redo 大小、绑核必须一致，每次压测前重置到同一快照；结果
分列 TPS/QPS、错误率、p50/p99、CPU、IO、客户端状态，不合并成一个“提升 X%”，p99 拿不到
就显式标缺失。详见 `references/ab-protocol.md` §1–§7。

## 常见失效模式

| 现象 | 处置 |
| --- | --- |
| QPS 低但服务端 CPU 不高 | 客户端先瓶颈：加压测进程/核、减小结果集 |
| 写入 TPS 抖动、`Innodb_log_waits` 增长 | 增大 redo、提高 io_capacity、分盘 |
| 错误率升高 / P99 长尾 | 查 `INNODB_TRX`/`PROCESSLIST`/错误日志；查刷脏、历史列表、慢日志 |
| 两轮差异大 / 参数“提升”不可复现 | 重置到同一快照、独占机器；单变量重测，>5% 波动不采信 |
| 新二进制结果不同 | 停性能测试，跑 mtr 与路径比对 |

完整表（现象/可能原因/处置）见 `references/ab-protocol.md` §8。

## 输出契约

报告至少包含：①环境与身份（硬件/NUMA、OS/编译器、MariaDB 版本与 md5、编译参数、依赖版本、
数据集与规模、部署形态/绑核、关键参数快照）；②功能证据（mtr 结果、冒烟查询与期望值、对账表、
复制/备份校验、失败样本）；③性能口径（场景/工具、并发/时长、warmup、轮数、数据重置、
客户端绑核）；④每场景 TPS/QPS、错误率、p50/p99、CPU（服务端/客户端）、IO；⑤判定
（有效/无效/劣化、是否保留、下一步）；⑥可复现命令（不依赖未记录的 shell 状态）。
p99 须来自 `--percentile=99` 或保留的延迟直方图，否则标“缺失”，不得用区间 p95 反推。
模板见 `references/ab-protocol.md` §6。

## 变量表（脚本与正文统一引用）

| 变量 | 含义 | 默认值 |
|------|------|--------|
| `${MARIADB_BIN}` | 客户端可执行（mariadb） | `mariadb` |
| `${SOCKET}` / `${HOST}`+`${PORT}` | 连接方式二选一（TCP 时端口 1-65535 校验） | 空 / `127.0.0.1`+`3306` |
| `${DB_USER}` / `${DB}` | 用户 / 业务库（可选） | 空 |
| `${MARIADB_DEFAULTS_FILE}` | 凭据文件（0600，优先于命令行传密码；sysbench 不读此项，用其 `--config-file`，见 references §2.2） | 空 |
| `${SAMPLE_TABLE}` / `${EXPECT_MIN_ROWS}` | 抽样表 / 行数下限期望 | 空 / `0` |
| `${CHECK_REPLICATION}` | 复制状态检查（auto/off） | `auto` |
| `${EXPECT_WRITABLE}` | 默认关，只读冒烟不判可写；写验收显式 =1——核 `read_only=OFF` | `0` |
| `${VERIFY_DB}` | 隔离验收库名（本轮唯一，重名即停；只清理本轮确认创建的库） | 示例生成：`verify_tmp_<时间戳>_<PID>` |
| `${RUN_SECS}` | 正式 run 窗口秒数（与 `sysbench --time` 一致，采集器据此并行覆盖） | `600`（示例） |

## 参考资料与脚本

| 文件 | 何时读 |
| --- | --- |
| `references/functional-verification.md` | 构建验证、mtr suite 选择、SQL 冒烟、对账、复制/备份校验、错误签名、验证矩阵 |
| `references/benchmark-harness.md` | sysbench 场景与参数、TPC-C 五步、绑核、指标解析、客户端瓶颈、结果模板 |
| `references/ab-protocol.md` | 基线/候选、多轮、噪声、保留/回滚、报告模板、失效模式表 |
| `scripts/smoke_test.sh` | 只读 SQL 冒烟（版本/引擎/计数/索引/事务配置/复制；事务提交回滚语义另见 §3 隔离库验收） |
