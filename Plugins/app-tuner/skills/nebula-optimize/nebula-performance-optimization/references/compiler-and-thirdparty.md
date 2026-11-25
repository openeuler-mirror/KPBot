# 编译选项与第三方库

## 1. 先固定基线

编译优化必须能回答“相对谁提升了”。记录：

- Nebula commit、二进制 md5；
- third-party 版本、jemalloc 版本、RocksDB 版本；
- CMake flags、`-march`、LTO、链接器；
- 同一份数据、同一部署/绑核、同一压测口径。

建议保留 `bin_native/` 与 `bin_opt/` 两套二进制，随时可 A/B。

## 2. LSE（ARM Large System Extensions）

### 2.1 机制

未启用 LSE 时，C++ 原子操作可能走 outline atomics：函数调用 + 运行时检测 + 分支 + LL/SC。
启用后直接生成 `ldadd/ldswp/cas` 等单条指令。

受影响最大的库：

- folly：F14 哈希表、引用计数、futex、Executor；
- RocksDB：迭代器/缓存引用计数、并发结构；
- jemalloc：分配器原子操作；
- wangle/fizz/proxygen 等。

### 2.2 实测

在 third-party 与 Nebula 侧统一加 `-march=armv8-a+lse`：

| 场景 | 基线 QPS | LSE QPS | 变化 |
|------|---------:|--------:|-----:|
| Go1Step | 186,734 | 187,466 | +0.4% |
| Go2Step | 14,419 | 14,330 | -0.6% |
| Go3Step | 514 | 513 | -0.2% |
| FindShortestPath | 9,092 | 10,670 | **+17.4%** |
| InsertPerson | 8,718 | 8,405 | -3.6% |

解释：FSP 的 BFS 大量使用 F14/引用计数/并发结构，LSE 收益明显；其他场景在噪声内。
这是“消除 outline 桩”的确定性收益，风险较低（前提：CPU 支持 LSE、库与 Nebula 一致）。

### 2.3 注意

- 只对 Nebula 加 `+lse`、third-party 仍是 outline，会有一部分收益但不如全量一致。
- 预编译 folly 的 F14 模式受 `-march` 的 CRC 特性影响；`+crc` 可能改变 F14 模式导致链接
  失败。LSE 与 CRC 是不同特性，不要混为一谈。
- 用 `objdump` 统计 LSE 指令数只能证明“生成了”，收益仍需 A/B。

## 3. LTO

```bash
-DCMAKE_C_FLAGS="-flto=auto ..." \
-DCMAKE_CXX_FLAGS="-flto=auto ..." \
-DCMAKE_EXE_LINKER_FLAGS="-flto=auto ..."
```

实测：

- 二进制通常略小（如 54MB → 51MB）；
- 读/写场景收益随版本/数据变化，不保证普适；
- 编译时间增加，链接内存增加；
- 必须整体一致（编译与链接都开），否则可能链接错误/无效果。

建议：作为独立变量验证一次；有稳定收益再保留。

## 4. jemalloc 5.3.1

```bash
./autogen.sh --prefix="${JEMALLOC_PREFIX}" \
  --disable-shared --enable-static --disable-initial-exec-tls
make -j"$(nproc)" && make install
```

链接：

```bash
-DCMAKE_C_FLAGS="-I${JEMALLOC_PREFIX}/include ..." \
-DCMAKE_CXX_FLAGS="-I${JEMALLOC_PREFIX}/include ..." \
-DCMAKE_EXE_LINKER_FLAGS="-L${JEMALLOC_PREFIX}/lib ..."
```

要点：

