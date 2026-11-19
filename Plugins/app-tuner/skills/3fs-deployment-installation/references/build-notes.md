# 3FS 编译环境与构建坑位

> **何时读我**：首次编译 3FS、切换 folly 分支后重编译、编译 USRBIO/fio 插件，或排查 CMake/编译器报错时。部署流程见 `installation-guide.md`，编译产物上线见 `binary-replacement.md`。
>
> 环境：鲲鹏 (aarch64，920 实测；CMake/编译器坑位与机型无关，适用于鲲鹏平台) + openEuler 24.03 LTS + 毕昇编译器 4.2.0（clang 17）。命令引用 `installation-guide.md` 开头的集群变量表（`${SRC_ROOT}`、`${BUILD_DIR}` 等）。编译仅在编译节点（示例为 meta）执行。

## 1. 依赖清单

```shell
yum install cmake libuv-devel lz4-devel xz-devel double-conversion-devel \
            libdwarf libdwarf-devel libunwind libunwind-devel libaio-devel \
            gflags-devel glog glog-devel gtest-devel gmock-devel gperftools-devel \
            gperftools openssl-devel gcc gcc-c++ boost* libatomic autoconf libevent-devel \
            libibverbs libibverbs-devel cargo numactl-devel lld gperftools-devel gperftools \
            double-conversion libibverbs rsync gperftools-libs glibc-devel python3-devel \
            meson vim jemalloc -y
```

## 2. 组件安装

### 2.1 Rust 1.85.0

```shell
cd /home
wget https://static.rust-lang.org/dist/rust-1.85.0-aarch64-unknown-linux-gnu.tar.xz
tar -xvf rust-1.85.0-aarch64-unknown-linux-gnu.tar.xz
cd rust-1.85.0-aarch64-unknown-linux-gnu
sh install.sh
```

### 2.2 FoundationDB 7.3.63（client + server）

```shell
wget https://github.com/apple/foundationdb/releases/download/7.3.63/foundationdb-clients-7.3.63-1.el9.aarch64.rpm
wget https://github.com/apple/foundationdb/releases/download/7.3.63/foundationdb-server-7.3.63-1.el9.aarch64.rpm
rpm -ivh foundationdb-clients-7.3.63-1.el9.aarch64.rpm
rpm -ivh foundationdb-server-7.3.63-1.el9.aarch64.rpm
```

安装后还需按 `installation-guide.md` §5.2 修改 foundationdb.conf / fdb.cluster。
编译节点需要 `/usr/lib64/libfdb_c.so`（随 client RPM 安装），部署时 rsync 到各节点。

### 2.3 毕昇编译器 4.2.0

```shell
cd /home
wget https://mirrors.huaweicloud.com/kunpeng/archive/compiler/bisheng_compiler/BiShengCompiler-4.2.0-aarch64-linux.tar.gz
tar -xvf BiShengCompiler-4.2.0-aarch64-linux.tar.gz
export PATH=/home/BiShengCompiler-4.2.0-aarch64-linux/bin:$PATH
clang -v
```

预期回显：

```txt
BiSheng Enterprise 4.2.0.B009 clang version 17.0.6 (958fd14d28f0)
Target: aarch64-unknown-linux-gnu
Thread model: posix InstalledDir: /home/BiShengCompiler-4.2.0-aarch64-linux/bin
Found candidate GCC installation: /usr/lib/gcc/aarch64-linux-gnu/10.3.1
Selected GCC installation: /usr/lib/gcc/aarch64-linux-gnu/10.3.1
Candidate multilib: .;@m64
Selected multilib: .;@m64
```

建议把 PATH 导出与代理配置写入自维护的环境脚本（如 `/opt/src/scripts/bisheng.sh`、`proxy.sh`），编译前 source。

### 2.4 Libfuse 3.16.1

```shell
cd /home
wget https://github.com/libfuse/libfuse/releases/download/fuse-3.16.1/fuse-3.16.1.tar.gz
tar -vzxf fuse-3.16.1.tar.gz
cd fuse-3.16.1
mkdir build
cd build
yum install -y meson
meson setup ..
ninja
ninja install
```

## 3. 3FS 源码获取与补丁

### 3.1 源码与官方补丁

```shell
export LD_LIBRARY_PATH=/usr/local/lib64:/usr/local/lib:/usr/local:/usr/lib:/usr/lib64:$LD_LIBRARY_PATH
mkdir -p ${SRC_ROOT} && cd ${SRC_ROOT}
git clone https://github.com/deepseek-ai/3fs
cd 3fs
git checkout 497941599c2ed7cefe2878b5c35dbd18f25a0415    # 实录所用 commit，按需替换
git submodule update --init --recursive
./patches/apply.sh
```

### 3.2 openEuler 适配补丁

