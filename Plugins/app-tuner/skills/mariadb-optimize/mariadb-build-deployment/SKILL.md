---
name: mariadb-build-deployment
description: >
  MariaDB 10.11.16（兼容 10.3.x 案例）在鲲鹏/ARM64（aarch64）与 x86_64 上的编译、部署、
  数据装载、备份恢复与升级回滚：三方依赖及各自作用、CMake 选项与 LSE/jemalloc 构建、
  初始化与 systemd/多实例、NUMA 绑核与大页、端口与目录规划、配置模板、TPC-C 数据装载、
  验收检查、mariabackup 全量备份/恢复、二进制滚动替换与回滚。内容来自鲲鹏 4P 与 KVM
  虚拟化实测部署实录，已参数化脱敏。用于从零搭建、复制部署、扩容、升级或排查部署问题；
  不适用参数调优（→ mariadb-performance-optimization）、压测与正确性验证
  （→ mariadb-test-verification）、热点归因（→ mariadb-hotspot-analysis）。
  触发词：MariaDB 编译、MariaDB 部署、MariaDB 安装、MariaDB 多实例、MariaDB 主从、
  MariaDB 升级、mariadb-install-db、jemalloc、LSE、mariabackup、NUMA 绑核、大页。
license: MulanPSL-2.0
compatibility: MariaDB 10.11.16 源码；10.3.x 鲲鹏 4P / KVM 案例；openEuler/CentOS 系、aarch64 与 x86_64
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: relational_database
  version: "1.0.0"
---
# MariaDB 编译与部署

## 简介

在鲲鹏/ARM64 或 x86_64 上从源码构建 MariaDB，并完成单实例、多实例、主从或 Galera 部署。
本文把“装依赖 → 编译 → 规划拓扑/端口/目录 → 写配置 → 初始化 → 启服务 → 装载数据 →
验收 → 备份/恢复 → 升级/回滚”固定成可复现流程；命令参数化，可直接套用。

**实测基线**：鲲鹏 4P（多 NUMA、NVMe 数据盘）、KVM 虚拟化（cpuset 绑核 + 大页 +
直通盘），MariaDB 10.3.x + TPCCRunner；源码基线 10.11.16。其他机型只改变核数、NUMA
布局与盘数，不改变流程。

## 何时触发

- 在鲲鹏/openEuler 或 x86 上从零搭建、重建 MariaDB（单实例/多实例/主从/Galera）。
- 编译 10.11.16、切换 LSE/jemalloc/tcmalloc、替换三方库、做性能构建对照。
- 规划 NUMA 绑核、大页、端口、socket、数据/日志目录、临时目录。
- 装载 TPC-C/业务数据，做备份、恢复、重置、版本切换与回滚。
- 部署后验收失败：起不来、连不上、端口冲突、目录权限、初始化报错、复制不通。

**不适用**：缓存/redo/IO/binlog 调优 → `mariadb-performance-optimization`；
压测口径与正确性用例 → `mariadb-test-verification`；火焰图与热点归因 →
`mariadb-hotspot-analysis`；OS/BIOS/网卡/绑核通用项 → `kpbot-app-tuner` 对应 subskill。

## 变量约定

执行前先填表，正文与脚本统一引用这些变量；示例值仅为文档占位，不是真实环境。

