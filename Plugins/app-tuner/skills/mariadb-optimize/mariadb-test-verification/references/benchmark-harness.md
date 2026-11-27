# 压测工具、场景与指标解析

本文件给出 sysbench 与 TPC-C 的可复现参数、绑核建议、日志解析与客户端瓶颈判断。

## 1. 通用准备

- 压测机与被测机分离，压测机单独绑核，避免与数据库抢 CPU。
- 采集窗口：`warmup`（不计入）+ `run`（计入）；读场景必须预热。
- 每轮前重置数据到同一快照；记录重置方式与耗时。
- 记录：连接数、并发、表数/行数、warehouse、绑核、二进制 md5、关键参数。

## 2. sysbench

### 2.1 场景

| lua | 语义 | 典型用途 |
| --- | --- | --- |
| `oltp_read_only.lua` | 只读事务（点查+范围+聚合） | 读吞吐、buffer pool 命中 |
| `oltp_point_select.lua` | 单行主键/唯一索引 | 连接/线程/latch 极限 |
| `oltp_write_only.lua` | 插入/更新/删除 | redo、双写、刷脏、binlog |
| `oltp_update_non_index.lua` | 非索引列更新 | 扫描、行锁、purge |
| `oltp_update_index.lua` | 索引列更新 | 二级索引维护 |
| `oltp_read_write.lua` | 混合 | 综合 OLTP |
| `oltp_insert.lua` | 纯插入 | 顺序写、页分裂 |

### 2.2 命令

```
SYSBENCH_LUA_DIR="${SYSBENCH_LUA_DIR:-/usr/share/sysbench}"
# sysbench 的选项表没有 --defaults-extra-file（传给它会 invalid option 直接 exit 1）；
# 口令改经 sysbench 自身的 --config-file（key=value、无 [section]）注入，不进 argv/脚本
umask 077
PWFILE="$(mktemp)"
SYSBENCH_CONF="$(mktemp)"
trap 'rm -f "${PWFILE}" "${SYSBENCH_CONF}"' EXIT
printf '%s\n' "${DB_PASSWORD}" > "${PWFILE}"
printf 'mysql-password=%s\n' "$(cat "${PWFILE}")" > "${SYSBENCH_CONF}"
COMMON=(--config-file="${SYSBENCH_CONF}" \
        --mysql-host="${DB_HOST}" --mysql-port="${PORT}" \
        --mysql-user=bench \
        --mysql-db=bench --db-driver=mysql \
        --tables=10 --table-size=10000000)

# prepare
sysbench "${SYSBENCH_LUA_DIR}/oltp_common.lua" "${COMMON[@]}" prepare

# 预热（可选）
sysbench "${SYSBENCH_LUA_DIR}/oltp_read_only.lua" "${COMMON[@]}" \
  --threads=64 --time=300 --report-interval=10 run

# 正式：读（--percentile=99 让汇总直接给出整轮 p99）
sysbench "${SYSBENCH_LUA_DIR}/oltp_read_only.lua" "${COMMON[@]}" \
  --threads=128 --time=600 --report-interval=10 --rand-type=uniform --percentile=99 run | tee ro.log

# 正式：点查
sysbench "${SYSBENCH_LUA_DIR}/oltp_point_select.lua" "${COMMON[@]}" \
  --threads=256 --time=600 --report-interval=10 --percentile=99 run | tee ps.log

# 正式：只写
sysbench "${SYSBENCH_LUA_DIR}/oltp_write_only.lua" "${COMMON[@]}" \
  --threads=64 --time=600 --report-interval=10 --percentile=99 run | tee wo.log

# 清理
sysbench "${SYSBENCH_LUA_DIR}/oltp_common.lua" "${COMMON[@]}" cleanup
```

> `--config-file` 是 sysbench 1.0.x 的选项；若构建不支持（`sysbench --help | grep
> config-file` 无输出），改用专用低权限压测用户 + `--mysql-password`（会出现在 `ps`
> argv，压测后轮换口令）或 unix_socket 认证。不要把 `--defaults-extra-file` 传给 sysbench；
> 该选项只对 mariadb/obclient 等客户端有效（见 `functional-verification.md` 的用法）。

参数说明：

- `--threads`：并发连接/线程，必须扫描（64/128/256/512）找拐点，而不是只测一个点。
- `--rand-type`：`uniform`/`gaussian`/`pareto` 影响热点分布；报告必须写明。
- `--report-interval`：按固定间隔输出中间结果便于看抖动；区间分位通常只有 p95，
  **不能**拼成整轮 p99，整轮 p99 取 `--percentile=99` 的汇总值。
