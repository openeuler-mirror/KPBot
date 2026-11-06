# 已知优化手段 Playbook

本文件收录"命中条件时可尝试"的具体优化手段。每条都给出:

- **命中条件**:什么症状/负载下值得尝试
- **参数**:确切选项名与默认值
- **配置写法**:options 文件、C++、Java
- **生效范围**:对已有数据是否生效
- **代价与风险**
- **验证指标**
- **源码依据**

原则不变:一次只改一个,先预测再复测。没有证据的改动标为"待验证假设"。

---

## 1. SST 读:用 prefix hash index 替代默认二分查找

### 命中条件

- 读密集(点查 / 前缀 seek 为主)
- key 有**可提取的公共前缀**(固定长度,或长度有上限)
- 延迟主要花在 SST 的 index block 查找上(`DB_GET` / `DB_SEEK` P99 高,`SST_READ_MICROS` 高)

### 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `BlockBasedTableOptions::index_type` | `kBinarySearch` | 设为 `kHashSearch` 启用前缀 hash index |
| `ColumnFamilyOptions::prefix_extractor` | `nullptr` | **必须设置**,否则 `ValidateOptions` 直接报 `InvalidArgument` |
| `index_block_restart_interval` | 16 | `kHashSearch` 下会被强制设为 1 |

### 配置写法

options 文件 / `GetColumnFamilyOptionsFromString` 字符串:

```text
prefix_extractor=fixed:8;
block_based_table_factory={index_type=kHashSearch;};
```

需要同时开 SST bloom 时:

```text
prefix_extractor=fixed:8;
block_based_table_factory={index_type=kHashSearch;filter_policy=bloomfilter:10:false;whole_key_filtering=1;};
```

- `fixed:N`:固定 N 字节前缀;`capped:N`:最多 N 字节前缀。
- `bloomfilter:10:false` = 10 bits/key、full filter(非 block-based)。

C++:

```cpp
rocksdb::ColumnFamilyOptions cf;
cf.prefix_extractor.reset(rocksdb::NewFixedPrefixTransform(8));  // 或 NewCappedPrefixTransform(8)

rocksdb::BlockBasedTableOptions table_options;
table_options.index_type = rocksdb::BlockBasedTableOptions::kHashSearch;
// 可选:
// table_options.filter_policy.reset(rocksdb::NewBloomFilterPolicy(10, false));
cf.table_factory.reset(rocksdb::NewBlockBasedTableFactory(table_options));
```

Java(RocksJava 6.26):

```java
Options options = new Options();
options.useFixedLengthPrefixExtractor(8);          // 或 useCappedPrefixExtractor(8)

BlockBasedTableConfig tableConfig = new BlockBasedTableConfig();
tableConfig.setIndexType(IndexType.kHashSearch);
tableConfig.setFilterPolicy(new BloomFilter(10, false));  // 可选
tableConfig.setWholeKeyFiltering(true);                    // 可选
options.setTableFormatConfig(tableConfig);
```

读侧要配合前缀 seek:

```cpp
rocksdb::ReadOptions ro;
ro.prefix_same_as_start = true;
// 不要设置 total_order_seek = true
```

Java:`readOptions.setPrefixSameAsStart(true)`。

### 生效范围(关键)

- `kHashSearch` **只对之后新写入的 SST 生效**;已有 SST 仍用旧的 index。
- 如果 SST 的 `prefix_extractor_name` 与当前配置不一致,reader 会**自动禁用该表的 hash index**
  (源码:`PrefixExtractorChanged`)。
- 因此要让存量数据也享受收益,需要**重写 SST**(compaction / `CompactRange`)。这会大量读写 IO,
  必须用户授权、先备份/确认窗口,并作为独立轮次复测。
- `total_order_seek=true` 会绕过 prefix index;点查/前缀 seek 场景应使用 `prefix_same_as_start=true`。

### 代价与风险

- index block 可能变大,内存/磁盘占用变化。
- 前缀提取有少量写 CPU 开销。
- key 没有有意义前缀(随机 key)时收益很小,甚至因 index 变大而变差。
- `kHashSearch` 与 `index_block_restart_interval > 1` 不兼容,后者会被强制为 1。

### 验证指标

- `DB_GET` / `DB_SEEK` 直方图 P50/P99
- `SST_READ_MICROS`、`READ_BLOCK_GET_MICROS`
- `BLOCK_CACHE_HIT/MISS`、perf_context `block_read_count`
- `sst_dump --show_properties` 检查 `prefix_extractor_name`(不是 `nullptr`)
- 复测必须同 key 分布、同并发、同数据量

### 源码依据(v6.26.1)

