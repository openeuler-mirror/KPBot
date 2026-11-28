---
name: mysql-build-deployment
description: >
  MySQL 8.0（8.0.25/8.0.28，兼容 Percona Server 5.7/8.0）在鲲鹏/ARM64（aarch64）与
  x86_64 Linux 上的依赖准备、源码编译、安装、初始化、基线配置、物理机单实例部署、验收、
  备份/升级/回滚：GCC/CMake/Boost/OpenSSL/libaio/ncurses 等依赖作用，CMake 基线与
  `-O3 -march=armv8-a+crypto+lse+crc` 优化构建，`--initialize-insecure`，my.cnf 基线，
  datadir/socket/日志目录规划，NUMA/IRQ/大页前置，常见启动与压测故障。容器与虚机形态
  分别交给 mysql-container-deployment / mysql-vm-deployment；不含参数调优
  （→ mysql-performance-optimization）、压测/正确性（→ mysql-test-verification）、
  热点归因（→ mysql-hotspot-analysis）。触发词：MySQL 编译、MySQL 安装、MySQL 部署、
  MySQL 初始化、Boost、CMake、my.cnf、datadir、升级、回滚、启动失败。
license: MulanPSL-2.0
compatibility: MySQL 8.0.25/8.0.28；Percona Server 5.7.44/8.0 可参考；鲲鹏 920/920B/920G、aarch64、openEuler 22.03/24.03、GCC 10.3/12.3 实测；x86_64 需按平台重选编译参数
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: relational_database
  version: "1.0.0"
---

# MySQL 编译与部署

## 简介

在 Linux（优先 aarch64/openEuler）上从源码构建 MySQL 8.0，完成初始化、基线配置、
单实例部署与验收。把“装依赖 → 编 Boost/MySQL → 安装 → 初始化 → 写 my.cnf → 启服务 →
验收 → 备份/升级/回滚”固定成可复现流程；命令参数化，可直接套用。

**实测基线**：鲲鹏 920G（96C/192T、768GB、NVMe）、openEuler 24.03 SP2、GCC 12.3.1、
glibc 2.38、CMake 3.27.9、MySQL 8.0.25、Boost 1.73.0；另一基线为 920B（80C/160T×2、
512GB、openEuler 22.03 SP1、64K 页、MySQL 8.0.28）。其他机型只改变量纲与 NUMA 布局，
不改变流程。

## 何时触发

- 从源码编译 MySQL 8.0，或切换 `-O3`/`-march` 优化构建。
- 在物理机/容器/虚机上从零部署单实例，或重建 datadir。
- 规划 basedir/datadir/socket/日志/tmpdir、NUMA 绑核、端口、文件系统与大页。
- 备份、升级、换二进制、回滚，或排查启动/连接/压测失败。
- 需要一份可复验的部署产物（`deploy.env`）供测试/调优环节消费。

**不适用**：容器形态 → `mysql-container-deployment`；虚机形态 → `mysql-vm-deployment`；
my.cnf 深度调参 → `mysql-performance-optimization` 的 `mysql-app-config-optimization`；
MTR/sysbench/A-B → `mysql-test-verification`；perf 归因 → `mysql-hotspot-analysis`。

## 变量约定

执行前先填表，正文与脚本统一引用；示例值仅为占位，不是真实环境。

