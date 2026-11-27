# 编译、三方库与源码优化

本文件覆盖 ARM 原子指令（LSE）、分配器、`-march`/LTO/PGO、三方升级，以及
InnoDB 源码级优化：伪共享、顺序锁、lock_word 拆分、NUMA interleave mask、cache-miss。
**源码部分以 10.3 案例为证据，但 10.11 的锁实现已重写，移植前必须重新验证。**

## 1. LSE（ARMv8.1 原子指令）

### 1.1 机制

ARMv8.0 用 `LDREX/STREX` 循环实现原子操作：

```
LDREX  r1, [addr]      ; 标记独占
... 计算 ...
STREX  r2, r1, [addr]  ; 若独占标记被清除则失败，r2=1，需要重试
```

高竞争下大量重试，表现为 `cas`/`ldrex` 热点。ARMv8.1 的 LSE 提供单指令原子：

- `CAS/CASP`（比较交换）
- `LDADD/LDCLR/LDEOR/LDSET/LDSMAX/LDSMIN/LDUMAX/LDUMIN`（原子读改写）
- `SWP`（交换）

一条指令完成，减少重试与总线流量。

### 1.2 构建

```
-DCMAKE_C_FLAGS="-O2 -march=armv8.2-a+lse"
-DCMAKE_CXX_FLAGS="-O2 -march=armv8.2-a+lse"
```

验证（与 `scripts/verify_build_opts.sh` 的“架构与指令集”段共用同一检查入口，
结论以脚本输出为准；不要在文档里另写一套裸指令名匹配）：

```
# 按反汇编助记符列（$3）匹配，覆盖 casal/ldaddal/swpal 等内存序后缀变体，
# 并覆盖 ldset/ldclr/ldeor/ldsmax/ldsmin/ldumax/ldumin 及 st 前缀族等完整 LSE 家族
# （与 §1.1 列出的指令族及 verify_build_opts.sh 的正则一致）。
# 先判 objdump 退出码——扫描失败与“扫描成功但零命中”是两种不同结论。
DIS="$(mktemp)" || exit 1
if objdump -d "${MARIADB_HOME}/bin/mariadbd" >"$DIS" 2>/dev/null; then
  awk '$3 ~ /^(cas|ldadd|ldclr|ldeor|ldset|ldsmax|ldsmin|ldumax|ldumin|swp|stadd|stclr|steor|stset|stsmax|stsmin|stumax|stumin)[a-z]*$/' "$DIS" | wc -l
else
  echo "objdump 失败：不能按零命中判定" >&2
fi
rm -f "$DIS"
```

### 1.3 结论与风险

- 鲲鹏 4P 的 C3（TPC-C）场景中 CAS 是热点，启用 LSE **实测有效**；但同一资料明确
指出“某些场景使用 LSE 会劣化”。
- 必须：目标 CPU 支持 → 目标负载 A/B → 比较 TPS/p99/CPU，而不是只看指令数。
- 二进制分发到不支持 LSE 的机器会非法指令；跨机型要分开构建。

## 2. 分配器：jemalloc / tcmalloc

| 方式 | 做法 | 优点 | 风险 |
| --- | --- | --- | --- |
| jemalloc（构建期） | `-DCMAKE_EXE_LINKER_FLAGS="-ljemalloc"` | 减少碎片/分配开销 | ABI/链接顺序；只选一种 |
| tcmalloc（运行期） | `LD_PRELOAD=libtcmalloc.so` | 无需重编，易回滚 | 与 jemalloc 冲突；需 frame pointers |

验证：

```
ldd "${MARIADB_HOME}/bin/mariadbd" | grep -Ei 'jemalloc|tcmalloc' || true

# 运行期必须是指定连接对应的实例：用 verify_build_opts.sh --socket /path/to.sock
# 确认 PID；多实例且 PID 无法确认时跳过该项，不要用 pgrep | head -1 取第一个进程。
# 单实例也可用服务单元的 MainPID：
#   PID="$(systemctl show -p MainPID --value mariadb)"
#   lsof -p "$PID" | grep -Ei 'jemalloc|tcmalloc' || true
```

纪律：

- 只启用一种；同时链接 jemalloc 又 preload tcmalloc 会让分配来源不确定。
- 分配器收益取决于分配模式（临时对象/结果集/连接），必须同口径 A/B。
- `--enable-frame-pointers` 有利于 perf 栈，但可能略影响性能，记录在案。

## 3. `-march`、LTO、PGO

| 选项 | 作用 | 注意 |
| --- | --- | --- |
| `-march=armv8.2-a+lse` | ARM 指令集 + LSE | 见 §1 |
| `-march=native`（x86） | 使用本机指令集 | 分发到不同 CPU 可能非法指令 |
| `-mtune=...` | 调度优化 | 收益通常小于热点改造，需 A/B |
| LTO | 跨编译单元优化 | 编译慢；需整体一致；可能改变行为 |
| PGO | 用训练负载优化分支/布局 | 训练集必须代表目标负载；可复现性差 |

原则：先做 LSE（路径清晰），LTO/PGO 只在有明确热点与稳定训练集时尝试；
不要用 `-O3` 盲替换 `-O2` 作为“优化”。

## 4. 三方库升级

