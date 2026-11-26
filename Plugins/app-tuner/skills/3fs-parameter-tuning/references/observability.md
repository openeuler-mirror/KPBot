# 观测与调参前后对比

> **何时读我**：要用 ClickHouse 指标证明"这次改参真的有效/无效"，或要判断瓶颈在客户端流控、服务端还是网络时读本文。本文面向**调参前后对比的轻量查询**（存活检查 → 时钟校正 → 加权均值/分位数 → 延迟拆分 → RDMA 带宽）。

**分工**：全链路写路径指标地图、每个指标的定义与采集点、火焰图/bpftrace 深度归因方法，属于 skill `3fs-hotspot-code-optimization`（其 SKILL.md 阶段 1f「写路径指标地图」）。本文只给"够用来做 A/B 判定"的查询，不重复那份地图。

节点角色与示例 IP 见 `SKILL.md`。

---

## 一、monitor_collector + ClickHouse 架构

`/opt/3fs/etc/monitor_collector_main.toml`（部署在 meta 节点，端口 10000）：

```toml
[server.monitor_collector]
batch_commit_size = 4096
conn_threads = 32
queue_capacity = 204800

[server.monitor_collector.reporter]
type = 'clickhouse'

[server.monitor_collector.reporter.clickhouse]
db = '3fs'
host = '127.0.0.1'
port = '9123'
user = 'default'
passwd = '<部署时注入, 不要写进仓库/命令行历史>'
```

各服务侧：`[common.monitor] collect_period='1s'`、`num_collectors=1`；`[[common.monitor.reporters]] type='monitor_collector'`、`remote_ip='<meta 节点 IP>:10000'`（storage/mgmtd 用 RDMA 网地址，示例 `203.0.119.10:10000`；`meta_main.toml` 用管理网地址，示例 `192.0.2.10:10000`）。

**查询前先确认采集服务存活** —— 曾因 `monitor_collector_main` 已死 **472 分钟**导致所有 A/B 查询返回空：

```bash
systemctl status monitor_collector_main clickhouse-server   # 或 clickhouse
ss -lntp | grep -E '10000|9123'
```

```bash
read -rs CK_PASS      # 密码只从 stdin 读
clickhouse-client --port 9123 --user default --password "$CK_PASS" --database 3fs
```

表：`` `3fs`.counters ``（TIMESTAMP, host, instance, metricName, val）与 `` `3fs`.distributions ``（..., count, mean, p50, p90, p95, p99, max）。

---

## 二、关键指标名

```
storage.req_write.{succ_latency,total,bytes,current}
storage.req_update.*          storage.reliable_update.*
storage.update_remote.succ_latency    storage.reliable_forward.succ_latency
storage.do_update.*           storage.do_commit.*
storage.engine_update.*       storage.engine_commit.*
storage.pwrite.succ_latency
storage.chunk_update.succ_latency     storage.chunk_commit.succ_latency
storage.wait_chunk_lock.succ_latency  storage.wait_channel_lock.succ_latency
storage.wait_post.succ_latency        storage.wait_sem.succ_latency
storage.update.queue_latency
storage.sync_start.succ_latency       storage.sync_done.succ_latency
storage.chunk_engine.{pwrite_latency,copy_on_write_latency,allocate_latency,
  copy_on_write_times,copy_on_write_read_times,copy_on_write_read_bytes,
  checksum_reuse,checksum_combine,checksum_recalculate,
  safe_write_direct_append,safe_write_indirect_append,
  safe_write_truncate_shorten,safe_write_truncate_extend,
  safe_write_read_tail_times,safe_write_read_tail_bytes,
  allocate_times,pwrite_times}
common.coroutines_num.current      (instance='UpdatePool')
fuse.iov.latency.{ib_reg,map}      fuse.iov.{bytes,bytes.ib_reg,times}
fuse.write.latency
usrbio.piov.{io_depth,total_bytes,io_size,submit,overall}
storage_client.{concurrent_user_calls,num_pending_ops,ops_per_user_call,
  requests_per_user_call,bytes_per_user_call,bytes_per_request,
  waiting_time,overall_latency,request_latency,server_latency,network_latency}
```

指标定义在源码 `src/storage/worker/CheckWorker.cc:50-69` 与 `:241-273`（1 秒采集周期）。

**调参 A/B 最常用的几个**：