```shell
mkdir -p /opt/src/install && cd /opt/src/install
wget https://gitcode.com/boostkit/3FS/blob/master/openeuler_3fs.patch
wget https://gitcode.com/boostkit/3FS/blob/master/openeuler_3fs_folly.patch
cd ${SRC_ROOT}/3fs
patch -p1 < /opt/src/install/openeuler_3fs.patch
cd ${SRC_ROOT}/3fs/third_party/folly
patch -p1 < /opt/src/install/openeuler_3fs_folly.patch
```

### 3.3 folly 优化分支（可选，ARM CRC32C 硬件加速）

实录部署的二进制均包含 folly 的 ARM CRC32C 硬件加速优化（crc32c_m 分支，来自团队自维护的 folly fork；
fork 地址按团队实际替换，优化细节见 skill `3fs-hotspot-code-optimization`）：

```shell
cd ${SRC_ROOT}/3fs/third_party/folly
git remote add fork <团队 folly fork URL>
git fetch fork
git checkout crc32c_m
```

分支切换/跟进上游的工作流（来自重编译 SOP）：

```shell
cd ${SRC_ROOT}/3fs/third_party/folly
git branch --show-current
git log --oneline -5
git checkout <编译分支>            # 实录曾用 crc32c-isa-l-sve2-fastpath-3fs（含 3FS 本地修改）
# git cherry-pick <commit-hash>   # 从上游优化分支（如 crc32c-isa-l-sve2-fastpath）拣选新 commit
```

**folly 本地 patch 清单**（来自 3FS `patches/folly.patch`，编译分支若从裸上游分支创建，
需先 cherry-pick 包含这些修改的 commit，否则 3FS 无法编译/运行）：

- CMake：C++20 标准、disable libunwind、jemalloc/mimalloc 支持
- ConcurrentHashMap：aarch64 兼容性修复
- Coroutine：3FS 异步框架支持
- Expected/Optional/StampedPtr：编译器兼容性
- Logging：Hf3fsLogFormatter
- OrderedMap：新容器
- ThreadPoolExecutor/UnboundedBlockingQueue：线程池调优
- ServerSocket：coroutine socket 改进
- 各种 aarch64/gcc 兼容性测试修复

## 4. 构建前置杂项（首次编译需要）

```shell
# 1. fast_float 头文件（新版 folly 依赖）
cd /tmp && git clone --depth=1 https://github.com/fastfloat/fast_float.git
cp -r /tmp/fast_float/include/fast_float /usr/local/include/

# 2. clang-format-14 软链接（3FS CMake 要求）
ln -sf /usr/bin/clang-format /usr/bin/clang-format-14

# 3. clang++-14 软链接（Rust cargo 链接器要求，指向毕昇 clang++）
ln -sf /home/BiShengCompiler-4.2.0-aarch64-linux/bin/clang++ /usr/bin/clang++-14
```

4. cargo 代理（`/root/.cargo/config.toml`）：如果 USTC 等镜像超时，需要配置代理：

   ```toml
   [http]
   proxy = "127.0.0.1:<本地代理端口>"
   ```

## 5. 三处必需 CMake 修改

以下三处在实录环境（毕昇 clang 17 + 新版 folly）为**必需**，否则编译失败：

### 修改 1：zstd include 目录用 BUILD_INTERFACE 包装

文件：`${SRC_ROOT}/3fs/CMakeLists.txt`。
问题：新版 folly CMake 对 INTERFACE_INCLUDE_DIRECTORIES 做严格检查，源目录路径会被拒绝。

```cmake
# 原始:
set(zstd_INCLUDE_DIRS "${PROJECT_SOURCE_DIR}/third_party/zstd/lib")
set(ZSTD_INCLUDE_DIR "${PROJECT_SOURCE_DIR}/third_party/zstd/lib")

# 修改为:
set(zstd_INCLUDE_DIRS "$<BUILD_INTERFACE:${PROJECT_SOURCE_DIR}/third_party/zstd/lib>")
set(ZSTD_INCLUDE_DIR "$<BUILD_INTERFACE:${PROJECT_SOURCE_DIR}/third_party/zstd/lib>")
```

### 修改 2：删除 Clang 分支的 -fcoroutines-ts

文件：`${SRC_ROOT}/3fs/CMakeLists.txt`。
问题：毕昇 clang 17 不支持已废弃的 `-fcoroutines-ts`；且 `-fcoroutines` 在 C++20 模式下是
unused argument，被 `-Werror` 捕获导致编译失败。

```cmake
# 原始:
if (CMAKE_CXX_COMPILER_ID STREQUAL "Clang")
    set (CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -fcoroutines-ts")
    set(CMAKE_CXX_LINK_FLAGS "${CMAKE_CXX_LINK_FLAGS} -latomic")

# 修改为（删除 -fcoroutines-ts 行）:
if (CMAKE_CXX_COMPILER_ID STREQUAL "Clang")
    set(CMAKE_CXX_LINK_FLAGS "${CMAKE_CXX_LINK_FLAGS} -latomic")
```

### 修改 3：rocksdb 关闭 FAIL_ON_WARNINGS