| 变量 | 含义 | 示例 |
| --- | --- | --- |
| `${MARIADB_HOME}` | 安装前缀（basedir） | `/opt/mariadb/10.11.16` |
| `${SRC_ROOT}` | 源码树根 | `/opt/src/mariadb-10.11.16` |
| `${BUILD_DIR}` | 构建目录（out-of-source，默认本轮新建空目录；必须在 `${SRC_ROOT}` 之内，非空既有目录拒绝复用/清理） | `${SRC_ROOT}/build_<ts>` |
| `${DATA_ROOT}` | 数据根目录 | `/data/mariadb` |
| `${DATADIR}` | 实例数据目录 | `${DATA_ROOT}/data` |
| `${LOG_DIR}` / `${PID_DIR}` / `${RUN_DIR}` | 日志/pid/socket 目录 | `${DATA_ROOT}/{log,run}` |
| `${TMPDIR}` | 临时表/排序目录 | `${DATA_ROOT}/tmp` |
| `${PORT}` | MySQL 协议端口 | 3306 |
| `${SOCKET}` | unix socket | `${RUN_DIR}/mysql.sock` |
| `${INSTANCE}` | 多实例名（systemd `mariadb@`） | `mdb1` |
| `${MEM_GB}` | 物理内存（GB） | 512 |
| `${BP_SIZE}` | buffer pool 大小 | 总内存的 50%–70% |
| `${CPUS}` | 绑核范围 | `0-63` |
| `${HUGE_PAGES}` | 2M/1G 大页数量 | 见 §大页 |
| `${BACKUP_ROOT}` | 备份根目录 | `/data/backup` |
| `${LOADER_HOME}` | TPCCRunner/sysbench 目录 | `/opt/bench` |
| `${CLIENT_HOST}` | 压测机（sysbench 客户端进程所在主机），不是被测数据库地址 | `db-client` |
| `${DB_HOST}` | 被测数据库主机地址，sysbench prepare/预热与 TPC-C 装载统一连它 | `db-server` |
| `${BENCH_DB}` | 本轮专用基准库名（含实例名与轮次，多实例不碰默认实例的 `bench`） | `bench_${INSTANCE}_20261126` |
| `${DB_PASSWORD}` | 装载/备份用户口令：由环境变量或交互式 `read -rs` 注入内存，不进 argv/进程环境（凭据契约见 `references/data-load-and-backup.md` §0） | 外部注入 |
| `${DB_USER}` | 装载/备份用户名（§4 备份凭据文件默认 `root`） | `root` |
| `${LOAD_USER}` | 远程 TPC-C 装载专用账号：仅授予本轮库所需权限，DDL 权限按需单独授权；不用 root 远程登录 | `mdb_loader` |
| `${PWFILE}` | 0600 临时口令文件（`umask 077` + `mktemp`），由 `references/data-load-and-backup.md` §0 创建一次 | `$(mktemp)` |
| `${CLIENT_CNF}` | 0600 `[client]` defaults-file，供所有 `mariadb`/`mariadb-admin` CLI 用 `--defaults-extra-file` 引用（不进 argv、不进进程环境） | `$(mktemp)` |
| `${CNF}` | §4 备份凭据文件，复用 `${CLIENT_CNF}` 别名，不重复 mktemp/trap | `${CLIENT_CNF}` |
| `${SYSBENCH_CONF}` | 0600 sysbench `--config-file`，口令不进 argv | `$(mktemp)` |
| `${SERVICE_CTL}` / `${SERVICE_ACTION}` / `${SERVICE_NAME}` | 服务控制变量：`${SERVICE_CTL:-systemctl} ${SERVICE_ACTION:-start} "${SERVICE_NAME}"`；单/多实例统一走 `${SERVICE_NAME:-mariadb@${INSTANCE:-default}}` | `systemctl` / `start` / `mariadb@mdb1` |
| `${UNIT_FILE}` | 源码部署自带的 unit 模板路径（由本 skill 创建） | `/etc/systemd/system/mariadb@.service` |

凭据由环境变量或交互式 `read -rs` 注入内存后写入 0600 临时 defaults-file，不写入仓库内
配置文件、脚本、命令行或报告；所有落盘凭据一律 `umask 077` + `mktemp`，由**一条** EXIT
trap 统一清理（见 `references/data-load-and-backup.md` §0），同一 shell 内不得再定义第二条
EXIT trap 覆盖它。**禁止 `MYSQL_PWD`**：环境变量同样可经 `/proc/<pid>/environ`（同 UID
进程/root）与 `ps e` 读取，且被子进程继承，暴露面与 argv 等价；CLI 统一用
`--defaults-extra-file="${CLIENT_CNF}"`（必须是客户端的第一个选项）。

## 工作流总览

