# 编译优化、NVMe 原子写与第三方

## 1. 编译基线

```
# 在源码顶层目录执行；先定义公共参数，再显式传给 C 与 C++（mysqld 主体是 C++）
COMMON_FLAGS="-O3 -march=armv8-a+crypto+lse+crc -flto=auto -funroll-loops -DNDEBUG"
cmake -S . -B build \
  -DCMAKE_C_FLAGS="${COMMON_FLAGS}" \
  -DCMAKE_CXX_FLAGS="${COMMON_FLAGS}" \
  -DCMAKE_BUILD_TYPE=RelWithDebInfo
```

不要依赖 `$CFLAGS`/`$CXXFLAGS`：shell 未定义时 `-DCMAKE_CXX_FLAGS="$CFLAGS"` 会是空串，
mysqld 的 C++ 代码就拿不到示例中的 LSE/LTO 选项；必须显式把同一个公共变量传给两者。

| 选项 | 作用 | 风险 |
| --- | --- | --- |
| `-O3` | 更激进优化 | 编译慢；个别代码可能变慢 |
| `+lse` | LSE 原子指令，减少 outline atomics | CPU 必须支持；旧 CPU 非法指令 |
| `+crc` | ARMv8 CRC 指令 | 需硬件支持；语义不变 |
| `+crypto` | 加密扩展 | 需硬件支持 |
| `-flto=auto` | 跨模块优化 | 链接失败/内存/时间 |
| `-funroll-loops` | 循环展开 | 代码膨胀/i-cache |
| `-DNDEBUG` | 关闭 assert | 不能用于 debug 定位 |

注意：

- 编译收益必须与基线分开 A/B；不能与 my.cnf 改动混在一起。
- `-march` 是平台相关：x86_64 用 `-march=x86-64-v3` 等，不能照搬 armv8。
- LTO 失败先回退，不要在生产交付未验证的构建。
- 记录 GCC/binutils/glibc/CMake 版本与 md5。

## 2. 构建产物校验

```
"${MYSQL_HOME}/bin/mysqld" --version
md5sum "${MYSQL_HOME}/bin/mysqld"
ldd "${MYSQL_HOME}/bin/mysqld" | grep -E 'ssl|crypto|aio|numa'
readelf -A "${MYSQL_HOME}/bin/mysqld" | head -40
# LSE 指令（aarch64）：
objdump -d "${MYSQL_HOME}/bin/mysqld" 2>/dev/null | grep -cE '\b(ldadd|ldclr|ldset|ldeor|swp)' || true
```

- 用 `scripts/verify_build_opts.sh` 做只读核对：版本检查同时看 `--version` 的退出码与输出，
  `ldd` 执行失败或缺少动态库均判 FAIL，关键检查失败时脚本返回非零（不能只留 WARN 仍返回 0）。
- 若二进制被 strip，符号/源码行号缺失，profiling 会退化；保留独立符号包。

## 3. NVMe 原子写

原理：支持原子写的 SSD 保证单 IO 要么全写、要么全失败；MySQL 可关闭 doublewrite，
减少一次写。

前提（必须逐项确认）：

- 先枚举实例内**所有** InnoDB 数据文件的真实路径并逐一核对整页原子写保证：
  系统表空间（`innodb_data_home_dir`/`innodb_data_file_path`）、独立表空间
  （`innodb_file_per_table`，可能分布在多个目录/挂载点）、undo 表空间
  （`innodb_undo_directory`）、临时表空间（`innodb_temp_data_file_path`）、
  通用表空间（`CREATE TABLESPACE ... ADD DATAFILE` 指定的路径），含符号链接与实际挂载点。
  对每条路径核对设备/固件/文件系统/页大小/O_DIRECT 的整页原子写保证。
- SSD 支持原子写（如 ES3000 系列，固件版本满足厂商要求）。
- 文件系统/页大小满足要求（示例：ext4 `bigalloc -C 16384` + 16K 页）。
- `innodb_flush_method=O_DIRECT`。
- 业务能接受“硬件/固件/文件系统绑定”的运维约束。

**任一路径无法确认整页原子写 → 不要关闭 doublewrite，保持 `innodb_doublewrite=1`（默认）。**
数据目录在原子写盘、独立表空间在普通盘时，后者同样失去撕裂页恢复保护；
只验证数据目录不足以做这个决定。doublewrite 保护的是实例内全部 InnoDB 数据文件，
不是某一个表空间。

配置（仅在上列前提全部满足后）：

```
[mysqld]
innodb_flush_method=O_DIRECT
innodb_doublewrite=0
```

验证：

```
# 厂商工具查询原子写状态（命令以实际产品为准）
# hioadm atomicwrite -d <device>
mysql -e "SHOW VARIABLES LIKE 'innodb_doublewrite';"
mysql -e "SHOW VARIABLES LIKE 'innodb_flush_method';"
```

风险：

- 任一表空间路径条件不满足却关闭 doublewrite → 页撕裂可能导致数据损坏。
- 更换盘/文件系统/页大小后必须重新确认。
- 回滚：恢复 `innodb_doublewrite=1`，重启，做数据校验/备份。

## 4. 第三方与分配器

| 项 | 说明 | 建议 |
| --- | --- | --- |
| OpenSSL | TLS/加密 | 与系统/编译一致；升级需回归 |
| Boost | 编译依赖 | 固定 1.73.0（8.0） |
| jemalloc/tcmalloc | 分配器替换 | 非默认；ABI/行为差异，需 A/B |
| 压缩库（zstd/lz4） | 压缩表/binlog | 按业务；CPU 换空间 |
| 监控/agent | performance_schema/插件 | 注意开销 |

原则：第三方全量升级没有明确收益时不要做；升级必须记录版本、ABI、数据格式兼容性。

## 5. 编译/第三方 A/B 清单

- 同一源码 commit、同一依赖版本（除被测项）
- 同一 CMake 参数（除被测项）
- 同一 my.cnf/数据/绑核/压测口径
- MTR/冒烟通过
- 四场景 ≥3 轮，波动 <5%
- 记录 md5、LSE 指令数、库版本
- 负收益回滚并记录

## 6. 常见问题

| 症状 | 处置 |
| --- | --- |
| 启动 `Illegal instruction` | `-march` 超出 CPU 支持；回退 |
| LTO 链接失败/OOM | 关闭 LTO 或降低并行 |
| 运行报 GLIBC 版本 | 构建机 glibc 高于目标；在目标 OS 构建 |
| 原子写工具查不到 | 盘/固件不支持；不要关闭 doublewrite |
| 关 doublewrite 后崩溃恢复异常 | 立即恢复 doublewrite，备份/校验数据 |
| 分配器替换后崩溃 | 回退系统 malloc；检查 ABI/链接顺序 |
