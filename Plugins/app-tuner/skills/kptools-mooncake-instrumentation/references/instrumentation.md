# MKT 打点规格（MKT_TTF / MKT_FINE / MKT_PUT / MKT_PFINE）

> 字段契约 + 函数级挂点。下游 `kptools-tpsplit-log-analysis` 的 MKT 解析按本契约 grep，
> **字段名、顺序、单位是硬契约，改动必须双方同步**。
>
> 两条链：**get 链（D 侧拉 KV）** → `[MKT_TTF]`（批次摘要）+ `[MKT_FINE]`（getmem 内部五相）；
> **put 链（P 侧存 KV）** → `[MKT_PUT]`（批次摘要）+ `[MKT_PFINE]`（BatchPut 内部相）。
> tag 不复用，P/D 混部时按 tag 无歧义区分。

## 1. 日志样例（契约基准）

glog INFO 级，输出到引擎进程 stdout（Ray 汇总到 head 日志）。

**get 链（实测样例）：**

```
I20261118 10:58:41.143564 719011 client_service.cpp:1219] [MKT_FINE] keys=49 open_ms=0.03536 build_ms=0.082311 alloc_ms=0.01996 submit_ms=2.24474 wait_ms=64.6071
I20261118 10:58:41.144507 719011 real_client.cpp:4783] [MKT_TTF] keys=49 mem=49 locdisk=0 disk=0 bytes=597295104 total_ms=68.8998 query_ms=0.693005 select_ms=0.1137 getmem_ms=67.9937 offload_ms=0 checksum_ms=0 diskio_ms=0 src_ep=7.150.0.11:15418 kv_addr=0x12f1c0c00000 dst_addr=0x12d8b68a0000 key=GLM-5.2-W4A8@pcp0@dcp0@head_or_tp_rank:0@pp_rank:0@group:0@cache_role:kv@cache_family:c1@013e... size=12189696
```

**put 链（设计格式串，待实测校准——字段名/顺序为契约，具体数值为示意）：**

```
I20261118 11:02:05.201443 707880 client_service.cpp:3715] [MKT_PFINE] keys=32 build_ms=0.1137 checksum_ms=1.8056 alloc_ms=3.2107 stage_ms=0.0221 submit_ms=2.4474 wait_ms=52.1071 dfs_ms=0 final_ms=0.8841
I20261118 11:02:05.203884 707880 real_client.cpp:5775] [MKT_PUT] keys=32 bytes=390070272 total_ms=61.1137 build_ms=0.2413 put_ms=60.7296 dst_ep=7.150.0.11:15418 kv_addr=0x12f1c0c00000 dst_addr=0x12d8b68a0000 key=GLM-5.2-W4A8@pcp0@dcp0@head_or_tp_rank:0@pp_rank:0@group:0@cache_role:kv@cache_family:c1@013e... size=12189696
```

## 2. MKT_TTF 字段契约（get 链批次摘要，real_client.cpp）

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

## 3. MKT_FINE 字段契约（get 链内部五相，client_service.cpp）

挂在内存路径 `ClientService` 侧批量传输函数（`RealClient` 调用的 `client_->BatchGet`）收尾处，与 MKT_TTF 同批次成对出现：

| 字段 | 类型/单位 | 语义 |
|---|---|---|
| `keys` | int | 本批 key 数（与 MKT_TTF 的 keys 一致，配对键） |
| `open_ms` | float/ms | 传输引擎会话/segment 打开 |
| `build_ms` | float/ms | 传输请求构建（batch/slice 组装） |
| `alloc_ms` | float/ms | 传输缓冲分配（如需要） |
| `submit_ms` | float/ms | 传输任务提交（RDMA submit） |
| `wait_ms` | float/ms | 等待传输完成（通常主导） |

## 4. MKT_PUT 字段契约（put 链批次摘要，real_client.cpp）

挂在 `RealClient` 的批量 put 入口收尾处（`batch_put_from` / `batch_put_from_multi_buffers` 两条入口——按 vLLM connector 实际调用的入口挂钩，同一进程只有一条会触发；不确定时两条都挂，格式相同）：

| 字段 | 类型/单位 | 语义 |
|---|---|---|
| `keys` | int | 本批 key 数（入参） |
| `bytes` | int | 本批写入总字节 |
| `total_ms` | float/ms | 批次整体墙钟 |
| `build_ms` | float/ms | 用户缓冲切 slice 与组装（`split_into_slices` 等）耗时 |
| `put_ms` | float/ms | `client_->BatchPut(keys, slices, config)` 调用总耗时（内含 MKT_PFINE 各相） |
| `dst_ep` | str | 采样传输的目标端点（KV 副本所在节点的 transfer engine 地址） |
| `kv_addr` | hex | 采样传输的本地 KV 源地址 |
| `dst_addr` | hex | 采样传输的远端目的地址 |
| `key` | str | 采样 key（批内首个，完整 Mooncake key 格式） |
| `size` | int | 采样 key 的字节数 |

## 5. MKT_PFINE 字段契约（put 链内部相，client_service.cpp）

挂在 `Client::BatchPut(keys, batched_slices, config)`（所有 put 变体的**单一收束点**，与入口选择无关）收尾处：