- `--time`：固定时长比固定事务数更可比。
- `--tables`/`--table-size`：决定工作集是否超过 buffer pool；报告必须写明。

### 2.3 日志解析

sysbench 末尾输出示例：

```
SQL statistics:
    queries performed:
        read:                            12345678
        write:                           0
        other:                           0
        total:                           12345678
    transactions:                        617283  (1028.80 per sec.)
    queries:                             12345678 (20576.13 per sec.)
    ignored errors:                      0      (0.00 per sec.)
    reconnects:                          0      (0.00 per sec.)

General statistics:
    total time:                          600.0123s
    total number of events:              617283

Latency (ms):
         min:                                    0.42
         avg:                                    6.23
         max:                                  188.70
         95th percentile:                        9.85
         sum:                              3845678.12
```

取数：

- QPS = `queries per sec`（多进程/多客户端要相加）。
- TPS = `transactions per sec`。
- p99：正式压测必须加 `--percentile=99`（汇总里直接给 99th percentile），或保留延迟
  直方图后离线计算。**不要用 `--report-interval` 的区间 p95 反推整轮 p99**：各窗口 p95
  无法还原全程分布。若日志只有区间 p95，报告中把 p99 标为“缺失（仅有区间 p95）”，
  不得填估算值或拿 p95 冒充。
- 错误率 = `ignored errors` / `total`；读场景应为 0。

## 3. TPC-C（TPCCRunner）

### 3.1 五类事务与占比

```
New-Order     : 1
Payment       : 1
Order-Status  : 45
Delivery      : 43
Stock-Level   : 10
思考时间全部为 0
```

> 上表是家族测试方案的偏斜口径（Order-Status/Delivery 为主，New-Order/Payment 各 1%），
> 不是 TPC-C 标准规格的 45/43/4/4/4；字段名与数值按测试方案原样保留，两腿必须一致，
> 不要把两种口径混比。

### 3.2 执行顺序

```
create_database.sql → create_table.sql → Loader → create_index.sql → Master + Slave(s)
```

- `loader.properties`：url/user/password 指向被测库。
- `master.properties`：`warmupMinutes`/`runMinutes`、事务占比、slaves 列表。
- `slave*.properties`：url/user/password、`poolSize`、`userCount`、
`startWarehouseID`/`warehouseCount`。
- 多 slave 用不同 warehouse 区间，减少同一行冲突；`userCount ≤ poolSize`。

### 3.3 结果解析

- 看 `tpmC`/TPS、事务成功率、各类事务延迟。
- 失败事务要区分：业务冲突（可重试）vs 连接/超时/死锁。
- 记录 slave 数（1/2/4）与 poolSize/userCount 组合，否则不可比。

## 4. 绑核与拓扑

```
# 数据库绑核（示例：数据库在前 64 核）
# 注意：numactl 不能套在 systemctl 客户端上——它只作用于瞬时的 systemctl 进程，
# mariadbd 是 systemd 拉起的子进程，绑核无效。正确做法：
# 1) 直接用 numactl 启动守护进程（与 #436 topology-and-config.md §2 一致）：
numactl --cpunodebind=0 --membind=0 -- "${MARIADB_HOME}/bin/mariadbd" --defaults-file=/etc/my.cnf
# 2) 或改 systemd unit：CPUAffinity=0-63 + NUMAPolicy=bind + NUMAMask=0，
#    再 daemon-reload + restart（NUMAPolicy 只收策略名；节点列表必须用 NUMAMask）

# 压测机绑核（示例：sysbench 独占另一段核）
taskset -c 64-127 sysbench ... run

# 中断绑核（网卡队列 → 数据库同 NUMA 的核）
grep -E 'eth|mlx' /proc/interrupts | head
```

原则：

- 数据库与压测端不共享核。
- 网卡中断与数据库同 NUMA，减少跨片。
- 多实例时每个实例绑定独立连续核段，不跨 NUMA。
## 5. 指标采集

采集器必须**后台并行覆盖正式负载窗口**：先取计数基线，再启动采集器，随后前台跑压测，
压测结束 `wait` 采集器收尾，最后取第二份计数。不能让 `pidstat`/`mpstat`/`iostat`/`sar`
在前台各跑满 60 秒——那样等到 `status_before` 和压测开始时它们早已退出，采集窗口与负载
窗口完全错开。`RUN_SECS` 设为与正式 `sysbench --time` 一致。