| 阶段 | 关键动作 | 材料 |
| --- | --- | --- |
| 1. 规划 | 形态、实例数、NUMA/绑核、端口、目录、内存预算 | 本文 §部署形态；`references/topology-and-config.md` |
| 2. 前置检查 | 架构/核数/内存/磁盘/依赖/端口/THP/ulimit/残留进程 | `scripts/preflight_check.sh`（只读） |
| 3. 构建环境 | 系统包、CMake、编译器、SSL/zlib、jemalloc/tcmalloc | `references/build-and-deps.md` |
| 4. 编译 | Release、`-march`、LSE、jemalloc、产物校验 | `references/build-and-deps.md` |
| 5. 配置 | my.cnf、目录、端口、socket、日志、绑核 | `references/topology-and-config.md` |
| 6. 初始化 | `mariadb-install-db`、root 认证、建库 | 本文 §初始化与启动 |
| 7. 启动/验收 | systemd 或 mysqld_safe、连通性、参数生效 | 本文 §启动与验收 |
| 8. 装载 | sysbench prepare / TPC-C loader、索引、对账 | `references/data-load-and-backup.md` |
| 9. 备份/恢复 | mariabackup 全量、prepare、restore、校验 | `references/data-load-and-backup.md` §备份 |
| 10. 升级/回滚 | 二进制替换、数据兼容、最小停机、回滚 | `references/operations-troubleshooting.md` §升级 |

## 部署形态选择

| 形态 | 进程 | 适用 | 注意 |
| --- | --- | --- | --- |
| 单实例 | 1 × mariadbd | 常规 OLTP、基准测试 | 绑核按 NUMA；buffer pool 占内存大头 |
| 多实例 | N × mariadbd | 隔离租户、提升利用率 | 端口/socket/pid/datadir/绑核必须成组 |
| 主从 | 主 + 从 | 读写分离、容灾、备份卸载 | GTID、复制延迟、半同步 |
| Galera | 3+ 节点 | 同步多主、高可用 | 写集认证、网络、SST 风险 |

选择原则：

- 只做功能/代码正确性验证：单实例最省事。
- 要看多实例资源隔离或提升整机利用率：多实例。
- 要读写分离或容灾：主从；要自动故障转移与多主：Galera（需评估写冲突）。

## 三方依赖与作用

| 依赖 | 作用 | 缺失后果 |
| --- | --- | --- |
| `cmake`（≥3.10） | 构建系统 | 无法配置 |
| `gcc/g++`（支持 ARMv8.1 LSE） | 编译器 | LSE/`-march` 不可用 |
| `make` / `ninja` | 构建执行 | 无法编译 |
| `ncurses-devel` | `mariadb` 命令行编辑 | 客户端缺功能 |
| `openssl-devel` | TLS、认证、加密函数 | `WITH_SSL` 失败 |
| `zlib-devel` | 压缩协议/引擎 | 压缩功能缺失 |
| `libaio-devel` | InnoDB 原生异步 IO | `native_aio` 不可用 |
| `bison` | SQL 语法生成 | 解析器无法生成 |
| `libxml2-devel` | XML 函数/引擎 | 相关功能缺失 |
| `lz4-devel` | 页压缩/备份压缩 | 压缩算法缺失 |
| `gnutls-devel`（可选） | 替代 OpenSSL 的 TLS | 视 `WITH_SSL` 选择 |
| `libcurl-devel` | 远程/HTTP 相关插件 | 部分插件不可用 |
| `boost`（部分版本/组件） | 某些存储引擎/工具 | 对应组件不编 |
| `jemalloc` / `tcmalloc` | 替代 glibc malloc，降低分配开销与碎片 | 只能用系统 malloc |
| `numactl` | 绑核/内存交错 | 无法做 NUMA 绑定 |
| `perl` + `python3` | mtr 测试框架 | 无法跑 mysql-test |
| `numactl-devel`（Debian/Ubuntu 为 `libnuma-dev`） | NUMA interleave / 定制参数 | 相关源码改动无法编 |

> 10.3 案例中的 `-DWITHOUT_TOKUDB=1` 在 10.11 已无意义（TokuDB 早已移除）；
> 不要照抄旧 CMake 选项，先看 10.11 的选项是否存在。

## 前置检查

```
# 只读检查：架构、NUMA、内存、磁盘、依赖、端口、THP、ulimit、残留进程
MARIADB_HOME=${MARIADB_HOME} DATA_ROOT=${DATA_ROOT} PORT=${PORT} \
  bash scripts/preflight_check.sh
```

