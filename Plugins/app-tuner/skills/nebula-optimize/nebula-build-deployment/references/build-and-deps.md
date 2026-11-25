# 构建环境、依赖与编译

## 1. 系统依赖（openEuler / aarch64）

```bash
dnf install -y cmake ninja-build numactl numactl-devel \
  wget flex bison libstdc++-static zstd git make gcc gcc-c++ python3
```

| 包 | 作用 | 缺失后果 |
|----|------|----------|
| `cmake` / `ninja-build` | 构建系统 | 无法配置/编译 |
| `gcc` / `gcc-c++` | 编译器（实测 12.3.1） | — |
| `libstdc++-static` | 静态 libstdc++ | 链接报 `cannot find -lstdc++` |
| `numactl` / `numactl-devel` | NUMA 绑核与库 | 无法按 NUMA 启动 |
| `flex` / `bison` | nGQL 解析器生成 | 解析器编译失败 |
| `zstd` | 解压 LDBC 数据集 | 数据集无法解压 |
| `python3` | importer/工具脚本 | 部分脚本不可用 |

可选：`ccache`（反复编译时提速）、`shellcheck`（脚本检查）。

## 2. third-party 3.3

Nebula 3.8.0 依赖预编译的 third-party（folly/fbthrift/rocksdb/jemalloc 等）。

```bash
mkdir -p "$(dirname "${THIRD_PARTY}")"
# 从官方 release 下载 aarch64 包，先校验 sha256/GPG，再执行
bash vesoft-third-party-3.3-aarch64-libc-2.34-gcc-11.2.0-abi-11.sh \
  --prefix="${THIRD_PARTY}"

ls "${THIRD_PARTY}/lib"/*.a | wc -l      # 实测约 82 个
du -sh "${THIRD_PARTY}"                  # 实测约 459MB
```

选择原则：

- 包的 `libc / gcc / abi` 必须与本机兼容；不匹配时换对应版本或重编。
- 预编译包针对 `armv8-a`，**不要**在 Nebula 侧随意加 `+crc`（或任何 armv8.1+ 的 `-march`，
  因其隐含 CRC）——folly F14 的哈希模式选择只看 `__ARM_FEATURE_CRC32`，`+lse` 原子指令
  **不参与** F14 模式选择（与 §5.4 的 `-march=armv8-a+lse` 优化构建不冲突），加 CRC 特性
  会改变 F14 模式导致链接失败或运行时不兼容。
- 安装前校验完整性；禁止把远程脚本直接管道给 root shell。

## 3. jemalloc 5.3.1（可选，优化构建用）

先固定来源并校验，再执行任何上游脚本：

```bash
# 方式 A（优先）：官方 release tarball + 已发布的 sha256/GPG
#   curl -fSLO <official-tarball-url>
#   sha256sum -c <official-sha256-file>   # 失败即中止
#   tar -xf <tarball> && cd <jemalloc-dir>

# 方式 B：git 克隆后固定到已核对的 commit，而不是直接信可变 tag
git clone https://github.com/jemalloc/jemalloc.git
cd jemalloc
git fetch --tags --force
git checkout --detach "${JEMALLOC_COMMIT}"   # 填入从 release/签名核对得到的 commit
test "$(git rev-parse HEAD)" = "${JEMALLOC_COMMIT}" || {
  echo "ERROR: jemalloc commit 校验失败" >&2; exit 1;
}

export CFLAGS="-O3 -march=armv8-a+lse -fPIC"
export CXXFLAGS="-O3 -march=armv8-a+lse -fPIC"
export LDFLAGS="-march=armv8-a+lse"
./autogen.sh --prefix="${JEMALLOC_PREFIX}" \
  --disable-shared --enable-static --disable-initial-exec-tls
make -j"$(nproc)" && make install

strings "${JEMALLOC_PREFIX}/lib/libjemalloc.a" \
  | grep -m1 -oE '5\.3\.1-0-g[0-9a-f]+' || {
    echo "ERROR: 未检出版本串，不能确认 jemalloc 5.3.1" >&2; exit 1;
  }
```