| 变量 | 含义 | 示例 |
| --- | --- | --- |
| `${SRC_ROOT}` | 源码与依赖包根目录 | `/opt/src` |
| `${MYSQL_SRC}` | MySQL 源码目录 | `${SRC_ROOT}/mysql-8.0.25` |
| `${BOOST_ROOT}` | Boost 安装/解压目录 | `${SRC_ROOT}/boost_1_73_0` |
| `${BUILD_DIR}` | 构建目录 | `${MYSQL_SRC}/build` |
| `${MYSQL_HOME}` | 安装前缀（basedir） | `/opt/mysql-8.0.25` |
| `${DATA_ROOT}` | datadir 根 | `/data/mysql` |
| `${SOCKET}` | unix socket | `${DATA_ROOT}/mysql.sock` |
| `${MYSQL_PORT}` | TCP 端口 | 3306 |
| `${MYSQLX_ENABLED}` | 是否启用 X Protocol（1/0） | 0 |
| `${MYSQLX_PORT}` | X Protocol 端口（启用时必检） | 33060 |
| `${MYSQL_USER}` | 运行用户 | `mysql` |
| `${MYSQL_CONF}` | 配置文件 | `/etc/my.cnf` |
| `${SPEC_CNF}` | 规格模板 | `my-8u32g.cnf` |
| `${CLIENT_HOST}` | 压测客户端地址 | `<客户端地址>` |
| `${PKG_SHA256}` | 源码包校验值 | 官方/内部发布值 |
| `${BACKUP_ROOT}` | 备份根 | `/data/backup` |
| `${CRED_FILE}` | 凭据文件（`--defaults-extra-file`，权限 600，用后 trap 清理） | mktemp 生成 |
| `${ETH}` | 宿主业务网卡（`references/topology-and-config.md` §2 使用） | `eth0` |

凭据一律通过 `--defaults-extra-file`（权限 600）或交互式 `read -rs` 注入；禁止写入
脚本/报告、禁止 `-p<密码>`。环境变量会经 `/proc/<pid>/environ` 泄露，不作为首选。

## 工作流总览

| 阶段 | 关键动作 | 材料 |
| --- | --- | --- |
| 1. 规划 | 部署形态、规格、NUMA/绑核、目录、文件系统、端口 | 本文 §规格与目录；`references/topology-and-config.md` |
| 2. 前置检查 | 架构/核数/内存/磁盘/依赖/端口/大页/残留进程 | `scripts/preflight_check.sh`（只读） |
| 3. 依赖 | 系统包、Boost、OpenSSL、libaio、ncurses 等 | `references/build-and-deps.md` |
| 4. 编译 | 基线或 `-O3` 优化构建、产物校验 | `references/build-and-deps.md` |
| 5. 安装与目录 | install、建用户/目录 | `references/install-and-init.md` |
| 6. 配置生成与初始化 | 先生成并核验本实例 my.cnf（目标 datadir/socket/页大小），再 `--initialize-insecure`、账号 | `references/topology-and-config.md` + `references/install-and-init.md` |
| 7. 启动与验收 | 启动、登录、版本、datadir、错误日志、sysbench prepare 冒烟 | 本文 §启动与验收 |
| 8. 备份/升级/回滚 | 停服、备份、二进制替换、数据兼容、复验 | `references/troubleshooting-and-upgrade.md` |

## 规格与目录

| 规格 | 典型分配 | 线程参考（Point/RO/RW/WO） | 备注 |
| --- | --- | --- | --- |
| 8U32G | 8 核 / 32 GiB | 28/28/80/80（容器基线）；物理机可按 1:7~1:8 | buffer pool ≈ 22G |
| 8U32G（20t） | 20 表×100 万 | 32/32/300/300 | socket 压测 |
| 8U32G（64t） | 64 表×1000 万 | 32/32/300/300 | socket 压测 |
| 32C256G | 32 核 / 256 GiB | 按 worker 核 1:6~1:8 | buffer pool ≈ 137G |
| 整机 1P | 74 worker + IRQ + log | 592（只读）/按 1:6~1:7（读写） | 见 §绑核 |

目录基线：

```
${MYSQL_HOME}/bin/mysqld
${MYSQL_HOME}/lib/plugin/...
${DATA_ROOT}/data/          # datadir
${DATA_ROOT}/log/mysql.log  # log-error
${DATA_ROOT}/tmp/           # tmpdir
${DATA_ROOT}/run/mysql.pid  # pid-file
${SOCKET}
```

## 前置检查

```
MYSQL_HOME=${MYSQL_HOME} DATA_ROOT=${DATA_ROOT} MYSQL_PORT=${MYSQL_PORT} \
  MYSQLX_ENABLED=${MYSQLX_ENABLED} MYSQLX_PORT=${MYSQLX_PORT} SOCKET=${SOCKET} \
  MYSQL_USER=${MYSQL_USER} bash scripts/preflight_check.sh
```

