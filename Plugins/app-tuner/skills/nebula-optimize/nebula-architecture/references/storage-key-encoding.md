# RocksDB Key/值编码与存储布局

## 1. 为什么 Key 编码决定性能

RocksDB 是 LSM-Tree 有序 KV，只认字节字典序。Nebula 把图语义编进 Key，使“前缀扫描”
直接变成“读某顶点的所有 Tag/出边/索引项”。查询条件越具体，Key 前缀越长，扫描范围越小。

所有 Key 的前 4 字节统一为：

```
(partId << 8) | type      —— 4 字节整数，type 在最低字节
```

| type | 含义 |
|------|------|
| 0x01 | Tag（顶点属性） |
| 0x02 | Edge（边） |
| 0x03 | Index（索引） |
| 0x04 | System（系统元数据） |
| 0x07 | Vertex（顶点存在性标记） |
| 0x08 / 0x09 | TOSS Prime / DoublePrime 事务锁 |

同一分区的数据物理相邻，便于分区级扫描、快照和备份。

## 2. 五种 Key 布局

### 2.1 Tag Key

```
[type+part 4B] [vertexId vIdLen] [tagId 4B]
```

排序效果：同分区 → 同顶点所有 Tag 连续 → 按 tagId 排序。
一次前缀扫描可读出该顶点的全部 Tag。

### 2.2 Edge Key

```
[type+part 4B] [srcId vIdLen] [edgeType 4B] [rank 8B] [dstId vIdLen] [placeholder 1B]
```

排序效果：同源顶点的所有出边连续 → 按 edgeType → 按 rank → 按 dst。
`GO 1 STEP FROM src OVER type` 就是 `prefix(type|part|src|edgeType)` 扫描。
`OVER *` 则去掉 edgeType 前缀，扫描该顶点所有出边。

### 2.3 Index Key

```
[type+part 4B] [indexId 4B] [indexed values 变长] [vid] 或 [src+rank+dst]
```

排序效果：同索引 → 按属性值排序 → 同值按 VID 排序。
等值查询是 prefix scan；范围查询是 range scan。索引值在 VID 前面，所以相同属性值的记录
聚在一起。

### 2.4 System Key

```
[type+part 4B] [sysKeyType 4B]
```

如 commitLogID、partPeers 等。用于 Raft/WAL 与分区元数据。

### 2.5 Vertex Key

```
[type+part 4B] [vertexId vIdLen]
```

表示顶点存在性（无属性时也能判断顶点是否存在）。

## 3. 值编码：字典序 = 大小序

### INT64

```
v ^= (1 << 63)          // 翻转符号位
bytes = big_endian(v)   // 大端
```

编码后：最小负数 → `0x0000000000000000`；0 → `0x8000000000000000`；
最大正数 → `0xFFFFFFFFFFFFFFFF`。字节序与数值序一致。

### Double

- 正数：翻转符号位后大端。
- 负数：全部位取反后大端。
- NaN：8 字节全 `0xFF`。

### String

按索引定义长度截断，`\0` 补齐；天然保持字典序。

### Rank

与 INT64 相同：符号位翻转 + 大端。

## 4. 典型查找与 Key 构造

设 `vid_type=FIXED_STRING(16)`、`partition_num=10`。

| 查找 | 构造的 Key | RocksDB 操作 |
|------|-----------|--------------|
| `FETCH PROP ON player "Tim Duncan"` | 完整 Tag Key（含 tagId） | `Get` 精确 1 条 |
| `MATCH (n) WHERE id(n)=="Tim Duncan"` | Tag 前缀（不含 tagId） | prefix scan，读全部 Tag |
| `GO 1 STEP FROM "Tim Duncan" OVER like` | Edge 前缀（含 src + edgeType） | prefix scan，读出边 |
| `GO 1 STEP ... OVER *` | Edge 前缀（含 src，不含 edgeType） | prefix scan，读全部出边 |
| `LOOKUP WHERE age == 40` | Index 前缀 + `encInt64(40)` | prefix scan |
| `LOOKUP WHERE age > 35` | `[encInt64(36), 全 FF)` | range scan |
| 多跳遍历 | 每跳一次 Edge 前缀 | N 次 prefix scan + N 次 RPC |

## 5. 索引查找的完整转换

```
① graphd 优化器选索引，生成 IndexQueryContext
② storaged 从 MetaClient 缓存取索引元数据
③ 构建 IndexNode 执行树
④ WHERE → Key 范围
     age == 40  → prefix(indexId + encInt64(40))
     age > 35   → range(encInt64(36), 0xFF...FF)
⑤ RocksDB scan 得到 VID，再回表读 Tag 取属性
```