| 字段 | 类型/单位 | 语义 |
|---|---|---|
| `keys` | int | 配对键（与 MKT_PUT 一致） |
| `build_ms` | float/ms | `CreatePutOperations`（ops 构建） |
| `checksum_ms` | float/ms | `ComputeBatchObjectChecksums`（对象级校验和） |
| `alloc_ms` | float/ms | `StartBatchPut`（向 master 分配/注册副本位置，**put 特有**，含 master RPC） |
| `stage_ms` | float/ms | `StageWriteBuffersForRemoteReplicas`（远端副本写缓冲准备） |
| `submit_ms` | float/ms | `SubmitTransfers`（传输提交） |
| `wait_ms` | float/ms | `WaitForTransfers`（等待完成，通常主导） |
| `dfs_ms` | float/ms | `SubmitDfsWrites`（DFS 副本异步写，无 DFS 副本时 0） |
| `final_ms` | float/ms | `FinalizeBatchPut`（master 元数据提交） |

## 6. 内部一致性（解析器断言，也是打点自检）

**get 链：**
1. 等尺寸 block 时 `keys × size = bytes`（样例 49×12,189,696=597,295,104 ✓）
2. `getmem_ms ≈ FINE 五段之和 + 调度开销`，差值容差 `≤ max(1.5ms, 2% getmem_ms)`（绝对+相对双口径：小批次保绝对下限、大批次按比例放宽；样例 67.9937 vs 66.9895，差 1.004ms ≤ 1.5ms ✓）——FINE 是 TTF 中 getmem 的子阶段分解
3. 同批次两行相邻（<数 ms）、同线程 id、同 keys 值
4. 六相中非本批次路径的相为 0（内存路径批次 offload/checksum/diskio 均 0）
5. 有效带宽 ≈ `bytes / total_ms`（样例 ≈8.67 GB/s，可与网卡规格对账）

**put 链：**
1. 等尺寸 block 时 `keys × size = bytes`（样例 32×12,189,696=390,070,272 ✓）
2. `put_ms ≈ PFINE 各相之和 + 调度开销`，差值容差 `≤ max(1.5ms, 2% put_ms)`——PFINE 是 PUT 中 put_ms 的子阶段分解
3. `total_ms ≈ build_ms + put_ms`（real_client 层仅这两段），差值容差同口径 `≤ max(1.5ms, 2% total_ms)`（样例 61.1137 vs 60.9709，差 0.143ms ≤ 1.5ms ✓）
4. 同批次 PUT/PFINE 相邻、同线程 id、同 keys 值
5. 无 DFS 副本时 `dfs_ms=0`；`alloc_ms` 含 master RPC，是 put 与 get 的差异相（get 的 master 查询在 query_ms）
6. 有效带宽 ≈ `bytes / total_ms`

## 7. 函数级挂点（按调用链定位，行号随版本漂移，禁止按行号找）

**get 链**（以 v0.3.14 结构核对，源码 `mooncake-store/src/`）：

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

**put 链**：

```
RealClient::batch_put_from(_multi_buffers)          ← MKT_PUT 挂点（按实际入口，可双挂）
  ├─ split_into_slices(buffer, size)                 ← build_ms
  └─ client_->BatchPut(keys, slices, config)        ← put_ms
       └─ Client::BatchPut                          ← MKT_PFINE 挂点（单一收束点）
            CreatePutOperations                      ← build_ms
            ComputeBatchObjectChecksums              ← checksum_ms
            StartBatchPut                            ← alloc_ms（master 副本分配）
            StageWriteBuffersForRemoteReplicas       ← stage_ms
            SubmitTransfers                          ← submit_ms
            WaitForTransfers                         ← wait_ms
            SubmitDfsWrites                          ← dfs_ms
            FinalizeBatchPut                         ← final_ms
```

定位方法（新版本适配时执行）：
1. get 链：在 `real_client.cpp` 搜 `client_->BatchQuery(keys)` 与 `SelectBestReplica` 圈定六相边界；搜 `client_->BatchGet(batch_keys` 圈定 getmem_ms 与 MKT_FINE 宿主；
2. put 链：在 `real_client.cpp` 搜 `batch_put_from`（含 `_internal` / `_multi_buffers` 变体）圈定 MKT_PUT 宿主；在 `client_service.cpp` 搜 `Client::BatchPut(`（接收 batched_slices 的重载）圈定 MKT_PFINE 宿主，其内部按上列八个调用逐段计时；
3. 采样字段：put 链从首个 key 的传输任务描述取 dst_ep 与源/目的地址（方向与 get 相反：dst_ep 为远端）。

计时实现纪律：
- 用单调时钟（`std::chrono::steady_clock`），不用系统时钟；
- 各相只计自身区间，异常提前返回也要把已耗相位打出（或打 `partial=1`），禁止吞行；
- 打点用 `LOG(INFO)` + `[MKT_TTF]` / `[MKT_FINE]` / `[MKT_PUT]` / `[MKT_PFINE]` 前缀，格式串固定拼接，保持 glog 原生格式。

## 8. 版本锚定记录

> **锚定策略（已决策：运行时现场锚定）**——不维护固定版本 diff；每次执行由 skill 的探测步骤
> 现场锚定现役版本，按第 7 节定位法现场适配打点。下表 ⏳ 项表示"每次运行时填充"，
> 不是待办欠账。仅当某版本被多次重复打点、值得沉淀固定 patch 时，才把 diff 登记到本表。

| 项 | 值 | 状态 |
|---|---|---|
| 参考核对版本 | v0.3.14（gitee tag，调用链已核对） | ✅ get 链已核对 |
| 参考核对版本 | v0.3.14（`Client::BatchPut` 3669-3715 已核对） | ✅ put 链已核对 |
| 集群现役版本 | 每次运行时由 probe_mooncake.sh 现场锚定 | 🔄 运行时锚定 |
| put 链实测样例 | 首次部署后采集回填第 1 节 | ⏳ 待实测 |

> 后续适配新版本时，先按第 7 节定位方法重新核对挂点，再增量改 patch。
