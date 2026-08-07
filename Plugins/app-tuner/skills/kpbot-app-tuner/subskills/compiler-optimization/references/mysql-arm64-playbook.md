# MySQL ARM64 Compiler Playbook

## Scope

MySQL 在 ARM/aarch64 上出现原子、CRC、锁竞争、CMake flag 覆盖或构建期优化问题时读取本文件。

## Pre-flight

开始重编译或补丁前必须检查：

```bash
grep -rnI '<<<<<<<' <mysql-src> | wc -l
gcc --version | head -1
grep 'CMAKE_C.*FLAGS' <build>/CMakeCache.txt
readelf -A <mysqld> 2>/dev/null | grep -E 'CRC|ATOMIC|FP'
objdump -d <mysqld> | grep -c 'ldaxr\|stlxr'
objdump -d <mysqld> | grep -c 'casal\|swpal\|ldaddal'
```

发现 merge conflict、源码目录不明、构建目录指向错误或目标实例不是该二进制时，停止并报告。

## LSE Atomics

MySQL 8.0.25 的 CMake 默认不一定启用 `-moutline-atomics`。若 ARM64 平台热点落在原子、mutex、spin 或 InnoDB 竞争路径：

1. 检查 CPU 是否支持 LSE：`lscpu | grep -i atomics` 或 `/proc/cpuinfo Features`。
2. 检查当前二进制是否有 LSE 指令：`casal|swpal|ldaddal`。
3. 若 CPU 支持但二进制无 LSE，候选动作可使用 `patches/mysql-8.0.25-lse-outline-atomics.patch`。
4. 编译后验证 CMake 日志含 `outline-atomics`，且二进制 LSE 指令数量明显大于 0。

不要只因为指令生成成功就宣称整体收益；目标热点占比低于 1% 时，预期收益应标低并要求小轮次验证。

## CRC32C

MySQL 8.0.25 的 ARM64 CRC32C 路径可能落到软件实现。若热点出现 `ut_crc32_byte_by_byte`、`crc32*_sw` 或 checksum 相关函数：

- 硬件层：确认 `crc32` flag。
- 编译层：确认 `-march=armv8.x-a+crc` 或等价 `-mcpu`。
- 代码层：检查是否已有 ARM64 intrinsics 或 runtime dispatch。
- 对 8.0.33+ 的 ARM64 CRC32C 实现 backport 只能作为源码候选，必须经过二进制等价门控。

## CMake And Build Pitfalls

| 问题 | 现象 | 处理 |
|---|---|---|
| Release/RelWithDebInfo 覆盖 | 用户传 `-O3`，CMakeCache 仍为 `-O2` | 显式设置 `CMAKE_C_FLAGS_RELEASE` / `CMAKE_CXX_FLAGS_RELEASE` |
| GCC < 12 大工程 LTO | 二进制膨胀、链接 OOM 或启动异常 | 禁用 LTO 或升级 GCC 后小轮次验证 |
| `-moutline-atomics` 兼容性 | 特定旧平台 dispatch 异常 | 先用 `+lse` 单独验证，再评估 outline |
| 多源码目录 | 构建目录指向旧源码 | 检查 `CMAKE_SOURCE_DIR`、二进制 hash 和 `/proc/<pid>/exe` |
| Boost 目录 | 构建失败或链接错库 | 确认 `-DWITH_BOOST` 指向正确路径 |

## Binary Equivalence Gate

MySQL 候选二进制上线前记录：

- 源码 tag/commit、补丁路径、构建目录和完整构建日志。
- `mysqld --version`、二进制 hash、链接库、启动参数和配置文件差异。
- 启动后 `/proc/<pid>/exe`、`cmdline`、`maps`、监听端口。
- smoke test：连接、认证、最小查询、错误日志、低风险 warmup。
- 回退：停止候选实例，恢复基线二进制和配置，重新验证目标实例身份。

若同时改变编译选项、MySQL 配置、数据目录、NUMA/cpuset 或运行库，本轮标记 `confounded_binary_test`。