要求：

- 有 `FAIL` 不得开始部署；修复后重跑至无 FAIL。
- `WARN`（THP、SELinux、ulimit、大页、SMMU、端口占用）先确认影响再继续。
- 端口检查包含 `MYSQL_PORT`，启用 X Protocol（`MYSQLX_ENABLED=1`）时同时核验 `MYSQLX_PORT`；socket 除检查文件是否残留外，还须按运行账号核验父目录写权限。

## 依赖与编译（详见 references/build-and-deps.md）

最小路径：

```
# 1) 系统依赖（按发行版包名调整）
sudo yum install -y gcc gcc-c++ cmake make m4 automake flex bison \
  ncurses-devel openssl-devel libtirpc-devel rpcgen libaio-devel \
  cyrus-sasl-devel openldap-devel numactl sysstat dwarves elfutils-libelf-devel

# 2) 校验并解压 Boost 1.73.0（MySQL 8.0 要求）
sha256sum "${SRC_ROOT}/boost_1_73_0.tar.gz"   # 与发布值比对
tar -xzf "${SRC_ROOT}/boost_1_73_0.tar.gz" -C "${SRC_ROOT}"

# 3) 基线构建
mkdir -p "${BUILD_DIR}" && cd "${BUILD_DIR}"
cmake "${MYSQL_SRC}" \
  -DCMAKE_INSTALL_PREFIX="${MYSQL_HOME}" \
  -DWITH_BOOST="${BOOST_ROOT}" \
  -DWITH_BLACKHOLE_STORAGE_ENGINE=0 \
  -DWITH_FEDERATED_STORAGE_ENGINE=0 \
  -DWITH_ARCHIVE_STORAGE_ENGINE=0 \
  -DDISABLE_PSI_MUTEX=0 -DWITH_MYSQLX=OFF -DFORCE_INSOURCE_BUILD=1
make -j"$(nproc)" && make install
```

`-O3` 优化构建（aarch64，先确认 CPU 支持 LSE/CRC）：

```
cmake "${MYSQL_SRC}" \
  -DCMAKE_INSTALL_PREFIX="${MYSQL_HOME}" -DWITH_BOOST="${BOOST_ROOT}" \
  -DCMAKE_C_FLAGS="-O3 -march=armv8-a+crypto+lse+crc -flto=auto -funroll-loops -DNDEBUG" \
  -DCMAKE_CXX_FLAGS="-O3 -march=armv8-a+crypto+lse+crc -flto=auto -funroll-loops -DNDEBUG" \
  -DCMAKE_C_FLAGS_RELWITHDEBINFO="-O3 -march=armv8-a+crypto+lse+crc -flto=auto -funroll-loops -DNDEBUG" \
  -DCMAKE_CXX_FLAGS_RELWITHDEBINFO="-O3 -march=armv8-a+crypto+lse+crc -flto=auto -funroll-loops -DNDEBUG" \
  -DWITH_BLACKHOLE_STORAGE_ENGINE=0 -DWITH_FEDERATED_STORAGE_ENGINE=0 \
  -DWITH_ARCHIVE_STORAGE_ENGINE=0 -DDISABLE_PSI_MUTEX=0 -DWITH_MYSQLX=OFF \
  -DFORCE_INSOURCE_BUILD=1
```

产物校验：`${MYSQL_HOME}/bin/mysqld --version`、`ldd`、`md5sum`，并记录编译参数。
优化构建必须与基线分开、单独 A/B；不能把编译收益与参数收益混在一起。

## 初始化与基线配置（详见 references/install-and-init.md）