要求：

- 存在 `FAIL` 时不得开始部署；修复后重跑至无 FAIL。
- `WARN` 项（THP、swappiness、ulimit、透明大页、时区）先确认影响再继续。
- 端口检查要覆盖 `PORT`、`extra_port`、Galera 端口与多实例端口段。

## 构建（详见 references/build-and-deps.md §4/§5）

完整 cmake 参数、make/make install 步骤与产物核对命令以 `references/build-and-deps.md` §5
为准，本文件不重复：cmake 配置 → make -j → make install，重点选项
`CMAKE_C_FLAGS`/`CMAKE_CXX_FLAGS`（平台自适应；仅当目标 ARM CPU 确认支持 LSE 时才加
`-march=armv8.2-a+lse`，x86 上传该参数会被 GCC 拒绝）、`WITH_INNOBASE_STORAGE_ENGINE`、
`WITH_SSL/WITH_ZLIB=system`；产物记录 `mariadbd --version` 与 md5。

优化构建（LSE 仅在目标 ARM CPU 确认支持时、jemalloc、tcmalloc）只在明确对比性能时使用，
必须单独记录参数与二进制 md5，不能与基线混用。

## 配置要点（完整模板见 references/topology-and-config.md）

- `innodb_buffer_pool_size`：占内存 50%–70%，按工作集调整；10.11 已移除
  `innodb_buffer_pool_instances`，buffer pool 内部自行分片。
- `innodb_log_file_size`：10.11 默认 96MB 偏小，写密集需增大（可在线 resize）。
- `innodb_flush_method=O_DIRECT`（10.11 默认），避免 page cache 双重缓存。
- `innodb_io_capacity`：NVMe 可显著高于默认 200；过低导致刷脏跟不上。
- `innodb_flush_log_at_trx_commit` / `sync_binlog`：性能与持久性的权衡，成对评估。
- `thread_handling`：高并发短查询考虑 `pool-of-threads`，否则保持每连接线程。
- `table_open_cache` / `thread_cache_size`：按连接数与表数调，避免频繁开关表/建线程。
- `aria_pagecache_buffer_size`：内部临时表多时增大。
- 数据盘 XFS + `noatime`；`tmpdir` 独立盘，避免排序/临时表与数据写竞争。
- 多实例：每实例独立 `port/socket/pid/datadir/log-error`，分别绑核。

## 初始化与启动

```
# 目录与权限
groupadd -f mysql
id -u mysql >/dev/null 2>&1 || useradd -g mysql -s /sbin/nologin mysql
mkdir -p "${DATADIR}" "${LOG_DIR}" "${RUN_DIR}" "${TMPDIR}"
chown -R mysql:mysql "${DATA_ROOT}"

# 初始化（10.11 推荐 mariadb-install-db；旧脚本 mysql_install_db 仍可用）
"${MARIADB_HOME}/bin/mariadb-install-db" \
  --defaults-file=/etc/my.cnf \
  --basedir="${MARIADB_HOME}" \
  --datadir="${DATADIR}" \
  --user=mysql

# 源码安装包不带 systemd unit（unit 来自发行版打包，发行版 mariadb.service 的 datadir
# 通常指向 /var/lib/mysql；直接 systemctl start mariadb 会启动另一个实例）。按本安装
# 前缀创建 mariadb@.service 模板，再用变量化服务名启停：
SERVICE_CTL="${SERVICE_CTL:-systemctl}"
SERVICE_NAME="${SERVICE_NAME:-mariadb@${INSTANCE:-default}}"
SERVICE_ACTION="${SERVICE_ACTION:-start}"
UNIT_FILE="/etc/systemd/system/mariadb@.service"
cat > "${UNIT_FILE}" <<UNIT
[Unit]
Description=MariaDB %i instance (source build)
After=network.target

[Service]
Type=simple
User=mysql
Group=mysql
ExecStart=${MARIADB_HOME}/bin/mariadbd --defaults-file=/etc/my.cnf --basedir=${MARIADB_HOME} --datadir=${DATADIR}
LimitNOFILE=65535
TimeoutStartSec=600
TimeoutStopSec=300
KillMode=process
Restart=on-failure

[Install]
WantedBy=multi-user.target
UNIT
"${SERVICE_CTL}" daemon-reload
"${SERVICE_CTL}" enable --now "${SERVICE_NAME}"
```