问题：rocksdb 代码在新编译器下产生 `-Wunused-but-set-variable` 警告，被 rocksdb 自身的 `-Werror` 捕获。
修复：cmake 配置时加 `-DFAIL_ON_WARNINGS=OFF`（见 §6 完整配置命令）。

## 6. CMake 配置与编译

### 6.1 标准配置（首次全新编译，安装手册版）

```shell
cd ${SRC_ROOT}/3fs
cmake -S . -B build -DCMAKE_CXX_COMPILER=clang++ -DCMAKE_C_COMPILER=clang \
      -DCMAKE_BUILD_TYPE=RelWithDebInfo -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
cmake --build build -j
```

### 6.2 完整配置（重编译版，显式编译器路径 + 修改 3）

```shell
source <毕昇环境脚本>      # export PATH=/home/BiShengCompiler-4.2.0-aarch64-linux/bin:$PATH
source <代理脚本>          # 如需

cmake -S ${SRC_ROOT}/3fs -B ${BUILD_DIR} \
  -DCMAKE_BUILD_TYPE=RelWithDebInfo \
  -DCMAKE_CXX_COMPILER=/home/BiShengCompiler-4.2.0-aarch64-linux/bin/clang++ \
  -DCMAKE_C_COMPILER=/home/BiShengCompiler-4.2.0-aarch64-linux/bin/clang \
  -DENABLE_FUSE_APPLICATION=ON \
  -DFAIL_ON_WARNINGS=OFF
```

### 6.3 重编译前备份旧产物

```shell
BACKUP_NAME="build_backup_$(date +%Y%m%d_%H%M%S)"
mkdir -p ${SRC_ROOT}/3fs/${BACKUP_NAME}
cp -a ${BUILD_DIR}/bin/{hf3fs_fuse_main,storage_main,mgmtd_main,meta_main,monitor_collector_main,admin_cli} \
      ${SRC_ROOT}/3fs/${BACKUP_NAME}/
echo "Backup: ${SRC_ROOT}/3fs/${BACKUP_NAME}/"
```

### 6.4 分目标编译（迭代开发）

```shell
# 先编 folly（切换 folly 分支后）
cmake --build ${BUILD_DIR} -j$(nproc) --target folly

# 再编 3FS 主要二进制
cmake --build ${BUILD_DIR} -j$(nproc) --target \
  hf3fs_fuse_main mgmtd_main meta_main monitor_collector_main storage_main admin_cli

# 或单个目标（如 mgmtd_main）
cd ${SRC_ROOT}/3fs && cmake --build build --target mgmtd_main -j$(nproc)
```

### 6.5 产物检查

```shell
ls -lh ${BUILD_DIR}/bin/
# 应包含: admin_cli mgmtd_main meta_main storage_main monitor_collector_main hf3fs_fuse_main 等
ls -lh ${BUILD_DIR}/src/lib/api/libhf3fs_api_shared.so
```

## 7. USRBIO（hf3fs_usrbio.so）与 fio 编译

用于原生客户端（USRBIO，绕过 FUSE 内核）的 fio 性能测试。

1. 编译安装 fio：

   ```shell
   cd ${SRC_ROOT}
   git clone https://github.com/axboe/fio.git      # 实录已提前克隆到 ${SRC_ROOT}/fio
   cd fio
   ./configure
   make
   make install
   ```

2. 编译 USRBIO 插件：

   ```shell
   cd ${SRC_ROOT}/3fs/benchmarks/fio_usrbio
   make HF3FS_LIB_DIR=${HF3FS_LIB_DIR} HF3FS_INCLUDE_DIR=${HF3FS_INCLUDE_DIR} FIO_SRC_DIR=${FIO_SRC_DIR}
   ```

   路径参数（按实际替换）：

   | 变量 | 默认路径 | 说明 |
   |------|---------|------|
   | `HF3FS_LIB_DIR` | `${SRC_ROOT}/3fs/build/src/lib/api` | libhf3fs_api_shared.so 所在目录 |
   | `HF3FS_INCLUDE_DIR` | `${SRC_ROOT}/3fs/src/lib/api` | USRBIO 头文件目录 |
   | `FIO_SRC_DIR` | `${SRC_ROOT}/fio` | fio 源码主目录（须包含 config-host.h，即已 ./configure） |

   示例：

   ```shell
   make HF3FS_LIB_DIR=${SRC_ROOT}/3fs/build/src/lib/api \
        HF3FS_INCLUDE_DIR=${SRC_ROOT}/3fs/src/lib/api \
        FIO_SRC_DIR=${SRC_ROOT}/fio
   ```

3. 产物确认：

   ```shell
   ls -l ${SRC_ROOT}/3fs/benchmarks/fio_usrbio/hf3fs_usrbio.so
   ```

部署时 `hf3fs_usrbio.so` 分发到 FUSE 客户端 `/usr/lib64/`、fio 分发到客户端
（实录 stage 集群放 `/usr/local/bin/fio`，tester 集群放 `${SRC_ROOT}/fio/fio`），见
`installation-guide.md` §5.9 与 `fuse-client-expansion.md` §7。