```
sudo useradd -r -s /sbin/nologin "${MYSQL_USER}" 2>/dev/null || true
sudo mkdir -p "${DATA_ROOT}"/{data,log,tmp,run}
sudo chown -R "${MYSQL_USER}":"${MYSQL_USER}" "${DATA_ROOT}"

# --initialize 读取 --defaults-file：必须先渲染并核验本实例配置
# 按 references/topology-and-config.md 模板生成 ${MYSQL_CONF}，再核验实际生效值：
"${MYSQL_HOME}/bin/mysqld" --defaults-file="${MYSQL_CONF}" --verbose --help 2>/dev/null \
  | grep -Ei 'datadir|socket|innodb[-_]page[-_]size'
# 确认 datadir=${DATA_ROOT}/data 且为空、socket=${SOCKET}、页大小与目标一致

sudo -u "${MYSQL_USER}" "${MYSQL_HOME}/bin/mysqld" \
  --defaults-file="${MYSQL_CONF}" --initialize-insecure

# 启动后设置 root 密码与测试账号（隔离库/最小权限/限定客户端，见 install-and-init.md §3）
```

基线 my.cnf 见 `references/topology-and-config.md`，关键项：

- `basedir`、`datadir`、`socket`、`log-error`、`pid-file`、`tmpdir` 必须一致且可写。
- `innodb_page_size` 仅初始化可设：由初始化时的 my.cnf 决定，已有 datadir 不可更改；变更须新建实例并迁移数据。
- `innodb_buffer_pool_size` 通常为内存 50%–70%；小规格容器 22G/32G 是实测基线。
- `innodb_buffer_pool_instances`、`innodb_log_file_size`、`innodb_log_buffer_size`、
`innodb_open_files`、`table_open_cache` 按并发与数据集设置。
- `innodb_flush_method=O_DIRECT`、`innodb_flush_log_at_trx_commit` 与 `sync_binlog`
按 durability 目标设置；压测值与生产值必须分开记录。
- `performance_schema=OFF` 可降开销，但会失去等待/线程证据；定位阶段建议临时开启。

## 启动与验收

启动顺序：初始化 → `mysqld_safe`/systemd 启动 → 等待 socket → 登录验收。

```
sudo -u "${MYSQL_USER}" nohup "${MYSQL_HOME}/bin/mysqld_safe" \
  --defaults-file="${MYSQL_CONF}" >/dev/null 2>&1 &

"${MYSQL_HOME}/bin/mysql" --socket="${SOCKET}" -uroot -e "SELECT VERSION();"
```

验收清单：

| 检查 | 命令 | 通过标准 |
| --- | --- | --- |
| 进程 | `ps -ef \| grep [m]ysqld` | 1 个主进程，无反复重启 |
| 监听 | `ss -lntp \| grep ${MYSQL_PORT}` | TCP LISTEN；socket 文件存在 |
| 版本 | `mysqld --version` / `SELECT VERSION()` | 与目标版本一致 |
| 登录 | `mysql --socket=... -e 'SELECT 1'` | 返回 1 |
| 数据目录 | `du -sh ${DATA_ROOT}/data` | 初始化文件齐全（ibdata1、undo、mysql 库） |
| 错误日志 | `tail -n 100 ${DATA_ROOT}/log/mysql.log` | 无 ERROR/崩溃/权限错误 |
| 参数 | `SHOW VARIABLES LIKE 'innodb_buffer_pool_size'` | 与配置一致 |
| 冒烟 | `CREATE DATABASE`/`CREATE TABLE`/`INSERT`/`SELECT`（隔离库） | 成功且可回读 |

## 备份、升级与回滚

规则（完整命令见 `references/troubleshooting-and-upgrade.md`）：

- 升级前先停写（`FLUSH TABLES WITH READ LOCK`/只读或停服）再做一致性备份：配置、datadir、binlog 位置、二进制 md5、账号与复制状态。
- 恢复点必须最终一致：若备份完成时仍有事务提交，保留并验证从备份时点到停写点的 binlog（或停写后重做备份）；否则恢复快照会丢失已确认写入，不得替换二进制。
- 数据格式兼容：MySQL 只保证向上兼容；旧二进制不能打开新版本 datadir。
8.0 内小版本升级通常原地替换二进制并启动；跨大版本按官方升级路径（8.0.16+ 由新二进制
启动时自动完成数据字典/系统表升级，`mysql_upgrade` 已废弃只打印提示；5.7→8.0 前先跑
upgrade checker），不要直接替换。
- 最少停机：停写 → 一致性备份并确认最终恢复点 → 替换二进制 → 启动 → 功能/性能验收 → 开放流量。
- 回滚：停服/停写 → 确认恢复点（必要时用已验证 binlog 前滚到停写点）→ 恢复旧二进制与旧配置 → 恢复升级前数据快照 → 复验。
没有数据快照不得声称可回滚。
- 二进制替换前保留 `bin.bak.<时间戳>` 并记录 md5；配置覆盖前保留 `.bak.<时间戳>`。

