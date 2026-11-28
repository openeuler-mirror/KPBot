# sysbench 压测编排

## 1. 场景与事务构成

| 场景 | 命令 | 事务内容 | 主要瓶颈 |
| --- | --- | --- | --- |
| Point | `oltp_read_only --range_selects=0 --skip-trx=1` | point select | 解析/优化、页查找、网络/IRQ |
| RO | `oltp_read_only` | point:simple:sum:order:distinct = 10:1:1:1:1 | 读路径 + collation/排序 + 临时表 |
| RW | `oltp_read_write` | RO + index/non-index update + delete_insert | 行锁、redo、binlog、刷脏 |
| WO | `oltp_write_only` | index/non-index update + delete_insert | redo/binlog fsync、doublewrite、IO |

术语：`range_selects=0` 去掉 range 查询；`skip-trx=1` 不做 BEGIN/COMMIT；
`db-ps-mode=auto` 使用 prepared statement。

## 2. 数据集与 prepare

```
sysbench --db-driver=mysql --mysql-host="${CLIENT_TARGET}" --mysql-port="${MYSQL_PORT}" \
  --mysql-user="${MYSQL_USER}" --mysql-db="${TEST_DB}" \
  --tables=64 --table_size=10000000 --threads=96 --time=180 \
  --report-interval=10 oltp_common prepare
```

- 常用规模：64×1000w（约 64 张 sbtest 表）、20×100w。
- prepare 用较大线程缩短时间，但不要与服务端其他负载并发。
- MySQL 的 `ANALYZE TABLE` 需要显式表名，不支持库名/表名通配符；
  按 `${TEST_DB}` 和 prepare 的实际表数 `${TABLES}` 列出表名，逐表/合批执行并检查退出码。
- 记录数据集大小、表数、行数、磁盘占用；不同数据集不可直接横比。
- 清理：`oltp_common cleanup`（会删表，确认库是测试库）。

```bash
# 按 prepare 的实际表数生成显式表名（凭据文件选项必须排在其它客户端选项之前）
ANALYZE_SQL=""
for ((i = 1; i <= TABLES; i++)); do
  [ -n "${ANALYZE_SQL}" ] && ANALYZE_SQL+=","
  ANALYZE_SQL+="sbtest${i}"
done
mysql --defaults-extra-file="${CRED_FILE}" -u "${MYSQL_USER}" \
  --socket="${MYSQL_SOCKET}" -e "ANALYZE TABLE ${ANALYZE_SQL};" \
  "${TEST_DB}" || { echo "ANALYZE TABLE failed" >&2; exit 1; }
```

## 3. 预热

- 只读/Point：正式测前用 32 线程跑 `oltp_read_only` 约 600s，让 Buffer Pool 达到稳态。
- 读写/只写：先跑一段写负载让 redo/undo/脏页进入稳态，再开始计时。
- 每轮之间等 mysqld CPU、iowait 降下来；写场景必须等刷脏/checkpoint 结束。
- 预热数据不进入结果统计。

## 4. 并发选择

- 并发按**每 worker 核线程数**估算（单位：线程），不同部署形态差异显著，不能跨形态套同一比例；以下基线均标注形态与单位，最终以并发扫描找 TPS 平台与 P95 拐点为准。

| 形态（8U，worker 核数 8） | Point/RO | RW/WO |
| --- | --- | --- |
| 容器 8U32G | 28–40 线程（3.5–5 线程/核；按 0 号核是否兼 IRQ 调整） | 80–100 线程（10–12.5 线程/核） |
| 虚机 8U32G（virtio 等待更多） | 100 线程（12.5 线程/核） | 300 线程（37.5 线程/核） |

- 读写/只写因锁与 IO 等待需要比只读更高并发；大核数机型按每核线程数外推，单台客户端可能先瓶颈，需多客户端。
- 并发扫描：从小到大（如 8/16/32/64/128/256），找到 TPS 平台与 P95 拐点。

## 5. 命令模板

