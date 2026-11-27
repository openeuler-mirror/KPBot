# 构建与三方依赖

源码根 `${OB_SRC}`。OceanBase 不用系统自带编译器直接编，而是用 `deps/init` 固定一套
工具链与三方库，保证 ABI/指令集/优化参数一致。

## 1. 三方依赖从哪来

```
${OB_SRC}/deps/init/
  ├─ dep_create.sh                  # 识别发行版/架构，下载并安装 RPM
  ├─ oceanbase.el7.{aarch64,x86_64}.deps
  ├─ oceanbase.el8.{aarch64,x86_64}.deps
  ├─ oceanbase.el9.{aarch64,x86_64}.deps
  └─ oceanbase.al8.{aarch64,x86_64}.deps
```

`./build.sh init` 调用 `deps/init/dep_create.sh`，按 `/etc/os-release` 选择清单，把包安装到
`${OB_SRC}/deps/3rd/usr/local/oceanbase/`：

```
deps/3rd/usr/local/oceanbase/
  ├─ deps/devel/        # 三方库头文件/静态库/动态库
  └─ devtools/          # cmake、clang、ld.lld、binutils、bison、flex、ccache（清单无 ninja；make 来自系统包）
```

> 依赖是**按 OS 大版本 × 架构**固定版本；换 OS（el7/el8/el9/al8）或换架构必须换清单，
> 不要混装不同清单的包。

## 2. 依赖作用（按用途分组）

| 依赖 | 作用 | 与性能/正确性的关系 |
| --- | --- | --- |
| `devdeps-protobuf-c` | RPC/序列化 | 版本不一致会导致协议不兼容；序列化是 sql_nio/ApplySrv 热点之一 |
| `devdeps-boost` | 基础库（容器、线程、文件系统等） | 与编译器 ABI 绑定 |
| `devdeps-isa-l-static` | Intel ISA-L（CRC、压缩等） | 校验/压缩路径；ARM 上收益有限，需实测 |
| `devdeps-openssl-static` | TLS/摘要/加解密 | 静态链接避免运行时版本漂移 |
| `devdeps-libcurl-static` | HTTP 客户端（备份/对象存储等） | 静态链接避免系统 curl 差异 |
| `devdeps-mariadb-connector-c` | MySQL 协议客户端库 | obclient/内部连接 |
| `devdeps-libaio` | 异步 IO | 直接影响 IO 路径与延迟 |
| `devdeps-relaxed-rapidjson` | JSON 解析 | 配置/审计等 |
| `devdeps-zlib-static` / `xz` / `ncurses-static` | 压缩/终端 | 工具与日志 |
| `devdeps-libxml2` / `mxml` | XML | 配置/兼容 |
| `devdeps-apr` | Apache Portable Runtime | 部分工具/库依赖 |
| `devdeps-lua` | Lua（存储过程/扩展） | PL 引擎 |
| `devdeps-abseil-cpp` | 基础容器/字符串 | 编译期依赖 |
| `devdeps-s2geometry` | 地理几何 | GIS |
| `devdeps-icu` | 国际化/字符集 | 排序/比较（collation）行为 |
| `devdeps-roaringbitmap-croaring` | 位图 | 索引/过滤 |
| `devdeps-apache-arrow` / `apache-orc` | 列式格式 | 分析/外表 |
| `devdeps-fast-float` | 浮点解析 | 类型转换 |
| `devdeps-oss-c-sdk` / `cos-c-sdk` / `s3-cpp-sdk` / `hdfs-sdk` | 对象存储/大数据 | 备份、外表、归档 |
| `devdeps-vsag` | 向量检索 | 向量索引 |
| `devdeps-gtest` | 单测 | `ENABLE_TESTING`/unittest |
| `devdeps-oblogmsg` | 日志格式化 | 日志路径 |
| `devdeps-rocksdb` | 部分组件的 KV 存储 | 与 OB 自身存储不同，注意别混淆 |
| `obstack` / `obshell` / `obdal` | 运维/部署/数据访问组件 | 部署工具链 |
| `ob-deploy` / `obclient` / `libobclient` | 部署与客户端 | 验收与压测连接 |

工具链：