## 安全基线

- **凭据不入库、不落 argv**：用 `--defaults-extra-file`（权限 600）或交互式 `read -rs`；
禁止 `-p<密码>`、禁止把密码写入脚本/报告。
- **下载物校验**：源码包、Boost、第三方库安装前校验 sha256/GPG；禁止从网络管道直接执行下载的脚本（先落盘、校验后再执行）。
- **最小权限**：mysqld 以专用用户运行；datadir/socket/log 权限最小化；不默认 `bind 0.0.0.0`。
- **变更可回滚**：任何配置/二进制/数据变更先备份，记录改前改后与回滚命令。
- **不自动改内核/BIOS**：只检查并提示；OS/BIOS 变更交给 `mysql-os-optimization`/`bios-optimization`。

## 错误处理

| 症状 | 处置 |
| --- | --- |
| `cannot allocate memory for the buffer pool` | buffer pool 超过可用内存；降到内存 50%–70%，检查 cgroup/容器限制 |
| `Can't connect ... through socket` | socket 路径不一致或被占用；删除残留 `.sock`/`.sock.lock`，核对 my.cnf |
| `component_reference_cache.so` 找不到 | `basedir` 错误或复制二进制未带 `lib/plugin`；修正 basedir 或完整安装 |
| 启动即崩溃/版本不匹配 | datadir 由不兼容版本创建；恢复备份或重新初始化（会丢数据，需确认） |
| `Too many open files` | 提高 `ulimit -n` 与 `open_files_limit`，检查 systemd `LimitNOFILE` |
| `mysql_stmt_prepare() failed`/1461 | 提高 `max_prepared_stmt_count` |
| 端口占用 | `ss -lntp` 定位；换端口或停旧实例 |
| 编译 `cannot find -lstdc++` | 安装 `libstdc++-static`/开发包 |
| 编译 LTO 失败 | 回退 LTO，确认 GCC/binutils 版本一致 |
| 压测 CPU 打不满 | 先查客户端瓶颈、服务端网络、sysbench 并发（见 test-verification） |

## 输出契约

- **新部署**：变量表、规格、编译参数、目录/端口/绑核、验收结果、二进制 md5/版本。
- **初始化**：datadir、账号（不输出密码）、字符集/collation、时区。
- **升级/回滚**：改前/改后版本与 md5、停机窗口、备份路径、验收与回滚命令。
- **任何变更**：改前 → 改后、验证命令、回滚方式、风险与用户确认记录。

## 参考资料与脚本

| 文件 | 何时读 |
| --- | --- |
| `references/build-and-deps.md` | 依赖作用、Boost/OpenSSL/libaio、基线/优化构建、编译报错 |
| `references/install-and-init.md` | install、用户/目录、初始化、账号、字符集/时区、systemd |
| `references/topology-and-config.md` | 规格/目录/端口、my.cnf 模板、NUMA/IRQ/大页前置 |
| `references/troubleshooting-and-upgrade.md` | 启动/连接/压测故障、备份、升级、回滚 |
| `scripts/preflight_check.sh` | 部署前只读检查（PASS/WARN/FAIL，有 FAIL 退出非零） |
| `mysql-container-deployment/`（域内兄弟成员） | 容器镜像、cpuset/mems、host IRQ、容器内绑核 |
| `mysql-vm-deployment/`（域内兄弟成员） | libvirt、网桥、大页/NUMA、vcpupin、直通 |