| 目的 | 指标 |
|---|---|
| 写吞吐 | `storage.req_write.bytes`（counters） |
| 写延迟（端到端/引擎） | `storage.req_write.succ_latency`、`storage.engine_update.succ_latency`、`storage.do_update.succ_latency` |
| sync 相关改动（如 `sync_when_write`） | `storage.sync_start.succ_latency`、`storage.sync_done.succ_latency`、`storage.chunk_engine.pwrite_latency` |
| checksum 相关改动 | `storage.chunk_engine.checksum_reuse/checksum_combine/checksum_recalculate`、`copy_on_write_*` |
| 客户端流控改动（`traffic_control`、`num_bg_threads`） | `storage_client.waiting_time`、`concurrent_user_calls`、`num_pending_ops`、`usrbio.piov.io_depth` |
| 转发/副本拓扑改动 | `storage.reliable_forward.succ_latency`、`storage.update_remote.succ_latency` |
| 队列/协程是否打满 | `storage.update.queue_latency`、`common.coroutines_num.current`（instance='UpdatePool'） |

---

## 三、标准 SQL

```sql
WITH now() - INTERVAL 3 HOUR AS begin_time, now() AS end_time
SELECT host, metricName, sum(count),
       round(sum(mean*count)/greatest(sum(count),1), 2) AS weighted_mean,
       max(p50), max(p90), max(p99), max(max)
FROM `3fs`.distributions
WHERE TIMESTAMP BETWEEN begin_time AND end_time
  AND metricName IN ('storage.engine_update.succ_latency', 'storage.do_update.succ_latency')
GROUP BY host, metricName
FORMAT PrettyCompact;
```

聚合规则：

- **延迟**用 `sum(mean*count)/sum(count)` 加权均值，不要对 mean 取算术平均
- **吞吐**用 `sumIf(val, metricName='storage.req_write.bytes')` from counters
- **chunk_engine 计数器是每秒 `swap(0)`** → 用窗口内 `sum(val)`，**不是** max−min
- **gauge**（`*.current`、coroutines）→ min/avg/max

**A/B 对比模板**（把两腿的时间窗分开传，避免"3 小时窗口含多次测试与空闲期"的口径错误）：

```sql
-- 每条腿单独一个窗口: 用压测脚本打印的起止时间, 不要用 now() - INTERVAL 3 HOUR
SELECT '<腿名>' AS leg, host, metricName,
       sum(count) AS n,
       round(sum(mean*count)/greatest(sum(count),1), 2) AS weighted_mean_us,
       max(p99) AS p99_us
FROM `3fs`.distributions
WHERE TIMESTAMP BETWEEN '<BEG>' AND '<END>'
  AND metricName IN ('storage.req_write.succ_latency',
                     'storage.engine_update.succ_latency',
                     'storage.sync_done.succ_latency')
GROUP BY host, metricName
ORDER BY metricName, host
FORMAT PrettyCompact;
```

稳态吞吐要看**逐秒时间线**，不要看窗口总量除以窗口长度：

```sql
SELECT toStartOfSecond(TIMESTAMP) AS ts, host, sum(val) AS bytes_per_s
FROM `3fs`.counters
WHERE TIMESTAMP BETWEEN '<BEG>' AND '<END>' AND metricName = 'storage.req_write.bytes'
GROUP BY ts, host ORDER BY ts FORMAT PrettyCompact;
```

---

## 四、时钟偏移校正（必做）

storage 节点时钟比 ClickHouse 快约 **113 秒**；FUSE 节点偏移实测 **-136 秒**。不校正则 storage 侧查询返回空。

```bash
CK_NOW=$(clickhouse-client --port 9123 --user default --password "$CK_PASS" "SELECT toUnixTimestamp(now()) FORMAT TSVRaw")
CK_OFFSET=$((CK_NOW - $(date +%s)))
# FUSE 指标用本地时钟窗口 [T0-30s, T1+60s]
# storage 指标用校正后窗口 BEG_CK/END_CK（storage 时间戳跟随 META 时钟）
```

或直接在 SQL 里放容差：`end_time = now() + INTERVAL 5 MINUTE`。

**异常数据剔除**：B 集群 storage2 的 `do_update/do_commit/reliable_update/req_update` 出现 ~`7.47e12` 的百分位（时钟/recorder 时基异常）→ 该节点该指标整列排除，不参与结论。

> 时钟问题还会伪装成拓扑故障（target 集体 OFFLINE、rotate 失败），见 `topology-and-placement.md` §五。**查询返回空时，先怀疑时钟与采集服务存活，再怀疑"没有数据"。**

---

## 五、延迟拆分（定位等待发生在哪一层）

一次 1260 MiB/s 跑的实测拆分：

```
usrbio.piov.submit / overall   ≈ 748 ms
storage_client.overall_latency ≈ 617 ms
  ├─ waiting_time              ≈ 572 ms   ← 信号量/流控等待，在 RPC 之前
  ├─ request_latency           ≈  31 ms
  ├─ server_latency            ≈   4 ms
  └─ network_latency           ≈   0.09 ms
concurrent_user_calls max=32   num_pending_ops max=1024
io_depth max=8                 bytes_per_request max=1048576
```