| 工具 | 版本（清单示例） | 作用 |
| --- | --- | --- |
| `obdevtools-llvm` | 11.0.1（C++11）/ 17.0.3（C++20） | 主编译器（clang/clang++）+ lld |
| `obdevtools-gcc` | 9.3.0 / 12.3.0 | `--gcc-toolchain` 提供的 libstdc++/头文件 |
| `obdevtools-cmake` | 3.30.x | 构建 |
| `obdevtools-binutils` | 2.30 | ld/objdump 等 |
| `obdevtools-bison/flex` | 2.4.1 / 2.5.35 | 解析器生成 |
| `obdevtools-ccache` | 3.7.x | 编译缓存 |

## 3. 编译模式（build.sh）

| 模式 | 关键 CMake 参数 | 用途 | 注意 |
| --- | --- | --- | --- |
| `debug` | `CMAKE_BUILD_TYPE=Debug` | 开发/定位 | 性能不可用于对比 |
| `release` | `RelWithDebInfo` + lld | 常规性能基线 | 保留调试信息 |
| `release_no_unity` | 关闭 unity build | 改单文件/调试 | 编译更慢 |
| `perf` | `ENABLE_AUTO_FDO=ON`、`ENABLE_THIN_LTO=ON`、`ENABLE_HOTFUNC=ON`、`ENABLE_BOLT_AUTO=ON`（开源树下 BOLT 恒不生效，见下方说明） | 性能构建 | 必须记录参数 |
| `rpm` / `deb` / `package` | `OB_BUILD_PACKAGE=ON`、`ENABLE_AUTO_FDO=ON`、`ENABLE_THIN_LTO=ON`、`ENABLE_HOTFUNC=ON`、`ENABLE_BOLT`（该版本 package 分支显式置 OFF）、静态 LGPL | 发布包 | 产物用于交付 |
| `release_asan` / `debug_asan` | `OB_USE_ASAN=ON` | 内存问题 | 性能不可比 |
| `errsim` / `errsim_debug` | `OB_ERRSIM=ON` | 故障注入 | 仅测试 |
| `dissearray` | `OB_DIS_SEARRAY=ON` | 排查 SEArray | 仅测试 |
| `sanity` | `ENABLE_SANITY=ON` | 静态/运行检查 | 仅测试 |

> **BOLT 在 4.3.5 开源树不生效**：`cmake/Env.cmake` 无条件 `OB_BUILD_OPENSOURCE ON`，
> `ENABLE_BOLT` 的启用条件含 `NOT OB_BUILD_OPENSOURCE`，故 `BOLT_OPT` 恒为空、
> `--emit-relocs` 与 BOLT 重排不会进入产物；`build.sh` 的 package 分支还显式置
> `ENABLE_BOLT=OFF`。只有内部（非开源）构建才可能真正启用。记录编译身份时不要把
> “BOLT=on”当作已生效的优化；perf 模式收益只算 LTO/AutoFDO/HOTFUNC/LSE。

常用开关：

| 开关 | 默认 | 说明 |
| --- | --- | --- |
| `OB_USE_LLD` | ON（el6 发行版，即 `/etc/issue` 含 `release 6` 时自动 OFF；与内核版本无关） | 使用 `ld.lld`；开启后启用 `--gc-sections`、hotfunc 排序 |
| `ENABLE_AUTO_FDO` | perf/rpm 开 | 用 `profile/observer-{aarch64,x86_64}.prof` 做采样反馈 |
| `ENABLE_THIN_LTO` | perf/rpm 开 | `-flto=thin`，链接并行与缓存 |
| `ENABLE_HOTFUNC` | perf/rpm 开 | 用 `profile/hotfuncs-{arch}.txt` 做符号排序 |
| `ENABLE_BOLT` / `ENABLE_BOLT_AUTO` | package/perf（开源树恒不生效；package 模式显式 OFF） | 内部构建才启用 `-Wl,--emit-relocs` + BOLT 重排；开源树为空操作 |
| `OB_DISABLE_LSE` | OFF | ON 时 `-march=armv8-a+crc`（NOLSE）；OFF 时 `+lse` |
| `OB_ENABLE_UNITY` | ON | 合并编译单元，编译快但定位难 |
| `OB_ENABLE_MCMODEL` | 版本相关 | `-mcmodel=large`，大地址空间 |
| `CPP_STANDARD_20` | OFF | ON 需 LLVM ≥ 17 |
| `OB_STATIC_LINK_LGPL_DEPS` | 全模式默认 ON（`cmake/Env.cmake`）；build_package 再显式传 ON | 静态链接 LGPL 依赖，便于分发 |