启动前确认 `my.cnf` 中 `[mysqld]` 段的 `basedir/datadir/socket/port/pid-file/log-error`
与实际目录一致；多实例用独立配置文件，并令 `INSTANCE` 指向该实例。启停与恢复/回滚统一
使用下面的包装：unit 不存在或 systemctl 不可用时显式回退，不再因 `systemctl ... || exit 1`
直接中止恢复/回滚路径。

```
svc_start() {
  "${SERVICE_CTL}" start "${SERVICE_NAME}" 2>/dev/null && return 0
  echo "unit 不存在或 systemctl 不可用，回退 mysqld_safe" >&2
  "${MARIADB_HOME}/bin/mysqld_safe" --defaults-file=/etc/my.cnf \
    --basedir="${MARIADB_HOME}" --datadir="${DATADIR}" --user=mysql &
}
svc_stop() {
  "${SERVICE_CTL}" stop "${SERVICE_NAME}" 2>/dev/null && return 0
  echo "unit 不存在或 systemctl 不可用，回退 mariadb-admin shutdown" >&2
  "${MARIADB_HOME}/bin/mariadb-admin" --defaults-extra-file="${CLIENT_CNF}" \
    --socket="${SOCKET}" -u root shutdown
}
svc_start
```

回退关闭需要 `${CLIENT_CNF}`（由 `references/data-load-and-backup.md` §0 建立 0600
defaults-file）；单独执行运维/恢复章节前必须先加载本节变量与函数。

## 启动与验收

验收清单：

| 检查 | 命令 | 通过标准 |
| --- | --- | --- |
| 进程 | `ps -ef \| grep [m]ariadbd` | 进程数与规划一致，无反复重启 |
| 监听 | `ss -lntp \| grep -E ':${PORT}\|mariadbd'` | 端口/socket 在，且归属正确 |
| 连接 | `mariadb --socket="${SOCKET}" -e "SELECT 1"` | 返回 1 |
| 版本 | `SELECT VERSION();` | 与二进制/构建记录一致 |
| 参数 | `SHOW VARIABLES LIKE 'innodb_buffer_pool_size';` | 与 my.cnf 一致 |
| 引擎 | `SHOW ENGINES;` | InnoDB 为 DEFAULT |
| 目录 | `du -sh "${DATADIR}"`、`ls "${DATADIR}"` | 有 `ibdata1`、`*.ibd`、redo、`mysql/` |
| 日志 | `tail -n 100 "${LOG_DIR}"/*.err` | 无 `ERROR`/`InnoDB: Cannot`/`Aborting` |
| 复制（如启用） | `SHOW SLAVE STATUS\G` | `IO/SQL Running: Yes`，延迟可接受 |
| Galera（如启用） | `SHOW STATUS LIKE 'wsrep_%';` | `wsrep_ready=ON`、`cluster_status=Primary` |

## 数据装载与备份

- **sysbench**：`prepare` 建表与数据，`run` 前统一 warmup/时长；每次压测前重置到同一
  快照，避免“越测越高”。`prepare`/预热与 TPC-C 装载都连 `${DB_HOST}`（被测库），
  不是运行压测进程的 `${CLIENT_HOST}`；目标库用本轮专用 `${BENCH_DB}`。
- **TPC-C（TPCCRunner）**：先 create database/table，再 loader 导入，再建索引；
slave 使用不同 warehouse 区间降低热点冲突；`userCount ≤ poolSize`。
- **备份**：`mariadb-backup --backup`（新流程默认不压缩）→ `--prepare` → 恢复到空目录后
  `--copy-back`，任一步失败即停止；`--compress`/`--compress-threads`/`--decompress` 基于
  QuickLZ，上游自 10.1.31/10.2.13 弃用（10.11 仍可用），仅用于兼容既有压缩备份——压缩
  备份须对同一目录先 `--decompress` 再 `--prepare`；备份前记录 binlog 位点/GTID；恢复后
  用 prepare 成功与计数对账（10.11 的 mariadb-backup 无 `--verify`）。