`waiting_time` 572 ms vs `server_latency` 4 ms → **瓶颈在客户端流控/调度，不在服务端**。这解释了为什么单纯增大 fio `numjobs` 无收益（见 `lever-ranking.md` §2.1）。

并发模型核对：32 个 fio job × 每 job 一个 PioV in-flight × 每 PioV 8×4MiB=32MiB × 每 batchWrite 32 个 1MiB WriteIO → `concurrent_user_calls max ≈ 32`、`num_pending_ops max = 32×32 = 1024`。实测吻合。

**用法**：改动客户端并发类参数（`traffic_control.write`、`num_bg_threads`、fio `iodepth`）前后各拆一次，看 `waiting_time` 占比是否真的下降；改动服务端参数（`sync_when_write`、线程池）则看 `server_latency` 与 `storage.engine_*`。**拆分层级选错，就会把收益记错地方。**

---

## 六、RDMA 带宽监控

### 6.1 手工采样

`perfquery -x -C mlx5_0 -P 1` 取 `PortXmitData`/`PortRcvData`，1 秒采样，**MB/s = (delta × 4) / 1048576**（计数器单位是 4 字节字）。多节点用 ssh 并发跑采样脚本后 awk 汇总每节点 TX/RX 峰值与均值。需要 `infiniband-diags` 包。

无 `perfquery` 时降级读 `/sys/class/infiniband/<dev>/ports/<n>/counters/{port_xmit_data,port_rcv_data}`，同样 ×4 换算。

**峰值与均值必须分清**：峰值用于判"是否触顶"，均值用于判"稳态约束"。第一次分析用 TX 峰值 9299 MB/s 得出"74% 利用率"，改用均值 4825 MB/s 后节点角色解读随之改变（某节点 TX=0 说明它不是 MIDDLE，实际链路只有 2 跳）。

### 6.2 用 rdma_monitor.sh（`scripts/fio_bench.sh` 已可选集成）

`rdma_monitor.sh` 的真源在 skill `3fs-hotspot-code-optimization`；本 skill 不复制该文件，只按接口调用：

```bash
# 接口: start_rdma_monitor / stop_rdma_monitor <label>
# env : REMOTE_NODES(空格分隔的远程节点) / SSH_PASS / RDMA_MON_DEV(默认 mlx5_0) / RDMA_MON_PORT(默认 1)
RDMA_MON_LIB=/path/to/rdma_monitor.sh \
REMOTE_NODES="<storage1> <storage2>" \
RDMA_MON_DEV=mlx5_0 RDMA_MON_PORT=1 \
  bash scripts/fio_bench.sh seq-write
```

- 提供 `RDMA_MON_LIB` 时：每个测试自动 `start_rdma_monitor` → 跑 fio → `stop_rdma_monitor "<测试名>"`，输出每节点 TX/RX 的峰值与均值（MB/s）与采样秒数
- 未提供时：打印一行提示后跳过，压测照常进行（**不要因为没装监控就不跑压测**）
- 该库的远程采样通过 `SSH_PASS` 环境变量把密码传给 `sshpass`，请自行 `read -rs SSH_PASS && export SSH_PASS` 注入；本 skill 的脚本不设置任何密码默认值
- 远程采样会在目标节点 `/tmp` 下临时落一个采样脚本与日志，`stop_rdma_monitor` 会取回并清理

**判读**：HEAD 节点 RX 峰值接近 ~11369 MB/s（~91% 线速）说明网络已是约束，此时 CPU 侧/配置侧改动不会涨带宽（见 `topology-and-placement.md` §一）。

---

## 七、一次调参的观测清单（照抄即可）

```
[ ] 1. monitor_collector / clickhouse 存活, 端口在听
[ ] 2. 记录压测起止时间(本地时钟) + 各节点时钟偏移
[ ] 3. 改前: 跑压测 + 采集窗口 SQL(加权均值/p99/逐秒吞吐) + RDMA TX/RX 峰值与均值
[ ] 4. 记录 ConfigVersion 与二进制 md5
[ ] 5. 改参(一次一个变量) → 回读确认生效 → 必要的最小重启面
[ ] 6. 改后: 同口径重跑, 同一组 SQL, 同一组 RDMA 指标
[ ] 7. 延迟拆分对比: waiting_time / request_latency / server_latency / network_latency
[ ] 8. 结论写台账: 带宽、CPU-秒、压缩率、延迟分位数分开记, 附 ConfigVersion
```