- `include/rocksdb/table.h:186-209`:`IndexType`、`kHashSearch` 注释、默认 `kBinarySearch`
- `table/block_based/block_based_table_factory.cc:517-523`:`kHashSearch` 缺少 `prefix_extractor` 时报错
- `table/block_based/block_based_table_factory.cc:471-476`:`index_block_restart_interval` 强制为 1
- `table/block_based/block_based_table_reader.cc:124-136`:`PrefixExtractorChanged` 时禁用 hash index
- `table/block_based/block.cc:1027-1028`:`total_order_seek` 时绕过 `prefix_index`
- `options/options_settable_test.cc:166`、`options/options_test.cc:454`:`index_type=kHashSearch` / `prefix_extractor=capped:8` 的配置写法

---

## 2. Memtable 读:开启 prefix bloom filter

### 命中条件

- 已设置 `prefix_extractor`
- 读密集、点查为主,且读会落在 memtable(未 flush 的数据)
- `DB_GET` P99 高,perf_context 显示 `get_from_memtable_time` 占比高

### 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `ColumnFamilyOptions::memtable_prefix_bloom_size_ratio` | `0.0` | > 0 才启用;bloom 大小 = `write_buffer_size * ratio`;> 0.25 会被截断到 0.25 |
| `ColumnFamilyOptions::memtable_whole_key_filtering` | `false` | 仅在 ratio != 0 时生效;按整 key 做 bloom,点查可降低 CPU |
| `prefix_extractor` | `nullptr` | 必须设置(或用 whole-key filtering) |

两者都标注为 **Dynamically changeable through `SetOptions()`**。

### 配置写法

```text
prefix_extractor=fixed:8;
memtable_prefix_bloom_size_ratio=0.01;
memtable_whole_key_filtering=true;
```

C++:

```cpp
cf.prefix_extractor.reset(rocksdb::NewFixedPrefixTransform(8));
cf.memtable_prefix_bloom_size_ratio = 0.01;   // 0 < r <= 0.25
cf.memtable_whole_key_filtering = true;        // 可选
```

Java(RocksJava 6.26):

```java
options.useFixedLengthPrefixExtractor(8);
options.setMemtablePrefixBloomSizeRatio(0.01);
// 注意:RocksJava 6.26 未暴露 setMemtableWholeKeyFiltering,
// 需要 whole-key memtable bloom 时用 C++ 侧或核对更高版本绑定。
```

动态修改:

```cpp
db->SetOptions(cf, {{"memtable_prefix_bloom_size_ratio", "0.01"},
                    {"memtable_whole_key_filtering", "true"}});
```

### 生效范围(关键)

- bloom 在 memtable 构造时创建;**已有 memtable 不会追溯生效**。
- 修改后需要等新 memtable 产生(或主动 flush / 重启)才能观察到效果。
- `memtable_whole_key_filtering` 只在 `memtable_prefix_bloom_size_ratio != 0` 时生效。
- `prefix_extractor` 变更同样影响新 memtable/新 SST;存量 SST 的 prefix 不匹配会禁用 hash index。

### 代价与风险

- 额外内存:`write_buffer_size * ratio`,ratio 越大越占内存。
- 写入时多一次 bloom 插入 CPU;误判会带来少量无效读。
- ratio 过小收益有限;过大(> 0.25)会被截断,不要指望继续调大。

### 验证指标

- `MEMTABLE_HIT` / `MEMTABLE_MISS` ticker
- perf_context `bloom_memtable_hit_count` / `bloom_memtable_miss_count`
- perf_context `get_from_memtable_time` / `get_from_memtable_count`
- `DB_GET` 直方图 P50/P99
- 内存:`rocksdb.cur-size-all-mem-tables`

### 源码依据(v6.26.1)

- `include/rocksdb/advanced_options.h:345-362`:两个参数的注释、默认值、动态可改
- `db/memtable.cc:50-57`:bloom 大小由 `write_buffer_size * ratio` 计算
- `db/memtable.cc:119-122`:`prefix_extractor_` 或 `memtable_whole_key_filtering` 且 bits > 0 时创建 `DynamicBloom`
- `db/memtable.cc:605,642,890-892`:whole-key / prefix 过滤路径
- `options/options_settable_test.cc:496-497`:`memtable_prefix_bloom_size_ratio`、`memtable_whole_key_filtering` 的配置写法

---

## 3. 组合使用顺序

1. 先确认 `prefix_extractor` 与访问模式匹配(前缀长度、是否固定)。
2. 先开 memtable prefix bloom(动态、影响小),复测 `MEMTABLE_HIT/MISS` 与 `DB_GET` P99。
3. 再开 SST `kHashSearch`(需要新 SST);若要覆盖存量数据,单独安排一次 compaction 重写并复测。
4. 每次只动一个参数;把原值、预期、结果写入诊断报告。

---

## 4. 新增手段的模板

后续补充优化点时,按这个结构追加:

```markdown
## N. <一句话标题>

### 命中条件
### 参数
### 配置写法
### 生效范围
### 代价与风险
### 验证指标
### 源码依据(版本 + 文件:行号)
```

要求:参数名、默认值、配置语法必须来自**实际版本源码**,不要凭记忆写。