要点：

- **校验前置**：`sha256sum -c`/GPG/commit 校验必须在 `autogen.sh`、`make` 之前；事后
  `strings` 只能证明版本串，不能阻止已执行的恶意脚本。
- 不要把可变 tag（如 `5.3.1`）当作完整性校验；必须固定 commit 或使用官方 tarball 哈希。
- `-march=armv8-a+lse` 启用 LSE 原子指令；只有 CPU 支持 LSE 时才用。
- 静态库优先于 third-party 内的 jemalloc 5.2.1；链接顺序要让 `-L${JEMALLOC_PREFIX}/lib`
  出现在 third-party 之前。
- 构建记录写入 `${JEMALLOC_COMMIT}` / tarball sha256 / `libjemalloc.a` sha256。

## 4. 辅助工具

| 工具 | 版本（实测） | 用途 |
|------|-------------|------|
| Go | 1.16.6 arm64 | 构建 xk6/k6、importer |
| xk6 | v0.4.1 | 构建带 Nebula 插件的 k6 |
| k6-plugin | v1.0.0 | xk6-nebulagraph 插件 |
| k6 | v0.33.0 | 压测引擎 |
| nebula-console | v3.8.0 arm64 | 交互/脚本查询 |
| nebula-importer | v3.1.0 arm64 | CSV 导入 |
| NebulaGraph-Bench | v1.2.0 | 场景脚本与编排 |

构建 k6 的常见路径：

```bash
# 安装 Go 后；Go module 走 checksum DB，记录实际版本
GOSUMDB=sum.golang.org go install github.com/k6io/xk6/cmd/xk6@v0.4.1

# k6-plugin 固定到已核对 commit，先校验再执行 make
git clone https://github.com/vesoft-inc/k6-plugin.git
cd k6-plugin
git fetch --tags --force
git checkout --detach "${K6_PLUGIN_COMMIT}"   # 填入已核对的 commit
test "$(git rev-parse HEAD)" = "${K6_PLUGIN_COMMIT}" || {
  echo "ERROR: k6-plugin commit 校验失败" >&2; exit 1;
}
make build-dev                      # 产出 k6 二进制
sha256sum k6 > k6.sha256            # 记录产物身份
```

> 上述 `make build-dev` 会执行上游构建脚本；只能在来源/commit 校验通过后运行，
> 并尽量使用非 root 构建用户。不要用可变 tag 代替完整性校验。



> importer v3.1.0 的 `withHeader: true` 在部分环境有崩溃问题；优先使用无表头 CSV。

## 5. 编译 Nebula

### 5.1 通用配置

```bash
cd "${NEBULA_HOME}"
mkdir -p "${BUILD_DIR}" && cd "${BUILD_DIR}"
cmake .. -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DNEBULA_THIRDPARTY_ROOT="${THIRD_PARTY}" \
  -DENABLE_STANDALONE_VERSION=OFF \
  -DENABLE_MEMORY_TRACKER=OFF \
  -DENABLE_TESTING=OFF \
  -DENABLE_WERROR=OFF \
  -DNEBULA_USE_LINKER=bfd \
  -DCMAKE_C_COMPILER=gcc -DCMAKE_CXX_COMPILER=g++
```

| 选项 | 作用 | 建议 |
|------|------|------|
| `ENABLE_STANDALONE_VERSION` | ON=单进程三服务；OFF=三个独立二进制 | 功能验证 ON；多进程架构 OFF |
| `ENABLE_MEMORY_TRACKER` | 每次 new/delete 记账 | 压测/性能对比 OFF；内存排查 ON |
| `ENABLE_TESTING` | 编译 ctest/TCK 依赖 | 只部署 OFF；要跑单测 ON |
| `ENABLE_WERROR` | 告警当错误 | GCC 12 对 folly/s2 告警会失败，设 OFF |
| `NEBULA_USE_LINKER` | 链接器 | bfd 最稳；lld 需自行验证 |

