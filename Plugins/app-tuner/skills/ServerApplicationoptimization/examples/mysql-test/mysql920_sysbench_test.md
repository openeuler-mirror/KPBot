# MySQL Sysbench 性能测试用例

> 测试规格：8 vCPU / 32GB RAM / NVMe SSD
>
> MySQL 版本：8.0.25（自编译）
>
> Sysbench 版本：1.0.20

---

## 目录

- [1. 测试概述](#1-测试概述)
  - [1.1 测试用例](#11-测试用例)
  - [1.2 测试环境](#12-测试环境)
  - [1.3 远程压测组网](#13-远程压测组网)
- [2. MySQL 部署指导](#2-mysql-部署指导)
  - [2.1 依赖安装](#21-依赖安装)
  - [2.2 源码编译 MySQL 8.0.25](#22-源码编译-mysql-8025)
  - [2.3 MySQL 配置](#23-mysql-配置)
  - [2.4 初始化与启动](#24-初始化与启动)
  - [2.5 创建测试用户与数据库](#25-创建测试用户与数据库)
  - [2.6 数据准备](#26-数据准备)
  - [2.7 测试执行命令](#27-测试执行命令)
  - [2.8 远程只读压测](#28-远程只读压测)
- [3. 测试结果与分析](#3-测试结果与分析)
  - [3.1 只读测试结果 (oltp_read_only)](#31-只读测试结果-oltp_read_only)
  - [3.2 结果分析](#32-结果分析)
- [4. 各阶段耗时统计](#4-各阶段耗时统计)
  - [4.1 编译部署阶段](#41-编译部署阶段)
  - [4.2 数据准备阶段](#42-数据准备阶段)
  - [4.3 性能测试阶段](#43-性能测试阶段)
  - [4.4 全流程汇总](#44-全流程汇总)
- [5. 附录](#5-附录)
  - [5.1 一键测试脚本](#51-一键测试脚本)
  - [5.2 服务端监控与瓶颈排查](#52-服务端监控与瓶颈排查)
  - [5.3 测试后清理](#53-测试后清理)
  - [5.4 注意事项](#54-注意事项)

---

## 1. 测试概述

### 1.1 测试用例

基于已生成的 **64 张表 × 10,000,000 行**（NVMe SSD）测试数据，执行只读压力测试：

| 编号 | 测试场景 | Sysbench 命令 | 说明 | 事务组成 |
|------|----------|--------------|------|----------|
| TC-01 | 只读测试 | `oltp_read_only` | 纯读压力测试，衡量 Buffer Pool 命中与 CPU 性能 | 每事务 6 条 SELECT |

**测试参数**：

| 参数 | 值 | 说明 |
|------|------|------|
| `--tables` | 64 | 已生成的测试表数量 |
| `--table-size` | 10,000,000 | 每表 1000 万行 |
| `--threads` | 40 | 并发线程数 |
| `--time` | 120 | 每轮测试时长 120 秒 |
| `--report-interval` | 10 | 每 10 秒输出中间结果 |

**关键指标**：

| 指标 | 说明 | 关注点 |
|------|------|--------|
| TPS (Transactions/sec) | 每秒事务数 | 核心吞吐量指标 |
| QPS (Queries/sec) | 每秒查询数 | 总查询处理能力 |
| Latency Avg (ms) | 平均延迟 | 整体响应时间 |
| Latency P95 (ms) | 95 百分位延迟 | 长尾延迟 |
| Latency Max (ms) | 最大延迟 | 异常毛刺 |

### 1.2 测试环境

#### 1.2.1 服务器硬件

| 项目 | 规格 |
|------|------|
| 服务器型号 | Huawei TaiShan 200 (Model 2280) |
| 主板 | Huawei BC82AMDDUA |
| BIOS | Huawei Corp. 4.03_DVM (2023-04-03) |

#### 1.2.2 CPU

| 项目 | 规格 |
|------|------|
| CPU 型号 | HUAWEI Kunpeng 920 7260 |
| 架构 | aarch64 (ARMv8) |
| 插槽数 | 2 |
| 每插槽核心数 | 64 |
| **总核心数** | **128 vCPU**（单线程，无超线程） |
| 主频 | 2.6 GHz (BogoMIPS: 200.00) |
| L1d Cache | 8 MiB（128 × 64 KiB） |
| L1i Cache | 8 MiB（128 × 64 KiB） |
| L2 Cache | 64 MiB（128 × 512 KiB） |
| L3 Cache | 128 MiB（4 × 32 MiB） |
| NUMA 节点 | 4（每节点 32 核） |

NUMA 拓扑：

| NUMA 节点 | CPU 核心 | 内存 |
|-----------|----------|------|
| Node 0 | 0-31 | ~62 GiB |
| Node 1 | 32-63 | ~63 GiB |
| Node 2 | 64-95 | ~126 GiB |
| Node 3 | 96-127 | ~125 GiB |

#### 1.2.3 内存

| 项目 | 规格 |
|------|------|
| 总内存 | **376 GiB**（394,643,384 kB） |
| 内存类型 | Registered DDR4 (RDIMM) |
| 插槽配置 | 2 Socket × 8 Channel × 1 DIMM/Channel = **16 条 DIMM** |
| Socket 0 | Channel 0-7，每通道 1 条 DIMM |
| Socket 1 | Channel 0-7，每通道 1 条 DIMM |
| Swap | 4 GiB |
| HugePages | 未启用（Hugepagesize 2048 kB） |

#### 1.2.4 磁盘

| 设备 | 型号 | 数量 | 单盘容量 | 接口 | 转速/类型 | I/O 调度器 |
|------|------|------|----------|------|-----------|-----------|
| sda ~ sdg | Seagate ST4000NM0035-1V4 | 10 | 3.64 TiB | SATA | HDD 7200RPM | mq-deadline |
| sdh | HGST HUS726T4ALA600 | 1 | 3.64 TiB | SATA | HDD 7200RPM | mq-deadline |
| nvme0n1 | Huawei HWE56P436T4M005N | 1 | 5.82 TiB | NVMe | SSD | none |

> 磁盘合计：**11 × 4TB HDD** + **1 × 5.8TB NVMe SSD**

文件系统挂载：

| 挂载点 | 设备 | 文件系统 | 容量 | 用途 |
|--------|------|----------|------|------|
| / (overlay) | /dev/mapper/openeuler-root | ext4 | 1.1 TiB | 系统盘 |
| /host/home | /dev/mapper/openeuler-home | ext4 | 2.6 TiB | 数据盘 |
| /host/home/mysql | /dev/nvme0n1 | xfs | 5.9 TiB | MySQL NVMe 数据 |

#### 1.2.5 网卡

| 网卡 | 型号/驱动 | 接口类型 | 速率 | 数量 | 状态 |
|------|-----------|----------|------|------|------|
| 板载网卡 | HiSilicon hns3 (PCI ID: 19e5:a222/a221) | 千兆/万兆 | - | 4 端口 (enp125s0f0~f3) | DOWN |
| PCIe 网卡 | HiSilicon hinic (PCI ID: 19e5:1822) | 光纤 | 10 GbE | 1 端口 (enp133s0) | **UP** |
| PCIe 网卡 | HiSilicon hinic (PCI ID: 19e5:1822) | 光纤 | 25 GbE | 1 端口 (enp134s0) | **UP** |
| PCIe 网卡 | HiSilicon hinic (PCI ID: 19e5:1822) | 光纤 | - | 2 端口 (enp135s0, enp136s0) | DOWN |

> 网卡合计：**1 张 4 端口板载 hns3** + **1 张 4 端口 PCIe hinic**，活跃端口 2 个（10GbE + 25GbE）

#### 1.2.6 操作系统

| 项目 | 版本 |
|------|------|
| OS | openEuler 22.03 LTS-SP2 |
| 内核 | 5.10.0-153.56.0.134.oe2203sp2.aarch64 |
| 架构 | aarch64 (GNU/Linux) |
| vm.swappiness | 10 |
| Transparent HugePage | always |

#### 1.2.7 软件版本

| 软件 | 版本 | 来源 |
|------|------|------|
| MySQL (自编译) | 8.0.25 | 源码编译安装至 /usr/local/mysql |
| Sysbench | 1.0.20 | yum 安装 (sysbench-1.0.20-3.oe2203sp2.aarch64) |

#### 1.2.8 环境模拟说明

本机实际硬件为 128 vCPU / 376GB RAM，通过资源限制模拟 **8U32G** 规格：

| 项目 | 实际配置 | 模拟规格 | 限制方式 |
|------|----------|----------|----------|
| CPU | 128 vCPU (Kunpeng 920) | 8 vCPU | systemd CPUAffinity 绑核 0-7 |
| 内存 | 376 GB | MySQL 限制 ~24GB | systemd MemoryMax + innodb_buffer_pool_size=20G |
| 磁盘 | NVMe SSD (HWE56P436T4M005N, 5.82TB) | NVMe SSD | 保持不变 |

### 1.3 远程压测组网

```
┌──────────────────────────────────┐       ┌──────────────────────────────────────────────────┐
│     压测客户端 (192.168.90.105)   │       │              MySQL 服务器 (192.168.90.170)          │
│     Huawei 服务器 (型号未识别)     │       │           openEuler 22.03 LTS-SP2                  │
│     HiSilicon × 2 / 502GB DDR4   │       │           aarch64                                   │
│                                   │       │           Kunpeng 920 × 2 / 376GB DDR4              │
│     Sysbench 1.0.20              │       │  ┌─────── 容器 mysql-test-8u32g (8U32G) ─────────┐  │
│     网卡: Intel 82599ES (ens4f1) │       │           2S×64C=128 vCPU / 376GB                  │
│                                   │       │                                                     │
│  ┌─────────────┐                  │       │  │  ┌──────────────┐                             │  │
│  │   Sysbench   │──── 10GbE ─────┼──────►│  │  │ MySQL 8.0.25 │                             │  │
│  │   (客户端)    │  客户端: ens4f1  │      │  │  │ (port 3308)  │                             │  │
│  └─────────────┘  服务端: enp133s0│       │  │  │ datadir:     │                             │  │
│                                   │       │  │  │ /host/home/  │                             │  │
│  工作目录:                         │       │  │  │ mysql/       │                             │  │
│  /ssd/data2/mysql-test            │       │  │  │ sxk-test     │                             │  │
│                                   │       │  │  └──────────────┘                             │  │
│                                   │       │  │  CPU: 8 vCPU (taskset 绑核)                    │  │
│                                   │       │  │  内存: 32GB (buffer_pool=20G)                  │  │
│                                   │       │  └────────────────────────────────────────────────┘  │
│                                   │       │  网卡：hinic (enp133s0, 10GbE)                     │
│                                   │       │  磁盘：NVMe SSD                                     │
└──────────────────────────────────┘       └──────────────────────────────────────────────────┘
```

**组网说明**：

| 项目 | MySQL 服务器 (被压测端) | 压测客户端 |
|------|----------------------|-----------|
| IP | 192.168.90.170 | 192.168.90.105 |
| 物理机 | Huawei TaiShan 200 (2280), Kunpeng 920 × 2 / 376GB DDR4 | Huawei 服务器 (型号未识别), HiSilicon (0x48, part 0xd03) × 2 / 502GB DDR4 |
| 容器 | `mysql-test-8u32g`，**8 vCPU / 32GB RAM**（mysqld 运行在容器内） | — |
| CPU | Kunpeng 920, 容器内 8 vCPU (taskset 绑核) | HiSilicon (0x48, part 0xd03), 2S×48C×2T = 192 vCPU |
| OS | openEuler 22.03 LTS-SP2 | openEuler 24.03 LTS-SP2 |
| MySQL | 8.0.25 (自编译) port 3308 | — |
| Sysbench | — | 1.0.20 |
| 数据目录 | `/host/home/mysql/sxk-test/` | `/ssd/data2/mysql-test/` |
| 网卡 | enp133s0 (hinic 10GbE UP) | ens4f1 (Intel 82599ES 10GbE 光纤, ixgbe 驱动) |
| 连接方式 | `bind_address=0.0.0.0` 允许远程 | 通过 TCP 连接 |
| SSH 登录 | `root@192.168.90.170` | `sxk@192.168.90.105` |

登录压测客户端：

```bash
ssh sxk@192.168.90.105
```

**远程压测连接参数**：

```bash
SB_REMOTE="--db-driver=mysql \
  --mysql-host=192.168.90.170 \
  --mysql-port=3308 \
  --mysql-user=sbtest \
  --mysql-password=sbtest123 \
  --mysql-db=sbtest"
```

> 远程压测经过物理网卡（10GbE），会有约 1ms 额外网络延迟。适用于模拟真实客户端-服务器部署场景。
>
> **MySQL 服务器硬件确认**（2026-04-27 更新）：MySQL 服务器 (192.168.90.170) 为 Huawei TaiShan 200 (2280), Kunpeng 920 × 2 / 376GB DDR4。MySQL 运行在该服务器的 8U32G 容器 `mysql-test-8u32g` 内，对外提供服务使用 hinic 10GbE 网卡（enp133s0）。压测客户端 (192.168.90.105) CPU 型号在 `lscpu` 中显示为 `-`（implementer=0x48 HiSilicon, part=0xd03），规格为 2 Socket × 48 Core × 2 Thread = 192 vCPU，主频 2.2 GHz，内存 502GB，登录用户为 `sxk`。

---

## 2. MySQL 部署指导

### 2.1 依赖安装

```bash
# 运行时依赖
yum install -y mysql mysql-server sysbench

# 编译依赖
yum install -y cmake ncurses-devel openssl-devel bison \
  libaio-devel rpcgen libtirpc-devel m4 gcc gcc-c++
```

### 2.2 源码编译 MySQL 8.0.25

#### 2.2.1 下载源码

```bash
cd /home/<user>/code

# 下载 MySQL 8.0.25 源码（通过代理）
export https_proxy=http://127.0.0.1:7890
curl -L -o mysql-8.0.25.tar.gz \
  https://github.com/mysql/mysql-server/archive/refs/tags/mysql-8.0.25.tar.gz
tar xzf mysql-8.0.25.tar.gz

# 手动下载 Boost 1.73.0（cmake 自动下载会失败，Bintray 已停服）
mkdir -p boost
curl -L -o boost/boost_1_73_0.tar.gz \
  "https://sourceforge.net/projects/boost/files/boost/1.73.0/boost_1_73_0.tar.gz/download"
```

#### 2.2.2 CMake 配置与编译

```bash
mkdir -p /home/<user>/code/mysql-8.0.25-build
cd /home/<user>/code/mysql-8.0.25-build

cmake /home/<user>/code/mysql-server-mysql-8.0.25 \
  -DCMAKE_INSTALL_PREFIX=/usr/local/mysql \
  -DMYSQL_DATADIR=/usr/local/mysql/data \
  -DSYSCONFDIR=/etc/mysql \
  -DWITH_BOOST=/home/<user>/code/boost \
  -DWITH_INNOBASE_STORAGE_ENGINE=1 \
  -DWITH_FEDERATED_STORAGE_ENGINE=1 \
  -DWITH_BLACKHOLE_STORAGE_ENGINE=1 \
  -DWITH_MYISAM_STORAGE_ENGINE=1 \
  -DENABLED_LOCAL_INFILE=1 \
  -DDEFAULT_CHARSET=utf8mb4 \
  -DDEFAULT_COLLATION=utf8mb4_unicode_ci \
  -DWITH_UNIT_TESTS=OFF \
  -DWITH_DEBUG=OFF \
  -DCMAKE_BUILD_TYPE=Release

# 编译（使用全部 128 核）
make -j$(nproc)
make install
```

#### 2.2.3 编译验证

```bash
/usr/local/mysql/bin/mysqld --version
# Ver 8.0.25 for Linux on aarch64 (Source distribution)

/usr/local/mysql/bin/mysql --version
# Ver 8.0.25 for Linux on aarch64 (Source distribution)
```

### 2.3 MySQL 配置

#### 2.3.1 NVMe 实例配置（端口 3307）

配置文件 `/etc/mysql/my_nvme.cnf`：

```ini
[mysqld]
basedir=/usr/local/mysql
datadir=/host/home/mysql/mysql_nvme/data
socket=/host/home/mysql/mysql_nvme/run/mysql.sock
log-error=/host/home/mysql/mysql_nvme/log/mysqld.log
pid-file=/host/home/mysql/mysql_nvme/run/mysqld.pid

character_set_server=utf8mb4
collation_server=utf8mb4_unicode_ci

port=3307
max_connections=500

innodb_buffer_pool_size=20G
innodb_buffer_pool_instances=8

innodb_log_file_size=1G
innodb_log_buffer_size=64M
innodb_flush_log_at_trx_commit=1
sync_binlog=1

# NVMe I/O 优化（比 HDD 高 50 倍）
innodb_io_capacity=10000
innodb_io_capacity_max=20000
innodb_flush_method=O_DIRECT

innodb_thread_concurrency=0
innodb_read_io_threads=8
innodb_write_io_threads=8
innodb_purge_threads=4
innodb_page_cleaners=4

table_open_cache=4000
table_definition_cache=2000
table_open_cache_instances=16

sort_buffer_size=4M
join_buffer_size=4M
read_buffer_size=2M
read_rnd_buffer_size=4M

innodb_file_per_table=ON
innodb_stats_persistent=ON
innodb_change_buffering=all
innodb_adaptive_hash_index=ON
slow_query_log=ON
long_query_time=2
```

#### 2.3.2 CPU/内存资源限制（模拟 8U32G）

```bash
# 方式一：systemd（物理机/虚拟机）
cp /usr/lib/systemd/system/mysqld.service /etc/systemd/system/mysqld.service
```

在 `/etc/systemd/system/mysqld.service` 的 `[Service]` 段末尾添加：

```ini
CPUAffinity=0 1 2 3 4 5 6 7
MemoryMax=24G
```

```bash
systemctl daemon-reload
```

```bash
# 方式二：taskset（容器/chroot 环境）
taskset -c 0-7 mysqld --user=mysql --datadir=/var/lib/mysql &
```

### 2.4 初始化与启动

#### 2.4.1 NVMe 实例

```bash
mkdir -p /host/home/mysql/mysql_nvme/{data,log,run}
chown -R mysql:mysql /host/home/mysql/mysql_nvme

/usr/local/mysql/bin/mysqld --defaults-file=/etc/mysql/my_nvme.cnf --initialize --user=mysql
grep 'temporary password' /host/home/mysql/mysql_nvme/log/mysqld.log

/usr/local/mysql/bin/mysqld --defaults-file=/etc/mysql/my_nvme.cnf --user=mysql &

/usr/local/mysql/bin/mysql -u root -p'<临时密码>' -S /host/home/mysql/mysql_nvme/run/mysql.sock \
  --connect-expired-password -e "ALTER USER 'root'@'localhost' IDENTIFIED BY 'Root@123456';"
```

#### 2.4.2 验证配置

```bash
/usr/local/mysql/bin/mysql -u root -p'Root@123456' -S /host/home/mysql/mysql_nvme/run/mysql.sock -e "
SHOW VARIABLES LIKE 'version';
SHOW VARIABLES LIKE 'innodb_buffer_pool_size';
SHOW VARIABLES LIKE 'innodb_io_capacity';
SHOW VARIABLES LIKE 'innodb_flush_method';
"
```

### 2.5 创建测试用户与数据库

```bash
/usr/local/mysql/bin/mysql -u root -p'Root@123456' \
  -S /host/home/mysql/mysql_nvme/run/mysql.sock << 'EOF'
CREATE DATABASE IF NOT EXISTS sbtest CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'sbtest'@'127.0.0.1' IDENTIFIED BY 'sbtest123';
GRANT ALL PRIVILEGES ON sbtest.* TO 'sbtest'@'127.0.0.1';
CREATE USER IF NOT EXISTS 'sbtest'@'localhost' IDENTIFIED BY 'sbtest123';
GRANT ALL PRIVILEGES ON sbtest.* TO 'sbtest'@'localhost';
FLUSH PRIVILEGES;
EOF
```

验证连接：

```bash
/usr/local/mysql/bin/mysql -u sbtest -psbtest123 -h 127.0.0.1 -P 3307 -e "SELECT 'connection OK';"
```

### 2.6 数据准备

> 以下步骤只需执行一次，生成的数据供只读测试使用。

#### 2.6.1 启动 NVMe 实例

```bash
# 创建目录（首次）
mkdir -p /host/home/mysql/mysql_nvme/{data,log,run}
chown -R mysql:mysql /host/home/mysql/mysql_nvme

# 启动 MySQL NVMe 实例（端口 3307）
/usr/local/mysql/bin/mysqld --defaults-file=/etc/mysql/my_nvme.cnf --user=mysql &

# 等待启动就绪
sleep 5
/usr/local/mysql/bin/mysqladmin -u root -p'Root@123456' \
  -S /host/home/mysql/mysql_nvme/run/mysql.sock ping
# mysqld is alive
```

#### 2.6.2 创建测试用户与数据库

```bash
/usr/local/mysql/bin/mysql -u root -p'Root@123456' \
  -S /host/home/mysql/mysql_nvme/run/mysql.sock << 'EOF'
CREATE DATABASE IF NOT EXISTS sbtest CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'sbtest'@'127.0.0.1' IDENTIFIED BY 'sbtest123';
GRANT ALL PRIVILEGES ON sbtest.* TO 'sbtest'@'127.0.0.1';
CREATE USER IF NOT EXISTS 'sbtest'@'localhost' IDENTIFIED BY 'sbtest123';
GRANT ALL PRIVILEGES ON sbtest.* TO 'sbtest'@'localhost';
FLUSH PRIVILEGES;
EOF
```

#### 2.6.3 生成测试数据（64 表 × 1000 万行）

```bash
sysbench oltp_read_only \
  --db-driver=mysql \
  --mysql-host=127.0.0.1 \
  --mysql-port=3307 \
  --mysql-user=sbtest \
  --mysql-password=sbtest123 \
  --mysql-db=sbtest \
  --mysql-socket=/host/home/mysql/mysql_nvme/run/mysql.sock \
  --tables=64 \
  --table-size=10000000 \
  --threads=64 \
  prepare
```

> 预计耗时约 45 分钟（NVMe SSD，64 线程并行插入）。

#### 2.6.4 数据验证

```bash
# 验证每表行数
/usr/local/mysql/bin/mysql -u sbtest -psbtest123 -h 127.0.0.1 -P 3307 \
  --mysql-db=sbtest -e "
SELECT table_name, table_rows, ROUND(data_length/1024/1024, 2) AS 'data_MB',
       ROUND(index_length/1024/1024, 2) AS 'index_MB'
FROM information_schema.tables
WHERE table_schema='sbtest'
ORDER BY table_name;
"
```

数据验证结果：

| 项目 | 值 |
|------|------|
| 测试表数量 | 64 张 |
| 每表行数 | 10,000,000（精确） |
| 总数据量 | 128.79 GB |
| 数据存储目录 | `/host/home/mysql/mysql_nvme/data/sbtest/` |
| 二级索引 | 64 个（每表 1 个，已创建完成） |

> 只读测试不修改数据，可多次运行而无需重新 prepare。

### 2.7 测试执行命令

> 以下测试均基于 NVMe 实例（端口 3307）上已生成的 64 表 × 1000 万行数据。

#### 2.7.1 公共参数

```bash
# NVMe 实例公共参数（64 表 × 1000 万行）
SB_NVME="--db-driver=mysql \
  --mysql-host=127.0.0.1 --mysql-port=3307 \
  --mysql-user=sbtest --mysql-password=sbtest123 \
  --mysql-db=sbtest \
  --mysql-socket=/host/home/mysql/mysql_nvme/run/mysql.sock \
  --tables=64 --table-size=10000000"

# 结果输出目录
RESULT_DIR="./sysbench_results"
mkdir -p "$RESULT_DIR"
```

#### 2.7.2 TC-01：只读测试 (oltp_read_only)

```bash
# 预热（将数据加载到 Buffer Pool）
sysbench oltp_read_only $SB_NVME --threads=40 --time=60 run

# 正式测试（40 并发，120 秒）
echo "===== TC-01 oltp_read_only threads=40 ====="
sysbench oltp_read_only $SB_NVME \
  --threads=40 --time=120 --report-interval=10 \
  run 2>&1 | tee ${RESULT_DIR}/oltp_read_only_t40.log
```

### 2.8 远程只读压测

> 以下测试基于远程压测组网（见 [1.3 远程压测组网](#13-远程压测组网)），
> Sysbench 运行在客户端 192.168.90.105，MySQL 运行在服务器 192.168.90.170:3308。

#### 2.8.1 前置准备

```bash
# 确认 MySQL 运行状态（服务端 192.168.90.170）
docker ps --filter name=mysql-test-8u32g
pgrep -a mysqld | grep sxk_optimized

# 确认连接
/usr/local/mysql/bin/mysql -u sbtest -psbtest123 -h 192.168.90.170 -P 3308 -e "SELECT VERSION();"

# 确认优化参数
/usr/local/mysql/bin/mysql -u sbtest -psbtest123 -h 192.168.90.170 -P 3308 -e "
SHOW VARIABLES LIKE 'innodb_buffer_pool_size';
SHOW VARIABLES LIKE 'innodb_adaptive_hash_index';
SHOW VARIABLES LIKE 'innodb_doublewrite';
SHOW VARIABLES LIKE 'performance_schema';
"
```

#### 2.8.2 公共参数

```bash
SB_REMOTE="--db-driver=mysql \
  --mysql-host=192.168.90.170 \
  --mysql-port=3308 \
  --mysql-user=sbtest \
  --mysql-password=sbtest123 \
  --mysql-db=sbtest \
  --tables=64 \
  --table-size=10000000"
```

#### 2.8.3 不同并发对比

```bash
for t in 40 64 128 256; do
  echo "===== threads=$t ====="
  sysbench oltp_read_only $SB_REMOTE \
    --threads=$t \
    --time=120 \
    --report-interval=10 \
    run 2>&1 | tee /tmp/sysbench_t${t}.log
  sleep 10
done
```

---

## 3. 测试结果与分析

> 测试时间：2026-04-14
>
> 环境：Kunpeng 920 (8 vCPU) / 32 GB RAM / openEuler 22.03 SP2 / MySQL 8.0.25 (自编译) / Sysbench 1.0.20
>
> 容器限制：`--cpuset-cpus=32-39`（8 核） / `-m 32g` / mysqld `taskset -c 32-39`
>
> 配置：NVMe SSD (Huawei HWE56P436T4M005N) / innodb_buffer_pool_size=20G / innodb_io_capacity=10000 / innodb_flush_log_at_trx_commit=1
>
> 数据：64 表 × 10,000,000 行，总数据量 ~128.79 GB

### 3.1 只读测试结果 (oltp_read_only)

| 并发线程数 | TPS | QPS | 平均延迟 (ms) | P95 延迟 (ms) | 最大延迟 (ms) |
|-----------|------|------|--------------|--------------|--------------|
| 40 | 3,605.06 | 57,681.04 | 11.09 | 16.12 | 104.31 |

### 3.2 结果分析

1. **CPU 是 8U 场景的核心瓶颈** — 只读 40 并发下 mysqld CPU 接近 800%（8 核满载），TPS 稳定在 3,605，无法继续提升。对比 128 核场景的 12,950 TPS，8U 场景性能约为全核的 **28%**，符合 8/128 的核心比例。

2. **资源利用率**：mysqld RSS 约 22.4 GB，在 32GB 容器限制内（< 70%）。测试期间无 OOM，内存不是瓶颈。

3. **性能提升建议**：
   - 将 `innodb_buffer_pool_size` 从 20G 提升至 24G 可更好利用 32GB 内存（留 8G 给 OS 和连接）
   - 将 `innodb_flush_log_at_trx_commit` 设为 `2` 可减少 fsync 次数（牺牲少量持久性）

---

## 4. 各阶段耗时统计

> 以下为 2026-04-14 实测耗时统计

### 4.1 编译部署阶段

| 步骤 | 耗时 | 说明 |
|------|------|------|
| 源码下载 (mysql-8.0.25.tar.gz, 271MB) | ~3 min | 通过 clash 代理下载 |
| Boost 1.73.0 下载 (122MB) | ~1 min | 从 SourceForge 下载 |
| 安装编译依赖 | ~1 min | yum install cmake, ncurses-devel 等 |
| CMake 配置 | ~2 min | 含 Boost 解压 |
| make -j128 编译 | ~25 min | 128 核并行编译 |
| make install | ~1 min | 安装至 /usr/local/mysql |
| MySQL 初始化 | ~18s | InnoDB 初始化 + 生成临时密码 |
| 启动 + 密码修改 + 创建用户 | ~30s | ALTER USER + CREATE DATABASE |
| **编译部署小计** | **~35 min** | 首次部署，后续无需重复 |

### 4.2 数据准备阶段（NVMe，64 表 × 1000 万行）

| 步骤 | 耗时 | 说明 |
|------|------|------|
| NVMe 实例初始化 | ~20s | mysqld --initialize |
| 数据插入 | ~35 min | 64 表 × 1000 万行，64 线程并行 |
| 二级索引创建 | ~10 min | 64 个索引并行创建 |
| **小计** | **~45 min** | NVMe SSD |

### 4.3 性能测试阶段（NVMe，64 表 × 1000 万行）

| 步骤 | 耗时 | 说明 |
|------|------|------|
| TC-01 只读 预热 (40 threads, 60s) | 60.0s | Buffer Pool 预热 |
| TC-01 只读 测试 (40 threads, 120s) | 120.0s | TPS 3,605 |
| **测试小计** | **~3 min** | 预热60s + 测试120s |

### 4.4 全流程汇总

| 阶段 | 耗时 | 说明 |
|------|------|------|
| 源码编译部署 | ~35 min | 首次部署 |
| NVMe 数据准备 | ~45 min | 64 表 × 1000 万行 |
| 性能测试（只读） | ~3 min | 只读 40 并发 |
| **总计** | **~83 min (~1h 23min)** | 编译+数据准备占 96% |

---

## 5. 附录

### 5.1 一键测试脚本

创建 `mysql_sysbench_benchmark.sh`：

```bash
#!/bin/bash
# MySQL Sysbench 只读性能测试一键脚本
# 适用：NVMe SSD / MySQL 8.0.25 / Sysbench 1.0.20
# 测试场景：只读(40并发)
# 前提：已生成 64 表 × 1000 万行数据

set -euo pipefail

# ==================== 配置区 ====================
MYSQL_HOST="127.0.0.1"
MYSQL_PORT="3307"
MYSQL_USER="sbtest"
MYSQL_PASSWORD="sbtest123"
MYSQL_DB="sbtest"
MYSQL_SOCKET="/host/home/mysql/mysql_nvme/run/mysql.sock"
MYSQL_ROOT_SOCKET="$MYSQL_SOCKET"
MYSQL_ROOT_PASSWORD="Root@123456"

TABLES=64
TABLE_SIZE=10000000
TIME=120
WARMUP_TIME=60
REPORT_INTERVAL=10

# 只读并发配置
READ_ONLY_THREADS=40

RESULT_DIR="./sysbench_results_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$RESULT_DIR"

# ==================== 颜色输出 ====================
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_step()  { echo -e "${BLUE}[STEP]${NC} $1"; }
log_header() { echo -e "\n${BOLD}${BLUE}========================================${NC}"; echo -e "${BOLD}${BLUE}  $1${NC}"; echo -e "${BOLD}${BLUE}========================================${NC}"; }

# ==================== 公共参数 ====================
SYSBENCH_BASE="--db-driver=mysql \
  --mysql-host=$MYSQL_HOST \
  --mysql-port=$MYSQL_PORT \
  --mysql-user=$MYSQL_USER \
  --mysql-password=$MYSQL_PASSWORD \
  --mysql-db=$MYSQL_DB \
  --mysql-socket=$MYSQL_SOCKET \
  --tables=$TABLES \
  --table-size=$TABLE_SIZE"

# ==================== 前置检查 ====================
preflight_check() {
    log_step "执行前置检查..."

    if ! command -v sysbench &>/dev/null; then
        log_error "sysbench 未安装"
        exit 1
    fi
    log_info "sysbench $(sysbench --version)"

    if ! /usr/local/mysql/bin/mysql -u $MYSQL_USER -p$MYSQL_PASSWORD \
         -h $MYSQL_HOST -P $MYSQL_PORT -e "SELECT 1;" &>/dev/null; then
        log_error "无法连接 MySQL (port=$MYSQL_PORT)，请检查服务状态"
        exit 1
    fi
    MYSQL_VER=$(/usr/local/mysql/bin/mysql -u $MYSQL_USER -p$MYSQL_PASSWORD \
      -h $MYSQL_HOST -P $MYSQL_PORT -B -e "SELECT VERSION();" 2>/dev/null | tail -1)
    log_info "MySQL 连接正常 ($MYSQL_VER)"

    # 检查测试数据是否存在
    TABLE_COUNT=$(/usr/local/mysql/bin/mysql -u $MYSQL_USER -p$MYSQL_PASSWORD \
      -h $MYSQL_HOST -P $MYSQL_PORT -B -e \
      "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='$MYSQL_DB';" 2>/dev/null | tail -1)
    if [[ "$TABLE_COUNT" -lt "$TABLES" ]]; then
        log_error "测试表不足: 期望 $TABLES 张, 实际 $TABLE_COUNT 张, 请先执行数据准备"
        exit 1
    fi
    log_info "测试数据: ${TABLE_COUNT} 张表 × ${TABLE_SIZE} 行/表"

    log_info "CPU: $(nproc) vCPU ($(uname -m))"
    log_info "内存: $(free -h | awk '/Mem:/{print $2}')"
    log_info "所有前置检查通过"
}

# ==================== 运行测试 ====================
run_test() {
    local test_type=$1
    local threads=$2
    local result_file="$RESULT_DIR/${test_type}_t${threads}.log"

    # 预热
    log_step "${test_type} 预热 (${threads} threads, ${WARMUP_TIME}s)..."
    sysbench $test_type $SYSBENCH_BASE \
        --threads=$threads --time=$WARMUP_TIME \
        run 2>&1 | tail -5

    # 正式测试
    log_step "${test_type} 正式测试 (${threads} threads, ${TIME}s)..."
    sysbench $test_type $SYSBENCH_BASE \
        --threads=$threads \
        --time=$TIME \
        --report-interval=$REPORT_INTERVAL \
        run 2>&1 | tee "$result_file"

    # 提取结果
    local tps qps latency_avg latency_95 latency_max
    tps=$(grep 'transactions:' "$result_file" | grep -oP '\([\d.]+ per sec\.\)' | tr -d '()' | awk '{print $1}')
    qps=$(grep 'queries:' "$result_file" | grep -oP '\([\d.]+ per sec\.\)' | tr -d '()' | awk '{print $1}')
    latency_avg=$(grep 'avg:' "$result_file" | head -1 | awk '{print $2}')
    latency_95=$(grep '95th percentile:' "$result_file" | awk '{print $2}')
    latency_max=$(grep 'max:' "$result_file" | head -1 | awk '{print $2}')

    echo -e "  ${GREEN}结果摘要${NC}: TPS=${tps} | QPS=${qps} | Avg=${latency_avg}ms | P95=${latency_95}ms | Max=${latency_max}ms"
    echo ""
}

# ==================== 汇总报告 ====================
generate_summary() {
    local summary_file="$RESULT_DIR/summary.txt"

    {
        echo "=========================================="
        echo "  MySQL Sysbench 只读性能测试汇总"
        echo "  测试时间: $(date '+%Y-%m-%d %H:%M:%S')"
        echo "=========================================="
        echo "MySQL: $MYSQL_VER"
        echo "数据: ${TABLES} 表 × ${TABLE_SIZE} 行 (NVMe SSD)"
        echo "测试时长: 预热${WARMUP_TIME}s + 正式${TIME}s"
        echo ""
        printf "%-20s %6s %12s %12s %10s %10s %10s\n" \
            "测试场景" "并发" "TPS" "QPS" "Avg(ms)" "P95(ms)" "Max(ms)"
        printf "%-20s %6s %12s %12s %10s %10s %10s\n" \
            "--------" "------" "----------" "----------" "----------" "----------" "----------"

        for result_file in "$RESULT_DIR"/*.log; do
            [ -f "$result_file" ] || continue
            local fname=$(basename "$result_file" .log)
            local test_type=$(echo "$fname" | sed 's/_t[0-9]*$//')
            local threads=$(echo "$fname" | grep -oP 't\K[0-9]+')

            local tps qps latency_avg latency_95 latency_max
            tps=$(grep 'transactions:' "$result_file" | grep -oP '\([\d.]+ per sec\.\)' | tr -d '()' | awk '{print $1}')
            qps=$(grep 'queries:' "$result_file" | grep -oP '\([\d.]+ per sec\.\)' | tr -d '()' | awk '{print $1}')
            latency_avg=$(grep 'avg:' "$result_file" | head -1 | awk '{print $2}')
            latency_95=$(grep '95th percentile:' "$result_file" | awk '{print $2}')
            latency_max=$(grep 'max:' "$result_file" | head -1 | awk '{print $2}')

            printf "%-20s %6s %12s %12s %10s %10s %10s\n" \
                "$test_type" "$threads" "${tps:-N/A}" "${qps:-N/A}" \
                "${latency_avg:-N/A}" "${latency_95:-N/A}" "${latency_max:-N/A}"
        done

        echo ""
        echo "详细日志目录: $RESULT_DIR/"
    } | tee "$summary_file"
}

# ==================== 主流程 ====================
main() {
    echo "========================================"
    echo "  MySQL Sysbench 只读性能测试"
    echo "  只读(${READ_ONLY_THREADS}t)"
    echo "========================================"
    echo ""

    START_TIME=$(date +%s)

    preflight_check

    # TC-01: 只读测试
    log_header "TC-01: 只读测试 (oltp_read_only, ${READ_ONLY_THREADS} 并发)"
    run_test oltp_read_only $READ_ONLY_THREADS

    END_TIME=$(date +%s)
    ELAPSED=$((END_TIME - START_TIME))

    log_header "测试完成"
    generate_summary
    log_info "总耗时: $((ELAPSED / 60))分$((ELAPSED % 60))秒"
    log_info "结果保存在 $RESULT_DIR/"
}

main "$@"
```

使用方式：

```bash
# 前提：NVMe 实例已启动，64 表 × 1000 万行数据已生成
chmod +x mysql_sysbench_benchmark.sh
./mysql_sysbench_benchmark.sh 2>&1 | tee test_run.log
```

### 5.2 服务端监控与瓶颈排查

> 压测期间在服务端另开终端执行以下监控命令。远程压测场景下建议同时在客户端和服务器两侧监控。

#### CPU 利用率

```bash
# mysqld 进程级（usr+sys，8核=800%）
pidstat -C mysqld 1

# 线程级分布（观察线程负载是否均匀）
pidstat -C mysqld -t 1 5

# top 方式（关注 mysqld 进程）
top -p $(pgrep mysqld) -H
```

#### 磁盘 I/O

```bash
# 指定设备监控（NVMe SSD）
iostat -xmd nvme0n1 5

# 所有设备概览
iostat -xmd 5
```

#### 网络

```bash
# 网卡流量
sar -n DEV 5

# MySQL 连接数统计
ss -t state established '( dport = :3308 or sport = :3308 )' | wc -l
```

#### CPU 热点函数（需 root）

```bash
sudo perf record -g -p $(pgrep -f "my_sxk_optimized") -- sleep 30
sudo perf report --stdio --no-children | head -30
```

#### InnoDB 状态

```bash
# Buffer Pool 命中率 + 连接数
/usr/local/mysql/bin/mysql -u sbtest -psbtest123 -h 192.168.90.170 -P 3308 -e "
SHOW GLOBAL STATUS LIKE 'Innodb_buffer_pool_read%';
SHOW GLOBAL STATUS LIKE 'Threads_connected';
"

# InnoDB 引擎详细状态
mysql -u sbtest -psbtest123 -e "SHOW ENGINE INNODB STATUS\G" | grep -A5 "OS FILE"
```

#### 系统级概览

```bash
# 系统级 I/O 等待
vmstat 5
```

### 5.3 测试后清理

```bash
# 清理 NVMe 实例测试数据
sysbench oltp_read_only \
  --db-driver=mysql --mysql-host=127.0.0.1 --mysql-port=3307 \
  --mysql-user=sbtest --mysql-password=sbtest123 --mysql-db=sbtest \
  --mysql-socket=/host/home/mysql/mysql_nvme/run/mysql.sock \
  --tables=64 cleanup

# 删除测试数据库和用户
/usr/local/mysql/bin/mysql -u root -p'Root@123456' -S /host/home/mysql/mysql_nvme/run/mysql.sock -e "
DROP DATABASE IF EXISTS sbtest;
DROP USER IF EXISTS 'sbtest'@'127.0.0.1';
DROP USER IF EXISTS 'sbtest'@'localhost';
"

# 停止 MySQL
mysqladmin -u root -p'Root@123456' -S /host/home/mysql/mysql_nvme/run/mysql.sock shutdown
```

### 5.4 注意事项

1. **CPU 绑核**：将 MySQL 限制在 8 个核心上，sysbench 客户端运行在其他核心上避免争抢。

2. **内存限制**：`MemoryMax=24G` 限制 MySQL 进程组最大内存，`innodb_buffer_pool_size=20G` 确保 InnoDB 缓冲池在限制范围内。

3. **数据预热**：首次测试前先运行一轮短时间预热（`--time=30`），让数据加载到 Buffer Pool，避免冷启动影响结果。

4. **多次测试取均值**：建议每种线程数至少跑 2-3 次，取 TPS/QPS 均值，减少波动。

5. **只读不修改数据**：`oltp_read_only` 不会修改任何数据，可放心多次运行。
