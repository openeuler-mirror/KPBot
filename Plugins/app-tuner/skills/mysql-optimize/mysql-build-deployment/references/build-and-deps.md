# 构建、依赖与编译选项

## 1. 依赖作用

| 依赖 | 作用 | 缺失/不匹配后果 |
| --- | --- | --- |
| GCC/G++ | C/C++ 编译器 | 无法编译；版本过低不支持 C++17 |
| CMake（Linux 上游下限 3.5.1；实测基线 3.27.9） | 构建系统 | 低于 3.5.1 时 CMake 直接拒绝配置；3.22 只是实测环境自带版本，不是 8.0 依赖 |
| Boost 1.73.0 | MySQL 8.0 指定 Boost；用于部分模板/几何/解析依赖 | 配置直接失败；版本不符会报找不到 Boost |
| OpenSSL 1.1/3.x | TLS、加密函数、认证插件 | SSL 相关编译/运行失败 |
| ncurses-devel | `mysql` 命令行客户端 | 客户端无法编译 |
| libaio-devel | InnoDB native AIO | `innodb_use_native_aio` 不可用或性能下降 |
| libtirpc-devel / rpcgen | RPC/NIS 相关头文件 | 编译报缺头文件 |
| cyrus-sasl-devel / openldap-devel | SASL/LDAP 认证插件 | 插件缺失 |
| numactl | NUMA 绑定、`numactl -C/-m` | 无法按 NUMA 部署/验证 |
| libcgroup | cgroup 管理（可选） | 资源限制脚本不可用 |
| gdb / pstack / sysstat | 调试与采集 | 故障定位降级 |
| dwarves / elfutils-libelf-devel | BTF/调试信息工具（内核/性能） | 部分 profiling 工具不可用 |

CMake 两个口径分开看：上游 `CMakeLists.txt:112` 的 Linux 下限是 3.5.1（macOS 分支才
提高到 3.15.3/3.18），低于 3.5.1 才会拒绝配置；实测基线 3.27.9 来自 openEuler 24.03 SP2
自带版本，不要因为未达 3.22 而强行升级 CMake。

安装示例（openEuler/RHEL 系，按实际源调整）：

```
yum install -y gcc gcc-c++ cmake m4 make automake flex bison \
  ncurses-devel openssl-devel libtirpc-devel rpcgen libaio-devel \
  cyrus-sasl-devel openldap-devel numactl libcgroup gdb sysstat \
  dwarves elfutils-libelf-devel
```

## 2. 源码包与校验

```
sha256sum "${SRC_ROOT}/mysql-8.0.25.tar.gz"
sha256sum "${SRC_ROOT}/boost_1_73_0.tar.gz"
# 与官方/内部发布值比对；不一致立即停止
tar -xzf "${SRC_ROOT}/mysql-8.0.25.tar.gz" -C "${SRC_ROOT}"
tar -xzf "${SRC_ROOT}/boost_1_73_0.tar.gz" -C "${SRC_ROOT}"
```

禁止跳过校验或把下载内容直接交给 shell 执行。

## 3. 基线构建

```
mkdir -p "${BUILD_DIR}" && cd "${BUILD_DIR}"
cmake "${MYSQL_SRC}" \
  -DCMAKE_INSTALL_PREFIX="${MYSQL_HOME}" \
  -DWITH_BOOST="${BOOST_ROOT}" \
  -DWITH_BLACKHOLE_STORAGE_ENGINE=0 \
  -DWITH_FEDERATED_STORAGE_ENGINE=0 \
  -DWITH_ARCHIVE_STORAGE_ENGINE=0 \
  -DDISABLE_PSI_MUTEX=0 \
  -DWITH_MYSQLX=OFF \
  -DFORCE_INSOURCE_BUILD=1
make -j"$(nproc)"
make install
```

说明：

- `-DFORCE_INSOURCE_BUILD=1` 允许在源码树内构建；若使用独立 build 目录可去掉。
- 关闭不用的存储引擎减少编译时间与攻击面；确认业务不需要 ARCHIVE/FEDERATED/BLACKHOLE。
- `DISABLE_PSI_MUTEX=0` 保留 mutex instrumentation；完全关闭可降开销但失去等待证据。
- `WITH_MYSQLX=OFF` 关闭 X Protocol；如业务使用 X DevAPI 需保留。

## 4. 优化构建（aarch64）

```
CFLAGS="-O3 -march=armv8-a+crypto+lse+crc -flto=auto -funroll-loops -DNDEBUG"
CXXFLAGS="$CFLAGS"
cmake "${MYSQL_SRC}" \
  -DCMAKE_INSTALL_PREFIX="${MYSQL_HOME}" -DWITH_BOOST="${BOOST_ROOT}" \
  -DCMAKE_C_FLAGS="$CFLAGS" -DCMAKE_CXX_FLAGS="$CXXFLAGS" \
  -DCMAKE_C_FLAGS_RELWITHDEBINFO="$CFLAGS" \
  -DCMAKE_CXX_FLAGS_RELWITHDEBINFO="$CXXFLAGS" \
  -DWITH_BLACKHOLE_STORAGE_ENGINE=0 -DWITH_FEDERATED_STORAGE_ENGINE=0 \
  -DWITH_ARCHIVE_STORAGE_ENGINE=0 -DDISABLE_PSI_MUTEX=0 \
  -DWITH_MYSQLX=OFF -DFORCE_INSOURCE_BUILD=1
```

- `+lse` 使用 LSE 原子指令，减少 outline atomics；CPU 必须支持 LSE（Kunpeng 920+ 支持）。
- `+crc` 使用 ARMv8 CRC 指令，加速 crc32 校验；需确认硬件支持。
- `-flto=auto` 增加编译时间/内存；链接失败先回退 LTO。
- `-funroll-loops` 收益依场景；不要与基线混用比较。
- x86_64 不要照搬 `-march=armv8...`；按平台选择 `-march=x86-64-v3` 等并单独验证。

## 5. 产物与身份记录

```
"${MYSQL_HOME}/bin/mysqld" --version
md5sum "${MYSQL_HOME}/bin/mysqld"
ldd "${MYSQL_HOME}/bin/mysqld" | grep -E 'ssl|crypto|aio|numa'
readelf -A "${MYSQL_HOME}/bin/mysqld" | head -40   # aarch64 特性
```

记录：源码版本/commit、Boost/OpenSSL 版本、CMake 参数、GCC 版本、md5、是否 LTO/LSE。

## 6. 常见编译问题

| 症状 | 处置 |
| --- | --- |
| 找不到 Boost | 确认 `-DWITH_BOOST` 指向解压后的 `boost_1_73_0` 根目录 |
| CMake 版本过低 | 低于 Linux 下限 3.5.1 才需升级（实测基线 3.27.9 不是上游要求） |
| `cannot find -lstdc++` | 安装 `libstdc++-static` 或对应开发包 |
| OpenSSL 头/库不匹配 | 统一 OpenSSL 版本与 `pkg-config` 路径 |
| LTO 链接 OOM/失败 | `-flto=auto` 改 `-flto` 或关闭；增加内存 |
| `-march` 非法 | 确认 GCC 支持该扩展，下降为 `armv8-a+crc` 验证 |
| 编译极慢 | 降低 `-j`，LTO 本身耗时；分模块排查 |
| 运行报 GLIBC 版本 | 构建机 glibc 高于目标机；在目标 OS 构建或使用兼容工具链 |
