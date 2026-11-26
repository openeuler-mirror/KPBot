# MKT 打点规格（MKT_TTF / MKT_FINE）

> 字段契约 + 函数级挂点。下游 `kptools-tpsplit-log-analysis` 的 MKT 解析按本契约 grep，
> **字段名、顺序、单位是硬契约，改动必须双方同步**。

## 1. 日志样例（契约基准）

glog INFO 级，输出到引擎进程 stdout（Ray 汇总到 head 日志）：

```
I20261118 10:58:41.143564 719011 client_service.cpp:1219] [MKT_FINE] keys=49 open_ms=0.03536 build_ms=0.082311 alloc_ms=0.01996 submit_ms=2.24474 wait_ms=64.6071
I20261118 10:58:41.144507 719011 real_client.cpp:4783] [MKT_TTF] keys=49 mem=49 locdisk=0 disk=0 bytes=597295104 total_ms=68.8998 query_ms=0.693005 select_ms=0.1137 getmem_ms=67.9937 offload_ms=0 checksum_ms=0 diskio_ms=0 src_ep=7.150.0.11:15418 kv_addr=0x12f1c0c00000 dst_addr=0x12d8b68a0000 key=GLM-5.2-W4A8@pcp0@dcp0@head_or_tp_rank:0@pp_rank:0@group:0@cache_role:kv@cache_family:c1@013e... size=12189696
```

## 2. MKT_TTF 字段契约（real_client.cpp，批次级六相）

挂在 `RealClient::BatchGet(keys, buffers, sizes)`（对外批量 get）的收尾处，一次调用打一行：

| 字段 | 类型/单位 | 语义 |
|---|---|---|
| `keys` | int | 本批 key 数（入参） |
| `mem` | int | 命中 MEMORY/NOF 副本（远端内存，RDMA 路径）的 key 数 |
| `locdisk` | int | 命中 LOCAL_DISK 副本数 |
| `disk` | int | 命中 DISK/DFS 副本数 |
| `bytes` | int | 本批传输总字节 |
| `total_ms` | float/ms | BatchGet 整体墙钟 |
| `query_ms` | float/ms | 元数据查询（`client_->BatchQuery(keys)`）耗时 |
| `select_ms` | float/ms | 副本选择循环（含 `SelectBestReplica` 与请求组装）耗时 |
| `getmem_ms` | float/ms | 内存路径 `client_->BatchGet(batch_keys, ...)` 耗时 |
| `offload_ms` | float/ms | LOCAL_DISK offload RPC 路径耗时 |
| `checksum_ms` | float/ms | DISK/DFS 数据校验耗时 |
| `diskio_ms` | float/ms | DISK/DFS 读盘耗时 |
| `src_ep` | str | 采样传输的源端点（如 `7.150.0.11:15418`，取首个内存副本传输任务的 source endpoint） |
| `kv_addr` | hex | 采样传输的远端源地址（source address） |
| `dst_addr` | hex | 采样传输的本地目的地址（destination buffer） |
| `key` | str | 采样 key（批内首个内存路径 key，完整 Mooncake key 格式） |
| `size` | int | 采样 key 的单 block 字节数 |

打点时机：六相耗时在函数内分阶段计时累加；采样字段（src_ep/kv_addr/dst_addr/key/size）取首个内存路径 key 对应的传输任务描述。无内存路径批次（全 disk 命中）时采样字段可省略或打 `na`，但 `mem/locdisk/disk` 计数必须准确。

## 3. MKT_FINE 字段契约（client_service.cpp，getmem 内部五相）

挂在内存路径 `ClientService` 侧批量传输函数（`RealClient` 调用的 `client_->BatchGet`）收尾处，与 MKT_TTF 同批次成对出现：

| 字段 | 类型/单位 | 语义 |
|---|---|---|
| `keys` | int | 本批 key 数（与 MKT_TTF 的 keys 一致，配对键） |
| `open_ms` | float/ms | 传输引擎会话/segment 打开 |
| `build_ms` | float/ms | 传输请求构建（batch/slice 组装） |
| `alloc_ms` | float/ms | 传输缓冲分配（如需要） |
| `submit_ms` | float/ms | 传输任务提交（RDMA submit） |
| `wait_ms` | float/ms | 等待传输完成（通常主导） |

## 4. 内部一致性（解析器断言，也是打点自检）

1. 等尺寸 block 时 `keys × size = bytes`（样例 49×12,189,696=597,295,104 ✓）
2. `getmem_ms ≈ FINE 五段之和 + <1ms` 调度开销（样例 67.99 vs 66.99 ✓）——FINE 是 TTF 中 getmem 的子阶段分解
3. 同批次两行相邻（<数 ms）、同线程 id、同 keys 值
4. 六相中非本批次路径的相为 0（内存路径批次 offload/checksum/diskio 均 0）
5. 有效带宽 ≈ `bytes / total_ms`（样例 ≈8.67 GB/s，可与网卡规格对账）

## 5. 函数级挂点（按调用链定位，行号随版本漂移，禁止按行号找）

调用链（以 v0.3.14 结构核对，源码 `mooncake-store/src/`）：

```
RealClient::BatchGet(keys, buffers, sizes)          ← MKT_TTF 挂点
  ├─ client_->BatchQuery(keys)                       ← query_ms
  ├─ for each key: SelectBestReplica(...) + 组装      ← select_ms（循环整体计时）
  │    按 replica 类型分流计数 mem/locdisk/disk
  ├─ client_->BatchGet(batch_keys, qrs, slices)     ← getmem_ms（内存/NOF 路径）
  │    └─ ClientService 批量传输                     ← MKT_FINE 挂点
  │         open → build → alloc → submit → wait     ← 五相计时
  ├─ LOCAL_DISK offload RPC 路径                      ← offload_ms
  └─ DISK/DFS 读盘 + 校验路径                          ← checksum_ms / diskio_ms
```

定位方法（新版本适配时执行）：
1. 在 `real_client.cpp` 搜 `client_->BatchQuery(keys)` 与 `SelectBestReplica` ——圈定 MKT_TTF 的六相边界；
2. 搜 `client_->BatchGet(batch_keys` ——圈定 getmem_ms 与 MKT_FINE 的宿主函数；
3. MKT_FINE 宿主在 `client_service.cpp`（或新版本等价文件），按传输引擎调用序列（open/build/alloc/submit/wait 对应的 API 调用）分段计时；
4. 采样字段：从首个内存路径 key 的传输任务描述里取 source endpoint 与源/目的地址。

计时实现纪律：
- 用单调时钟（`std::chrono::steady_clock`），不用系统时钟；
- 各相只计自身区间，异常提前返回也要把已耗相位打出（或打 `partial=1`），禁止吞行；
- 打点用 `LOG(INFO)` + `[MKT_TTF]` / `[MKT_FINE]` 前缀，格式串固定 `%s` 拼接，保持 glog 原生格式。

## 6. 版本锚定记录

| 项 | 值 | 状态 |
|---|---|---|
| 集群现役版本 | 待探测（probe_mooncake.sh 输出） | ⏳ 未锚定 |
| 打点 patch 基线源码 | 待定（探测后 checkout 对应 tag） | ⏳ 未锚定 |
| 参考核对版本 | v0.3.14（gitee tag，调用链已核对） | ✅ 调用链与字段对齐 |

> 锚定后在本表登记：现役版本号 / git commit / 打点 diff 摘要 / 编译产物清单（含 md5）。
> 后续适配新版本时，先按第 5 节定位方法重新核对挂点，再增量改 patch。