### 5.2 standalone

```bash
cmake .. ... -DENABLE_STANDALONE_VERSION=ON
ninja -j"$(nproc)" nebula-standalone
# 产物：${BUILD_DIR}/bin/nebula-standalone
```

### 5.3 多进程分布式

```bash
ninja -j"$(nproc)" nebula-metad nebula-graphd nebula-storaged
# 产物：bin/nebula-metad、bin/nebula-graphd、bin/nebula-storaged（实测各 54–59MB）
```

### 5.4 优化构建（LTO + LSE + jemalloc 5.3.1）

```bash
cmake .. -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DNEBULA_THIRDPARTY_ROOT="${THIRD_PARTY}" \
  -DENABLE_STANDALONE_VERSION=OFF \
  -DENABLE_MEMORY_TRACKER=OFF -DENABLE_TESTING=OFF -DENABLE_WERROR=OFF \
  -DNEBULA_USE_LINKER=bfd \
  -DCMAKE_C_COMPILER=gcc -DCMAKE_CXX_COMPILER=g++ \
  -DCMAKE_C_FLAGS="-march=armv8-a+lse -I${JEMALLOC_PREFIX}/include -flto=auto" \
  -DCMAKE_CXX_FLAGS="-march=armv8-a+lse -I${JEMALLOC_PREFIX}/include -flto=auto" \
  -DCMAKE_EXE_LINKER_FLAGS="-march=armv8-a+lse -L${JEMALLOC_PREFIX}/lib -flto=auto"
ninja -j"$(nproc)" nebula-metad nebula-graphd nebula-storaged
```

前提与限制：

- CPU 必须支持 LSE（`lscpu` 的 Flags 含 `atomics`）。
- `-L${JEMALLOC_PREFIX}/lib` 要在 third-party 搜索路径之前，链接才会选 jemalloc 5.3.1。
- LTO 会增加编译时间，二进制通常略小；是否保留以实测为准。
- 优化构建必须单独记录编译参数，不能与基线二进制混跑对比。

## 6. 时区文件与运行目录

```bash
mkdir -p "${NEBULA_HOME}/share/resources"
cp -f "${NEBULA_HOME}/resources/date_time_zonespec.csv" \
      "${NEBULA_HOME}/share/resources/"
```

**必须在 `${NEBULA_HOME}` 下启动**：部分版本按相对路径查找 `share/resources`，
找不到会 SIGSEGV/SIGBUS。systemd 单元要显式 `WorkingDirectory=${NEBULA_HOME}`。

## 7. 构建产物校验

```bash
# 1) 文件存在且大小合理（花括号展开必须发生在引号外，只给路径前缀加引号）
ls -lh "${BUILD_DIR}"/bin/nebula-{metad,graphd,storaged}

# 2) 版本与符号
"${BUILD_DIR}/bin/nebula-graphd" --version || true
nm -C "${BUILD_DIR}/bin/nebula-storaged" | grep -c "nebula::storage::StorageServer" || true

# 3) jemalloc 版本（优化构建）
strings "${BUILD_DIR}/bin/nebula-graphd" | grep -m1 -E 'jemalloc|[0-9]+\.[0-9]+\.[0-9]+-[0-9]+-g[0-9a-f]+'
# 期望：5.3.1（native 通常为 5.2.1）

# 4) LSE 指令数量（优化构建 vs native）
objdump -d "${BUILD_DIR}/bin/nebula-graphd" \
  | grep -cE '\b(ldadd|ldset|ldclr|ldeor|cas|swp)[a-z]*\s'

# 5) 记录身份
md5sum "${BUILD_DIR}/bin/nebula-"{metad,graphd,storaged}
git -C "${NEBULA_HOME}" rev-parse --short HEAD
```

> 注意：LSE 指令数量受编译器版本、内联策略影响，只用于“是否生效”的粗判，不作为性能结论。