- 升级 OpenSSL/zlib/lz4 通常只影响功能/安全，不直接提升 TPS。
- 升级 jemalloc/tcmalloc 可能改变分配行为，需要重新 A/B。
- 升级 RocksDB（MyRocks）涉及数据格式与 compaction 行为，走 `rocksdb-perf-diagnostics`。
- 全量三方升级的收益不确定，风险包括 API/ABI/数据格式/构建链；没有明确收益不要做。

## 5. 源码优化（10.3 证据，10.11 需重验）

### 5.1 伪共享修复

**机制**：多个线程频繁写同一 cache line 的不同变量，导致 cache line 在核间来回失效
（false sharing），表现为 `%sys` 高、IPC 低、原子/内存屏障热点。

**做法**：把热点共享结构按 cache line（通常 64B，ARM 可能 128B）对齐/填充：

```
struct hot_counter {
  alignas(64) std::atomic<uint64_t> a;
  alignas(64) std::atomic<uint64_t> b;
};
```

或在字段间加 padding（案例中的 `char pad1[128]`）。

**验证**：perf 中同一函数多线程高占比 + off-CPU 少 + IPC 低；改后对比 `%sys`、IPC、TPS。

**风险**：内存增加、结构布局变化影响其他字段；必须确认共享结构真实存在竞争。

### 5.2 读写锁改顺序锁（10.3 专用）

**机制**：读多写少时，读路径不加锁；写时递增计数器，读前后校验计数，冲突则重试。

案例实现（10.3，`storage/innobase/include/sync0rw.h` 的 `rw_lock_t`）：

```
/* 结构体新增 write_count + padding */
char pad1[128];
int32_t lock_word;
char pad2[128];
unsigned long write_count = 0;
char pad3[128];
int32_t waiters;
```

读路径：进入前读 `write_count`（偶数=无写），退出后再读，若一致则无锁成功；
否则退回原读锁流程。写路径：加锁前/解锁后原子更新 `write_count`。

**10.11 适用性**：`rw_lock_t` 已被 `std::atomic<uint32_t>` 的 `rw_lock` 类取代
（`include/rw_lock.h`），不存在 `lock_word`/`write_count`。该补丁**不能直接移植**，
需基于新实现重新设计并验证内存序/唤醒语义。

### 5.3 拆分 lock_word 到 s_lock_word（10.3 专用）

**机制**：读锁也走 CAS 修改同一 `lock_word`，高并发读时 CAS 成为热点。拆出
`s_lock_word` 让读锁用原子加，写锁只检查 `s_lock_word > 0`。

案例代码（节选）：

```
/* 加读锁：先原子加 s_lock_word，再检查写锁 */
my_atomic_add32_explicit(&lock->s_lock_word, 1, MY_RW_LOCK_ORDER_RELAXED);
if (my_atomic_load32_explicit(&lock->lock_word, MY_RW_LOCK_ORDER_LOAD) <= 0) {
    if (my_atomic_add32_explicit(&lock->s_lock_word, -1, MY_RW_LOCK_ORDER_STORE) == 1) {
        os_event_set(lock->wait_ex_event);
        sync_array_object_signalled();
    }
    return false;
}
return true;
```

```
/* 解读锁：s_lock_word 减到 0 且 lock_word <= 0 时唤醒写 */
if (my_atomic_add32_explicit(&lock->s_lock_word, -1, MY_RW_LOCK_ORDER_STORE) == 1 &&
    my_atomic_load32_explicit(&lock->lock_word, MY_RW_LOCK_ORDER_LOAD) <= 0) {
    os_event_set(lock->wait_ex_event);
    sync_array_object_signalled();
}
```

**10.11 适用性**：字段不存在，**不适用**。10.11 的 `rw_lock` 已用单原子字编码状态，
需要重新看 `rw_lock.h`/`srw_lock.h` 的读写路径与 futex 唤醒逻辑。

### 5.4 NUMA interleave node mask（定制补丁）

**机制**：默认 `innodb_numa_interleave` 只有开/关；定制补丁增加“按节点掩码交错”，
避免内存全部落在远端或单节点。

案例参数：

```
innodb_numa_interleave_permanent = 15   # 节点掩码（示例：低 4 位）
```

依赖：`libnuma` + 源码修改。**10.11 上游没有该参数**，属于下游定制；使用前确认
补丁来源、维护成本与升级兼容性。

### 5.5 cache-miss 优化

方向：

- 改善数据结构布局，减少随机访问与指针追逐；
- 热字段集中、冷字段分离；
- 预取（prefetch）规律访问的下一页/下一条记录；
- 避免大对象拷贝，使用引用/视图；
- 按 NUMA 分配本地内存。

验证：Topdown 的 Memory Bound、LLC miss、`perf stat` 的 cache-misses；
收益通常小于索引/配置优化，需明确证据。

## 6. 源码变更纪律

1. 先在 10.11 采样，确认热点函数与机制仍存在（不要照搬 10.3 符号）。
2. 设计最小改动，说明内存序、锁语义、失败路径。
3. 编译（Release + debug 符号）、mtr 相关 suite、业务路径比对。
4. 同口径 A/B，分场景判定；负收益回滚。
5. 记录：文件/函数、diff 摘要、二进制 md5、收益、风险、回滚命令。
6. 高风险改动（锁/事务/日志/持久化）必须有资深评审与回滚预案。

## 7. 负结果提示

- LSE 不是“全场景更快”；
- 分配器不是“换了就快”；
- `-O3`/LTO/PGO 不一定有收益；
- 老版本锁补丁不能直接移植；
- 没有热点证据的源码改造通常是负收益。