> LSE/NOLSE 必须与三方库、工具链、目标 CPU 一致。实测在锁争用不明显的 sysbench 场景，
> NOLSE 可比 LSE 高 2%~5%；锁争用严重的业务则相反。切换前后要记录并做 A/B。

## 4. 性能构建的参数含义

| 参数 | 机制 | 预期收益 | 风险 |
| --- | --- | --- | --- |
| `-march=armv8-a+crc+lse` | 使用 LSE 原子指令（ldadd/cas/swp） | 高争用场景减少 outline atomics | 低版本 CPU/库不支持 |
| `-march=armv8-a+crc`（NOLSE） | 回退 LL/SC 原子序列 | 低争用场景实测更好 | 高争用可能更差 |
| Thin LTO | 跨模块内联/去虚 | 读路径小幅 | 链接时间、内存 |
| AutoFDO（`.prof`） | 采样反馈优化分支/内联 | 前端 Bound 下降 | profile 与业务不匹配会负收益 |
| HOTFUNC（符号排序） | 热函数聚集，减少 iTLB/icache miss | 1%~3% | 排序文件与版本不符 |
| BOLT | 链接后基本块重排 | 开源树不生效（无收益可计）；仅内部构建可用 | 需 `--emit-relocs`，调试复杂 |
| （无 PGO/CSPGO 开关） | 该版本构建系统不存在 PGO/CSPGO 机制 | — | 如需类似效果只能自行改 CFLAGS，非 build.sh/Env.cmake 开关 |
| `-mtune=...` | 调度/流水线 | 视热点 | 实测 `neoverse-n2`/`tsv110` 有负收益案例 |

## 5. 产物与校验

```
ls -lh "${BUILD_DIR}"/src/observer/observer
md5sum "${BUILD_DIR}"/src/observer/observer
readelf -p .comment "${BUILD_DIR}"/src/observer/observer | head
objdump -d "${BUILD_DIR}"/src/observer/observer | grep -c -E '\b(ldadd|ldclr|ldset|swp|cas)[a-z]*\b'  # LSE 指令计数
strings "${BUILD_DIR}"/src/observer/observer | grep -m1 -E 'clang version|LLVM'
```

性能对比前必须记录：编译器版本、`-march`、LSE/NOLSE、LTO/AutoFDO/HOTFUNC/BOLT 状态、
依赖清单文件、二进制 md5。

## 6. 常见编译问题

| 症状 | 原因 | 处置 |
| --- | --- | --- |
| `Your workspace has not initialized dependencies` | 未 `init` | `./build.sh init` |
| `cmake: command not found` | 未用 devtools 的 cmake | 用 `deps/3rd/.../devtools/bin/cmake` 或 build.sh |
| clang 版本不满足 C++20 | LLVM < 17 | 用 C++11（默认）或安装 LLVM 17 清单 |
| `cannot find -lstdc++` / `-latomic` | 缺静态库/工具链路径 | 检查 gcc9/gcc12 与 `ARCH_LDFLAGS` |
| `undefined reference` 到 LSE 符号 | 目标 CPU/库不支持 LSE | 改 `OB_DISABLE_LSE=ON` 重编，或换匹配依赖 |
| 链接 OOM/超时 | Thin LTO 并行过高/内存不足 | 降 `-j`，调 LTO cache/jobs |
| 运行即 SIGILL | `-march` 与 CPU 不符 | 用目标机支持的 `-march` 重编 |
| RPM 打包缺依赖 | 静态/动态链接策略不一致 | 用 `rpm` 模式统一 `OB_STATIC_LINK_LGPL_DEPS` |

## 7. 最小构建记录模板

```
source:  ${OB_SRC} @ <commit/tag>
deps:    deps/init/oceanbase.<os>.<arch>.deps @ <sha256>
toolchain: clang <ver>, lld <ver>, gcc-toolchain <ver>
mode:    release|perf|rpm, unity=<on/off>
flags:   -march=<...>, LSE=<on/off>, LTO=<thin/none>, AutoFDO=<on/off>,
         HOTFUNC=<on/off>, BOLT=<on/off，开源树恒不生效>; 无 PGO/CSPGO 开关
binary:  md5=<...>, size=<...>
```