```
COMMON=(--db-driver=mysql --mysql-host="${CLIENT_TARGET}" --mysql-port="${MYSQL_PORT}"
  --mysql-user="${MYSQL_USER}" --mysql-db="${TEST_DB}"
  --tables="${TABLES}" --table_size="${TABLE_ROWS}"
  --report-interval=1 --percentile=95 --db-ps-mode=auto)

# Point
sysbench "${COMMON[@]}" oltp_read_only --time=120 --threads="${T}" \
  --range_selects=0 --skip-trx=1 run

# RO
sysbench "${COMMON[@]}" oltp_read_only --time=120 --threads="${T}" run

# RW
sysbench "${COMMON[@]}" oltp_read_write --time=120 --threads="${T}" run

# WO
sysbench "${COMMON[@]}" oltp_write_only --time=120 --threads="${T}" run
```

- 密码不要放 argv：sysbench 1.0.20 **没有** defaults-file 类客户端选项；本机用
  `--mysql-socket="${MYSQL_SOCKET}"` 指定实例，口令经受控环境变量（如 `MYSQL_PWD`，
  注意 `/proc` 可见性）注入；其他版本确需配置文件时先用 `sysbench --help` 核实选项。
- 多客户端：每个客户端连同一服务端，分别记录 TPS/CPU，汇总时相加。
- 同机 loopback 只适合功能验证，不适合网络/IRQ 收益结论。

## 6. 结果字段

sysbench 输出关键行：

```
transactions:  (per sec.)
queries:
Latency (ms, 95th):
errors:
```

记录：TPS、QPS、latency min/avg/max/P95（需要时 P99）、errors、时间戳、线程/时长。
同时采集：服务端 `pidstat -p $(pidof mysqld) 1`、`mpstat -P ALL 1`、`/proc/interrupts`
增量、`iostat -x 1`、客户端 `top`/`mpstat`。

## 7. 客户端瓶颈判别

| 证据 | 判断 |
| --- | --- |
| sysbench 进程 CPU ≥90%（按 cpuset/配额归一化后接近满载） | 客户端先瓶颈：加压测机/进程，减少结果集 |
| 客户端网卡接近线速/丢包 | 网络先瓶颈：多网卡/bond/换机 |
| 服务端业务核未满、IRQ 核满 | 中断瓶颈：调整 IRQ/队列/绑核 |
| 服务端业务核满、TPS 平台 | 服务端 CPU 瓶颈：进入热点分析 |
| 服务端 iowait 高 | IO 瓶颈：检查刷脏/盘/队列 |
| 错误率上升 | 锁/超时/连接数：先看失败样本 |

## 8. 容器/虚机注意事项

- 容器：cpuset 必须与 NUMA 一致；宿主 IRQ 不与业务核重叠；`--memory` 与 buffer pool 留余量。
- 虚机：virtio 波动大，多轮取均值；host 邻居负载隔离；vCPU steal 会污染结果。
- 两者都要在压测前固定 cpuset/IRQ/绑核，压测中不改。
- 写场景每轮等 IO 归零；容器/虚机刷脏通常比物理机慢。

## 9. 报告片段模板

以下为鲲鹏 920 + openEuler 22.03 + sysbench 1.0.20 的示例数值（仅示意格式，实测须替换）：

```
场景：RO，线程 64，时长 120s，3 轮
数据：64×1000w，预热 600s
结果：TPS 1234 / 1238 / 1230（均值 1234，波动 0.6%）
延迟：avg 51.9 ms，P95 68 ms，max 120 ms
CPU：mysqld 780%，IRQ 核 92%，客户端 18%
判定：基线稳定；候选 TPS 1247（+1.05%），P95 53.2 ms（-21.8%），保留
```

- 三量交叉自检（Little 定律，sysbench 闭链）：`avg 延迟(ms) ≈ 线程数 / TPS × 1000`。
  上例 64/1234×1000 ≈ 51.9 ms；若写 0.81 ms，对应 TPS 应约 7.9 万，不能与 64 线程
  /1234 TPS 同时成立；max 必须 ≥ avg（P95 可小于 avg，但须 ≤ max）。