- `-L${JEMALLOC_PREFIX}/lib` 要在 third-party 之前，否则会链接到 5.2.1。
- 确认版本用**可归属到 jemalloc 的证据**：构建/链接命令行（`-L${JEMALLOC_PREFIX}/lib` 是否
  生效）、`ldd` 中的 libjemalloc 条目（动态链接时）、二进制内 jemalloc 自身版本 banner——
  注意真实 banner **无 `jemalloc-` 前缀**，形如 `5.3.1-0-g81034ce…`（jemalloc.h:111，
  能命中 `jemalloc-5.3.1-…` 的只有 DWARF 构建目录路径，不是 banner 证据）。
  **归属先行**：`x.y.z-0-g<hash>` 只是格式特征——其他依赖库的 git-describe 版本串
  （如 5.3.1-0-g12345678）同样匹配，单凭格式取首个字符串会把别库版本当 jemalloc。
  必须先确认归属通道（ldd 的 libjemalloc 条目，或 nm 的 malloc/free/je_*_default
  定义符号——静态链接本 skill §4 的形态）非空，banner 才能作为版本证据；两者皆无
  时读到格式相似串只能记「未验证」，不能当 jemalloc 版本（verify_build_opts.sh 同口径）：
  `strings bin | grep -oE '\b[0-9]+\.[0-9]+\.[0-9]+-[0-9]+-g[0-9a-f]{8,40}\b'`；
  本 skill §4 的静态链接（--disable-shared）下 ldd 无条目，改用 nm 定义符号
  `nm | grep -E ' (T|W) (malloc|free|je_malloc|je_free|je_malloc_default|je_free_default)$'`
  作归属符号证据——无 `--with-jemalloc-prefix` 构建（third-party v3.3 不带前缀）下
  jemalloc_rename.h 把 je_malloc/je_free #define 成 malloc/free，公共符号以 malloc/free
  进入二进制、库内符号为 je_malloc_default/je_free_default；只 grep `je_malloc$` 在真实
  无前缀产物上恒为 0 命中。对任意 "x.y.z" 串 `grep -m1` 可能命中其他依赖的版本串或普通符号名，
  不能作归属证据，无法确认时明确记"未验证"。
- 收益取决于分配模式：读场景（Value/对象多）可能受益，写场景受 RocksDB 版本影响更大。
- 换分配器（mimalloc/tcmalloc）属于独立实验，不做基线不要直接上。

## 5. F14/ABI 约束

folly F14 会根据编译特性选择 `Simd` 或 `SimdAndCrc` 模式。若预编译 folly 与 Nebula 编译
参数不一致，可能出现：

- `F14LinkCheck` 未定义/链接失败；
- 运行时行为不一致。

规则：

- 使用预编译 third-party 时，Nebula 侧不要随意加 `+crc`、改变 F14 相关特性。
- 若要加，先确认 third-party 的编译参数，或整体重编 third-party。
- `-fno-plt`、`-fno-semantic-interposition`、`-Bsymbolic`、`-fvisibility=hidden` 等会改变
  符号可见性，与预编译库组合时容易链接失败；没有验证不要用。

## 6. 第三方库全量升级（2021 → 2025）

### 6.1 版本矩阵（实测）

| 库 | 旧 | 新 |
|----|----|----|
| folly/fbthrift/wangle/fizz/proxygen/mvfst/fatal | 2021.11.29 | 2025.06.09 |
| rocksdb | 7.5.3 | 11.8.1 |
| jemalloc | 5.2.1 | 5.3.1 |
| boost | 1.76 | 1.89 |
| glog | 0.5.0 | 0.7.1 |
| fmt | 8.0.1 | 11.2.0 |
| zstd/lz4/snappy | 1.5.0/1.9.3/1.1.9 | 1.5.7/1.10.0/1.2.2 |
| double-conversion | 3.1.5 | 3.4.0 |

### 6.2 必要适配

| 库 | 改动 |
|----|------|
| RocksDB 11.8 | C++20 `operator== = default`/`using enum` 改 C++17；`-march` 加 `+lse` |
| folly 2025 | 去掉 `boost::system` 依赖；禁用 `__cxa_throw` 覆盖避免符号冲突 |
| fbthrift 2025 | `HeaderServerChannel::isOneway` 空指针检查；`startConsumingInternal` 改名 |
| fizz/mvfst/proxygen/wangle | API/依赖适配补丁 |
| Nebula | Thrift 影子类型、folly API、glog 0.7、RocksDB API、链接顺序、RaftexService |

### 6.3 实测结论

- 全量升级对**读路径**净贡献接近 0；FSP 的收益主要来自 Nebula 侧补丁，不是升级本身。
- 写入路径受 RocksDB 版本影响明显：11.8 可能显著高于 7.5，但数据格式不兼容。
- 升级周期长、风险高；没有明确收益时，保持已验证的 3.3 依赖栈。

## 7. NEON / SVE 的边界

常见误解：

- “必须加 `-march=armv8-a` 才能用 NEON”：aarch64 默认定义 `__ARM_NEON`，手写 NEON 不依赖
  特定 `-march`；`-march` 主要影响编译器自动向量化范围。