```bash
RUN_SECS=600   # 与正式 run 窗口一致

# 1) 计数基线（窗口起点：warmup 之后、正式负载之前）
mariadb -e "SHOW GLOBAL STATUS" > status_before.txt

# 2) 采集器后台启动，覆盖整个正式负载窗口
pidstat -p "$(pgrep -x mariadbd | tr '\n' ',')" 1 "${RUN_SECS}" > pidstat.txt &
P_PID=$!
mpstat -P ALL 1 "${RUN_SECS}" > mpstat.txt &
M_PID=$!
iostat -x 1 "${RUN_SECS}" > iostat.txt &
I_PID=$!
# 关注：r/s w/s rkB/s wkB/s await %util aqu-sz
sar -n DEV 1 "${RUN_SECS}" > net.txt &
S_PID=$!

# 3) 正式负载（前台，与采集器并行）
sysbench "${SYSBENCH_LUA_DIR}/oltp_read_only.lua" "${COMMON[@]}" \
  --threads=128 --time="${RUN_SECS}" --report-interval=10 --percentile=99 run | tee ro.log

# 4) 等采集器收尾，再取第二份计数（窗口终点）
wait "${P_PID}" "${M_PID}" "${I_PID}" "${S_PID}"
mariadb -e "SHOW GLOBAL STATUS" > status_after.txt
# 计数器增量 = status_after - status_before（仅窗口内增量，不是时点值）
```

时序纪律：warmup 结束 → `status_before` → 采集器 `&` → 前台 `run` → `wait` 采集器 →
`status_after`。采集时长必须 ≥ 负载时长，否则窗口仍有缺口。

重点计数器：

| 计数器 | 含义 |
| --- | --- |
| `Queries`/`Questions` | 语句量 |
| `Com_commit`/`Com_rollback` | 事务量 |
| `Innodb_buffer_pool_read_requests` | 逻辑读 |
| `Innodb_buffer_pool_reads` | 物理读（未命中） |
| `Innodb_rows_read/inserted/updated/deleted` | 行操作 |
| `Innodb_log_waits` | redo 空间等待（写抖动信号） |
| `Innodb_row_lock_waits/time` | 行锁等待 |
| `Threads_running`/`Threads_connected` | 并发 |
| `Created_tmp_disk_tables` | 磁盘临时表 |
| `Handler_read_rnd_next` | 全表/无序扫描 |

## 6. 客户端瓶颈判断

| 信号 | 判断 |
| --- | --- |
| 服务端 CPU < <数值>%，QPS 不再增长 | 客户端/网络先瓶颈 |
| 压测进程 CPU <数值>% | 加压测进程/核，或减小结果集 |
| 网络带宽接近上限 | 减少返回列/行数，或换更大带宽 |
| `Threads_running` 高但 `Queries` 不涨 | 锁/IO 等待，转 hotspot |
| 延迟高但服务端 CPU 低 | 客户端序列化/GC/连接池排队 |

## 7. 结果记录模板

```
场景：oltp_read_only
数据：10 表 × 1000 万行，工作集 ~X GB
参数：threads=128, time=600, rand-type=uniform
绑定：DB cpu 0-63 / client cpu 64-127
结果：QPS=..., TPS=..., p95=..., p99=...（--percentile=99 或直方图；仅有区间 p95 则标缺失）, errors=0
资源：DB CPU <数值>%（user <数值>/kernel <数值>），iostat util <数值>%，await <延迟数值>
备注：warmup 300s，数据已重置，PFS 关闭
```

## 8. 指标口径速查

| 指标 | 含义 | 注意 |
| --- | --- | --- |
| TPS/QPS | 每秒事务/语句数 | 解析见 §2.3；TPC-C 用 tpmC 或 TPS；多 slave 要相加 |
| p50/p95/p99 | 延迟分位 | 均值掩盖长尾；p99 须由 `--percentile=99` 或保留的延迟直方图得出，仅有区间 p95 时标缺失、不得反推（§2.3） |
| 错误率 | 失败事务/总事务 | 读场景应接近 0；写冲突要区分业务与故障 |
| CPU% | 进程/整机 | 结合 `pidstat` 看用户态/内核态（§5） |
| IO | IOPS/吞吐/await/util | `iostat -x`，注意队列深度与写放大（§5） |
| 锁等待 | `Innodb_row_lock_waits/time` | 高则看 `INNODB_TRX`/`LOCK_WAITS`（§5 重点计数器） |
| 刷脏 | `Innodb_buffer_pool_pages_dirty`、`Innodb_log_waits` | 写入抖动的常见根因（§5） |

`mariadb` 客户端的凭据走 `--defaults-extra-file`（0600）；sysbench 的选项表没有该项，
改用其自身的 `--config-file`（见 §2.2），两者不可互换。