## 8. 第三方库升级（可选，风险较高）

实测把 2021 版第三方库升级到 2025 版（folly/fbthrift/wangle/fizz/proxygen/mvfst、
rocksdb 11.8.1、jemalloc 5.3.1、boost 1.89、glog 0.7.1、fmt 11.2.0 等）需要一组适配补丁。

| 库 | 关键改动 | 风险 |
|----|---------|------|
| rocksdb 11.8.1 | C++20 `operator== = default` / `using enum` 改 C++17 写法；`-march=...` 加 `+lse` | API/ABI、数据格式升级 |
| folly 2025 | 移除 `boost::system` 依赖；禁用 `__cxa_throw` 覆盖避免符号冲突 | 与 libstdc++ 符号冲突 |
| fbthrift 2025 | `HeaderServerChannel::isOneway` 空指针检查；`startConsumingInternal` 改名 | 服务端行为变化 |
| fizz/mvfst/proxygen/wangle | API/依赖适配 | 编译链长 |
| snappy/double-conversion | 版本适配 | 低 |
| Nebula 侧 | Thrift 影子类型、folly API 迁移、glog 0.7、RocksDB 11.8 API、链接顺序、RaftexService | 需要逐个 patch |

结论（实测）：

- 全量升级对**读路径**净贡献接近 0；`FindShortestPath` 的收益主要来自 Nebula 侧补丁，
  不是升级本身。
- 写入路径受 RocksDB 版本影响明显：11.8 的写性能可能显著高于 7.5，但数据格式不兼容，
  旧二进制打不开新数据。
- 升级需要重编 third-party 和 Nebula，周期长、风险高；没有明确收益时优先保持 3.3。

## 9. 编译选项的负面结果（避免重复踩坑）

| 选项 | 实测结果 | 原因 |
|------|---------|------|
| `-march=armv8.4-a+crc` | 链接失败 | 改变 folly F14 哈希模式，与预编译库不匹配 |
| `-fno-plt` / `-fno-semantic-interposition` / `-Bsymbolic` | 链接失败 | 与预编译库符号可见性冲突 |
| `-mtune=neoverse-n2` | Go1 QPS 约 -2.5% | 热点在 futex/jemalloc，指令调度收益为负 |
| `-mtune=tsv110` | 全场景 -1% ~ -3.2% | 鲲鹏 950 与 tsv110 流水线不同 |
| third-party `-O3`（autoconf 库） | 无显著收益 | 这些库已充分优化，负载非计算密集 |
| `-march=armv9.2-a+sve2` 自动向量化 | 需单独验证 | 热路径字符串宽度 ≤ NEON 单向量，SVE2 未必有增量 |

原则：**一次只改一个编译变量**，保留基线二进制，用同一数据/配置/压测口径对比。

## 10. 常见编译/链接错误

| 错误 | 原因 | 处置 |
|------|------|------|
| `cannot find -lstdc++` | 缺静态 libstdc++ | 安装 `libstdc++-static` |
| `F14LinkCheck` / F14 相关未定义 | folly F14 模式与编译 `-march` 不一致 | 去掉 `+crc`，与预编译库保持一致 |
| `-Werror` 在 folly/s2 告警处失败 | GCC 12 告警更严格 | `-DENABLE_WERROR=OFF` |
| CMake 找不到 third-party | 路径错或包未装 | 核对 `NEBULA_THIRDPARTY_ROOT` 与 `.a` 数量 |
| 链接到 jemalloc 5.2.1 | `-L` 顺序错 | 把 jemalloc 路径放在 third-party 之前 |
| `date_time_zonespec.csv` 找不到 | 未复制到 `share/resources` | 按 §6 复制并在源码根启动 |
| 编译内存不足 | 并发过高 | 降低 `-j`，或增加内存/swap |
| 离线构建 config.guess 失败 | 无法联网探测 | 使用本地 `config.guess` 回退（third-party 构建脚本） |