- **一致性**：备份期间避免 DDL；InnoDB 热备需 `--backup` 配合 redo 拷贝，不要直接
拷贝运行中的数据目录。

完整命令见 `references/data-load-and-backup.md`。

## 安全基线

- **凭据不入库、不进 argv/进程环境**：密码不写入仓库内脚本/配置样例/history；命令行可被
  `ps -ef`、`/proc/<pid>/cmdline` 读取，禁止 `-p"<密码>"`；环境变量同样可经
  `/proc/<pid>/environ`（同 UID 进程/root）与 `ps e` 读取并被子进程继承，故禁止 `MYSQL_PWD`。
  CLI 统一用 `--defaults-extra-file="${CLIENT_CNF}"`（0600、mktemp、单条 EXIT trap 清理）。
- **SSH 主机密钥校验**：默认 `StrictHostKeyChecking=accept-new`，禁止 `no` 与
`UserKnownHostsFile=/dev/null`。
- **下载物校验**：源码包/三方库安装前校验 sha256/GPG；禁止 `curl … | sh` 与 `-k`。
- **变更可回滚**：二进制替换前保留 `bin.bak.<ts>` 与 md5；配置变更保留原文件。
- **最小停机**：主从/多实例按“停一个、换一个、验一个”滚动替换，不同时全停。

## 错误处理

| 症状 | 处置 |
| --- | --- |
| 初始化报 `Can't create/write to file` | 目录属主/权限/SELinux；`chown -R mysql:mysql` 并检查 `datadir` |
| 启动即退出，日志 `unknown variable` | my.cnf 选项在 10.11 不存在或被移除；逐项核对 |
| `Address already in use` | 端口/socket 冲突；多实例端口段与 pid/socket 必须唯一 |
| 客户端 `Can't connect through socket` | `socket` 路径不一致或目录权限；显式 `--socket` |
| `InnoDB: Cannot allocate memory` | 大页未预留、buffer pool 超过可用内存、cgroup 限制 |
| 大页分配失败 | `nr_hugepages` 不足或 NUMA 节点不对；按节点设置并 `drop_caches` 后重试 |
| `mariadbd: error while loading shared libraries` | 运行时库路径；`ldconfig` 或 `LD_LIBRARY_PATH` |
| `Text file busy`（替换二进制） | 先停进程再覆盖，不要对运行中二进制 `cp` |
| 从库 `Last_IO_Error` 认证失败 | 复制用户/权限/密码、`server_id`、GTID 配置 |
| Galera 无法加入 | `wsrep_cluster_address`、SST 方法、防火墙、数据目录残留 |
| 导入后结果偏高 | 数据未重置/缓存未清；重装数据并 `drop_caches`，固定 warmup |

## 输出契约

- **新部署/重建**：变量表、形态、构建参数、端口/CPU/目录布局、初始化与验收结果、
二进制 md5 与版本。
- **装载**：数据集/规模、warehouse 或表数、索引、耗时、失败数、对账结果。
- **备份/恢复**：备份路径、binlog 位点/GTID、prepare 结果、恢复后校验。
- **升级/回滚**：改前/改后版本与 md5、停机窗口、验收结果、回滚命令与复验。
- **任何变更**：改前 → 改后、验证命令、回滚方式、风险与用户确认记录。

## 参考资料与脚本

| 文件 | 何时读 |
| --- | --- |
| `references/build-and-deps.md` | 装依赖、CMake 选项、LSE/jemalloc/tcmalloc、编译报错、构建对照 |
| `references/topology-and-config.md` | 单实例/多实例/主从/Galera、NUMA 绑核、大页、端口、完整 my.cnf 模板 |
| `references/data-load-and-backup.md` | sysbench/TPC-C 装载、索引、对账、mariabackup 备份恢复、重置 |
| `references/operations-troubleshooting.md` | 启停、状态、日志、常见故障、版本切换、升级回滚 |
| `scripts/preflight_check.sh` | 部署前只读检查（PASS/WARN/FAIL，有 FAIL 退出非零） |