- “NEON 补丁一定能加速”：收益取决于该字符串/比较操作在总 CPU 中的占比。实测字符串
  NEON 优化在 Go1/2/3 上多为噪声；TSC 快速路径有小幅收益。
- “SVE2 一定比 NEON 快”：热路径字符串宽度常 ≤16B，NEON 单向量已覆盖，SVE2 无增量；
  需按实际宽度/循环验证。

NEON 正确性坑（实测）：

- 用 `vmaxvq_u8(cmp) == 0` 判断“不相等”是错的：只有全部 16 字节都不同才返回 0；
  FIXED_STRING 的 `\0` 填充会让不同 VID 被误判相等，导致少做工作、QPS 虚高。
- 正确写法：`vminvq_u8(cmp) != 0xFF`（任一字节不同即不相等）。
- 任何 SIMD 改动都要做逐字节/逐 VID 比对，不能只看 QPS。

## 8. 负结果

| 选项 | 结果 | 原因 |
|------|------|------|
| `-march=armv8.4-a+crc` | 链接失败 | F14 模式与预编译库不匹配 |
| `-fno-plt` / `-fno-semantic-interposition` / `-Bsymbolic` | 链接失败 | 符号可见性冲突 |
| `-mtune=neoverse-n2` | Go1 -2.5% | 热点在 futex/jemalloc，非指令调度 |
| `-mtune=tsv110` | 全场景 -1%~-3.2% | 鲲鹏 950 与 tsv110 流水线不同 |
| third-party `-O3` | 无显著收益 | autoconf 库已充分优化 |
| 全量第三方升级 | 读路径≈0 | 收益主要来自 Nebula 补丁 |

## 9. 验证命令

```bash
# 库版本（归属先行：先 ldd/nm 通道，后 banner——见 §4 归属先行说明）
ldd "${BUILD_DIR}/bin/nebula-graphd" | grep -i libjemalloc || echo "未动态链接 libjemalloc（静态构建用下行 nm 判定）"
n=$(nm "${BUILD_DIR}/bin/nebula-graphd" 2>/dev/null | grep -cE ' (T|W) (malloc|free|je_malloc|je_free|je_malloc_default|je_free_default)$')
echo "静态链接 jemalloc 定义符号数（malloc/free/je_*，无前缀构建公共符号为 malloc/free）: ${n}"
if [ "${n:-0}" -eq 0 ] && ! ldd "${BUILD_DIR}/bin/nebula-graphd" 2>/dev/null | grep -qi libjemalloc; then
  echo "无 jemalloc 归属通道（ldd/nm 均无证据）：banner 不作为版本证据，版本未验证"
else
  strings "${BUILD_DIR}/bin/nebula-graphd" | grep -oE '\b[0-9]+\.[0-9]+\.[0-9]+-[0-9]+-g[0-9a-f]{8,40}\b' | head -1 || echo "版本 banner 不可读（已核归属，未读取到版本）"
fi

# LSE 指令
objdump -d "${BUILD_DIR}/bin/nebula-graphd" \
  | grep -cE '\b(ldadd|ldset|ldclr|ldeor|cas|swp)[a-z]*\s'

# 关键源码符号
nm -C "${BUILD_DIR}/bin/nebula-storaged" | grep -E 'findFirstNull|TscHelper' || true

# 二进制身份
md5sum "${BUILD_DIR}/bin/nebula-"{metad,graphd,storaged}
git -C "${NEBULA_HOME}" rev-parse --short HEAD
```

这些只证明“编译生效”，不证明“性能提升”；性能必须 A/B。

## 10. 决策树

```
读场景瓶颈在 FSP/哈希/引用计数？
  ├─ 是 → 先试 LSE（收益明确、风险低）
  └─ 否 → 先做部署/配置（实例/绑核/缓存），再考虑 LTO/jemalloc
写入瓶颈在 WAL/compaction？
  ├─ 是 → 先调 WAL/compaction/磁盘/批量，不要先升级第三方
  └─ 否 → 看解析/CPU，再考虑编译
需要全量第三方升级？
  ├─ 有明确 API/安全需求 → 测试环境完整验证 + 数据备份
  └─ 只为性能 → 通常不做；读路径收益≈0
```