优化含义：

- 前缀越短，扫描越多；多列索引要按最左前缀使用。
- 索引列类型决定编码；字符串索引必须指定长度，否则无法定位。
- LOOKUP 会广播到所有分区；分区数越多，单次 LOOKUP 的并行扫描越多。
- 索引不是免费的：写入时多写一份 Index Key，compaction 压力与空间放大都会增加。

## 6. RocksDB 实例与 Space

- `NebulaStore::addSpace(spaceId)` 为每个 Space 在每个 `data_path` 下创建一个 `RocksEngine`，
  即一个独立的 `rocksdb::DB::Open`。
- 目录形态：每个 storaged 实例使用 `${DATA_ROOT}/storage${N}/nebula/<spaceId>/data`、`.../wal`；不存在单一的 `${DATA_ROOT}/storage`。
- `USE space` 只改 session 的 spaceId，不关闭/打开 RocksDB；所有 Space 实例保持打开。
- `DROP SPACE` 才关闭实例并删除目录。
- 多盘用 `--data_path=/disk1,/disk2`：每块盘一个 RocksEngine，分区按负载分配。
- `replica_factor` 是不同节点上的 Raft 副本，与单机多盘是两回事。

### Block Cache 共享

`RocksEngineConfig` 中 Block Cache 是进程级 `static` 对象，所有 Space 共享同一份 LRU：

```
static std::shared_ptr<rocksdb::Cache> blockCache =
    rocksdb::NewLRUCache(FLAGS_rocksdb_block_cache * 1024 * 1024);
```

因此 `--rocksdb_block_cache=32768` 表示整个 storaged 进程 32GB，不是每个 Space 32GB。
打开多个 Space 不会成倍占用 cache；切换 Space 时按 LRU 互相淘汰。

### 内存与落盘

| 项 | 配置 | 行为 |
|----|------|------|
| Block Cache | `rocksdb_block_cache` | 按需缓存被读过的 SST block，启动时接近 0 |
| MemTable | `write_buffer_size` × `max_write_buffer_number` | 有写入才分配，满后 flush |
| Bloom Filter | 按 SST 元数据 | 打开时加载少量元数据 |
| SST 句柄 | `max_open_files` | 只占文件描述符 |
| WAL | `wal_ttl` | 写入日志，重启恢复；只读场景可调小 |

停止进程时 RocksDB 会 flush MemTable、关闭句柄、释放 cache；磁盘上 SST/WAL 保留。
重启后 cache 从空开始，首次查询会触发磁盘读，因此性能测试必须先预热。

## 7. 写入放大与 compaction

一次顶点/边写入可能产生：

- 顶点写 1 份 Tag Key；边写入固定为正向出边 + 反向入边两份记录（一次普通 INSERT EDGE 即
  如此，不依赖业务是否写双向边）；
- N 份 Index Key（每个索引一份）；
- Raft WAL 日志；
- RocksDB MemTable → L0 → 更深层 SST 的多次重写。

因此写入性能不能只看 `rocksdb::DB::Write`，还要看：

- `WriteThread::AwaitState`（写线程等待）；
- `LZ4_compress_fast_continue` / `BlockBuilder`（compaction CPU）；
- 内核态 `fdatasync`/`fsync`（WAL 持久化）；
- compaction 与前台查询争抢 CPU/IO。

实测中 Insert 场景 storaged 的 compaction 可占约 13%，WAL fsync 相关内核态约 10%；
这类瓶颈通常不是靠改 Nebula 业务代码解决，而是调 WAL/compaction/磁盘或改变批量写入方式。

## 8. 常见误判

| 误判 | 事实 |
|------|------|
| “每个 Space 一份 Block Cache，开多个 Space 会爆内存” | 进程级共享一份 cache |
| “USE 切换 Space 会重新打开 RocksDB” | 只改 session 变量 |
| “LOOKUP 只查一个分区” | 不知道 VID，广播所有分区 |
| “多跳查询是一次 RPC 拿完” | 轮次串行；每轮内顶点按主机分组并行发多个 RPC |
| “边只存一份” | 正向出边 + 反向入边两份 |
| “索引只影响读” | 写入也要维护 Index Key，增加写放大 |
| “Block Cache 调大一定更快” | 超过 SST 实际大小后，LRU 管理开销可能反噬 |
