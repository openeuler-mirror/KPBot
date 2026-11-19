# 3FS 主安装手册（鲲鹏平台 / openEuler 24.03，920 实测）

> **何时读我**：从零搭建 3FS 集群（或整体重建全部组件）时，按本文顺序执行；编译环境与编译坑位细节见 `build-notes.md`，部署完成后的日常运维见 `cluster-operations.md`。
>
> 本文由两套真实集群的部署实录合并而成（stage 集群：2 storage + 3 FUSE 客户端；tester 集群：3 节点从零安装），冲突处以更详细/更新的记录为准，环境差异以注记保留。全文已脱敏参数化：命令一律引用下方集群变量，示例 IP 使用文档保留网段 192.0.2.x / 198.51.100.x / 203.0.119.x，**非真实环境地址**。

## 集群变量表

执行前先在本机 shell 导出（或写入自维护的 env 脚本），全文命令直接引用：

```shell
export CLUSTER_ID=stage                    # 集群 ID
export META_IP=192.0.2.10                  # meta 节点 RDMA 网 IP（示例）
export STORAGE1_IP=192.0.2.11              # storage1 RDMA 网 IP（示例）
export STORAGE2_IP=192.0.2.12              # storage2 RDMA 网 IP（示例）
export FUSE_CLIENT_IPS="192.0.2.20"        # FUSE 客户端 RDMA 网 IP 列表（示例，可多台）
export MGMT_NET=198.51.100.0/24            # 管理网段（示例）
export RDMA_NET=192.0.2.0/24               # RDMA/业务网段（示例）
export RDMA_IFACE=enp43s0f0np0             # 本节点 RDMA 网卡名（filter_list 用，逐节点确认）
export SSH_USER=root                       # 节点间 SSH 用户
export SRC_ROOT=/opt/src/3FS               # 源码根目录（含 3fs/ 与 fio/）
export BUILD_DIR=${SRC_ROOT}/3fs/build     # 编译输出目录
export INSTALL_PREFIX=/opt/3fs             # 线上安装前缀
export MOUNT_POINT=/3fs/stage              # FUSE 挂载点
export CK_PORT=9123                        # ClickHouse TCP 端口
export FDB_PORT=4500                       # FoundationDB 端口
# ClickHouse 密码不入库，用时交互注入：
#   read -s -p 'ClickHouse password: ' CK_PASS; export CK_PASS
```

主机角色名 meta / storage1 / storage2 / client2 为示例角色名；node_id（1、100、10001、10002）、
chain_id（900100001）、target_id（101000100101）等为**示例编号方案，按集群规划替换**。

## 1. 环境要求与规划

### 1.1 硬件要求

**客户端**

| 项目 | 规格 |
|------|------|
| CPU 型号 | 鲲鹏系列处理器（实录为鲲鹏 920） |
| 核心数 | 2 × 48 |
| 主频 | 2600MHz |
| 内存 | 16 × 32 GB，2666MHz |
| 网卡 | Mellanox 100GE ConnectX-5（mlx5_0/mlx5_1 双口） |

**服务端（storage 节点）**：在客户端规格基础上增加 4 × 3.2TB NVMe SSD（实录型号 HWE62P443T2M002N，单盘可用 2.9T）。

> 上表为实录验证环境（鲲鹏 920）的规格；其它鲲鹏机型按同等核数/内存/RDMA 网卡能力对照即可，部署步骤与具体机型无强绑定。

### 1.2 软件要求

| 项目 | 版本 | 获取地址 |
|------|------|---------|
| 操作系统 | openEuler 24.03 LTS SP3 | https://mirrors.huaweicloud.com/openeuler/openEuler-24.03-LTS-SP3/ISO/aarch64/openEuler-24.03-LTS-SP3-everything-aarch64-dvd.iso |
| Rust | 1.85.0 | https://static.rust-lang.org/dist/rust-1.85.0-aarch64-unknown-linux-gnu.tar.xz |
| FoundationDB Client | 7.3.63 | https://github.com/apple/foundationdb/releases/download/7.3.63/foundationdb-clients-7.3.63-1.el9.aarch64.rpm |
| FoundationDB Server | 7.3.63 | https://github.com/apple/foundationdb/releases/download/7.3.63/foundationdb-server-7.3.63-1.el9.aarch64.rpm |
| 毕昇编译器 | 4.2.0 | https://mirrors.huaweicloud.com/kunpeng/archive/compiler/bisheng_compiler/BiShengCompiler-4.2.0-aarch64-linux.tar.gz |
| Libfuse | 3.16.1 | https://github.com/libfuse/libfuse/releases/download/fuse-3.16.1/fuse-3.16.1.tar.gz |

> **注记（环境差异）**：实录 stage 集群各节点操作系统为 openEuler 24.03 LTS 的 SP2/SP3/SP4 混合部署
> （meta 与一台 FUSE 客户端为 SP2、storage1 为 SP4、storage2 与另一 FUSE 客户端为 SP3），
> 安装时统一使用 SP3 ISO 即可。

### 1.3 节点角色规划（示例）

| 节点 | 管理网 IP | RDMA 网 IP | 角色 |
|------|-----------|-----------|------|
| meta | 198.51.100.10 | ${META_IP} (192.0.2.10) | MGMTD + META + MonitorCollector + ClickHouse + FDB + 编译节点（+ 可选 FUSE Client） |
| storage1 | 198.51.100.11 | ${STORAGE1_IP} (192.0.2.11) | Storage Service（node_id: 10001） |
| storage2 | 198.51.100.12 | ${STORAGE2_IP} (192.0.2.12) | Storage Service（node_id: 10002） |
| client2 等 | 198.51.100.20 | ${FUSE_CLIENT_IPS} (192.0.2.20 …) | FUSE 客户端（可多台） |

- 3FS 服务间通信全部走 RDMA 网段 `${RDMA_NET}`；`/etc/hosts` 中角色名应解析到 RDMA 网 IP。
- 3FS 源码位于 `${SRC_ROOT}/3fs`（实录 commit 497941599c2ed7cefe2878b5c35dbd18f25a0415）。
- 二进制在 meta 节点（编译节点）编译，rsync 分发到各节点。
- RDMA 网卡：Mellanox ConnectX-5 100Gb/s 双口（mlx5_0、mlx5_1）。IB 模式下网络设备名为
  ibp129s0f0、ibp129s0f1；以太（RoCE）模式下形如 enp43s0f0np0。用 `ibdev2netdev` / `rdma link` 查询，
  **各节点网卡名可能不同（异构），配置 filter_list 前必须逐节点确认**。
- 端口规划：4500（FDB）、8000（MGMTD/Storage 主服务）、8001（META 主服务）、9000（Core Service，MGMTD/Storage）、
  9001（Core Service，META）、${CK_PORT}（ClickHouse TCP，默认 9123）、10000（MonitorCollector）。
  完整端口表见 `cluster-operations.md` §8。
- 编号方案（示例）：mgmtd node_id=1，meta node_id=100，storage node_id=10001 起；
  chain/target ID 由 gen_chain_table.py 的前缀参数决定（见 §5.8）。

## 2. 部署前置检查

在**每个**参与部署的节点执行只读检查脚本（有 FAIL 项时退出码非零）：

```shell
EXPECT_HOSTNAME=meta CLUSTER_HOSTS="meta storage1 storage2" \
RDMA_IFACE=${RDMA_IFACE} NODE_ROLE=meta bash scripts/preflight_check.sh
```

覆盖：hostname/hosts 解析、RDMA link 状态与网卡名、时间同步偏差、必需端口空闲、依赖包存在性。
手工核对要点（详见脚本输出与 `fuse-client-expansion.md` §2）：

```shell
hostname
ip addr show ${RDMA_IFACE}
rdma link                       # 对应设备应为 state ACTIVE，netdev 为本节点 RDMA 网卡名
ping -c 3 ${META_IP}            # 逐节点互 ping RDMA IP
```

`/etc/hosts` 至少包含（角色名解析到 RDMA IP）：

```txt
192.0.2.10 meta
192.0.2.11 storage1
192.0.2.12 storage2
```

## 3. 环境准备

### 3.1 关闭防火墙与设置 SELinux（所有节点）

> **安全说明**：
> - 仅在可信内网或离线测试环境中关闭防火墙；生产环境必须启用防火墙并精确放行 §1.3 所列端口。
> - 禁用 SELinux 将使强制访问控制（MAC）失效，建议仅在测试环境使用 permissive；
>   生产环境应保持 enforcing，并通过 audit2allow 生成适配业务的自定义策略。

```shell
systemctl stop firewalld
systemctl disable firewalld
systemctl status firewalld
```

修改 `/etc/selinux/config`：

```ini
SELINUX=permissive
```

重启服务器使配置生效（扩容场景可用 `setenforce 0` 即时生效 + 改配置文件持久化）。

### 3.2 修改 hostname 与设置 hosts（所有节点）

```shell
hostnamectl --static set-hostname meta        # 各节点分别设为规划的角色名
vi /etc/hosts                                  # 填入 §2 所示的角色名 → RDMA IP 映射
```

> **注记**：实录环境的 /etc/hosts 还维护了管理网/RDMA 网的双份别名映射（同一台机器管理网名与
> RDMA 网名各一条），可按需添加；已停机的历史节点条目可保留或删除。

### 3.3 集群时间同步（所有节点）

openEuler 24.03 默认集成 chrony：

```shell
systemctl enable --now chronyd
systemctl status chronyd
chronyc tracking
chronyc sources -v
```

`/etc/chrony.conf` 默认使用公网池：

```txt
pool pool.ntp.org iburst
```

（可选）内网/离线环境：指定一台节点作内网 NTP 服务器，在该节点 `/etc/chrony.conf` 添加
`allow ${MGMT_NET}`（示例：`allow 198.51.100.0/24`），其余节点改为 `server <内网NTP节点IP> iburst`。
实录 stage 集群的 storage1 即按此方式指向另一台节点。无 chrony 的节点可用 ntpdate + crontab 方案，
见 `fuse-client-expansion.md` §4。

## 4. 构建编译环境与源码编译（编译节点）

完整步骤、依赖清单与全部编译坑位见 **`build-notes.md`**。概要（仅在 meta/编译节点执行）：

1. `yum install` 编译依赖（cmake、boost、libibverbs、jemalloc、meson 等，全清单见 build-notes.md §1）。
2. 安装 Rust 1.85.0、FoundationDB 7.3.63（client + server RPM）、毕昇编译器 4.2.0、Libfuse 3.16.1。
3. 获取 3FS 源码（git clone + checkout 目标 commit + submodule + `./patches/apply.sh`），
   合入 openEuler 适配补丁（openeuler_3fs.patch / openeuler_3fs_folly.patch）。
4. （可选性能优化）切换 folly 到 ARM CRC32C 硬件加速分支。
5. 完成**三处必需 CMake 修改**（zstd BUILD_INTERFACE、删 -fcoroutines-ts、FAIL_ON_WARNINGS=OFF，
   详见 build-notes.md §5），然后：

   ```shell
   cd ${SRC_ROOT}/3fs
   cmake -S . -B build -DCMAKE_CXX_COMPILER=clang++ -DCMAKE_C_COMPILER=clang \
         -DCMAKE_BUILD_TYPE=RelWithDebInfo -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
   cmake --build build -j
   ls build/bin          # 应产出 admin_cli、mgmtd_main、meta_main、storage_main、
                         # monitor_collector_main、hf3fs_fuse_main 等
   ```

6. 编译 fio 与 USRBIO 插件 hf3fs_usrbio.so（后续原生客户端性能测试用，见 build-notes.md §7）。

## 5. 部署 3FS

部署顺序：ClickHouse → FoundationDB → monitor_collector → admin_cli（所有节点）→ mgmtd → meta →
storage → admin user/targets/chain table → FUSE 客户端。除特别说明外，rsync 均以编译节点（示例为 meta）
为源；在目标节点执行。

### 5.1 安装 ClickHouse（meta 节点）

1. 获取安装包并安装：

   ```shell
   cd /home
   curl -k https://clickhouse.com/ | sh
   sudo ./clickhouse install
   ```

2. 出现 `Allow server to accept connections from the network (default is localhost only), [y/N]` 时选 `y`。
3. 安装完成时按提示输入密码（即 `${CK_PASS}`，用 `read -s` 注入记忆，**不要写入任何文档/脚本**；
   如使用其他密码，后续所有 ClickHouse 配置同步替换）。
4. 修改默认端口：

   ```shell
   chmod 660 /etc/clickhouse-server/config.xml
   vim /etc/clickhouse-server/config.xml
   ```

   定位 `<tcp_port>` 标签，改为：

   ```xml
   <tcp_port>9123</tcp_port>    <!-- ${CK_PORT} -->
   ```

5. 启动并创建 Metric table：

   ```shell
   clickhouse start
   read -s -p 'ClickHouse password: ' CK_PASS; export CK_PASS; printf '\n'
   clickhouse-client --port ${CK_PORT} --password "${CK_PASS}" -n < ${SRC_ROOT}/3fs/deploy/sql/3fs-monitor.sql
   ```

### 5.2 更新 FoundationDB 配置（meta 节点）

1. 修改 `/etc/foundationdb/foundationdb.conf`，定位 `[fdbserver]` 下的 `public-address`，
   改为本机 RDMA 网 IP（`$ID` 为 FDB 自身的占位符，保持原样）：

   ```ini
   public-address = 192.0.2.10:$ID    ; 即 ${META_IP}:$ID
   ```

2. 修改 `/etc/foundationdb/fdb.cluster`，将 `127.0.0.1:4500` 改为：

   ```txt
   192.0.2.10:4500    ; 即 ${META_IP}:${FDB_PORT}
   ```

3. 重启并验证：

   ```shell
   systemctl restart foundationdb.service
   systemctl status foundationdb.service
   ss -tuln            # 应看到 ${META_IP}:${FDB_PORT} 监听
   ```

### 5.3 部署 monitor_collector（meta 节点）

1. 从编译节点获取二进制、配置与 systemd 单元：

   ```shell
   mkdir -p /var/log/3fs
   mkdir -p ${INSTALL_PREFIX}/{bin,etc}
   rsync -avz meta:${SRC_ROOT}/3fs/build/bin/monitor_collector_main ${INSTALL_PREFIX}/bin/
   rsync -avz meta:${SRC_ROOT}/3fs/configs/monitor_collector_main.toml ${INSTALL_PREFIX}/etc/
   rsync -avz meta:${SRC_ROOT}/3fs/deploy/systemd/monitor_collector_main.service /usr/lib/systemd/system
   ```

2. 修改 `${INSTALL_PREFIX}/etc/monitor_collector_main.toml`：

   ```toml
   [server.base.groups.listener]
   filter_list = ['enp43s0f0np0']    # 填入本节点 RDMA 网卡名（rdma link / ibdev2netdev 查询）
   listen_port = 10000
   listen_queue_depth = 4096
   rdma_listen_ethernet = true
   reuse_port = false
   # custom_tcp_nic_prefix = "ibp"   # 仅 IB 模式环境需要（网卡名形如 ibp129s0f0 时）

   [server.monitor_collector.reporter.clickhouse]
   db = '3fs'
   host = '127.0.0.1'
   passwd = '<CK_PASS 实际值>'        # 本文件含凭据，建议 chmod 600
   port = '9123'                      # ${CK_PORT}
   user = 'default'
   ```

3. 启动并检查：

   ```shell
   systemctl daemon-reload
   systemctl start monitor_collector_main
   systemctl status monitor_collector_main
   ss -tuln            # 应看到 ${META_IP}:10000 或 0.0.0.0:10000 监听
   ```

### 5.4 安装管理工具 admin_cli（所有节点）

1. 从编译节点获取二进制与配置：

   ```shell
   mkdir -p /var/log/3fs
   mkdir -p ${INSTALL_PREFIX}/{bin,etc}
   rsync -avz meta:${SRC_ROOT}/3fs/build/bin/admin_cli ${INSTALL_PREFIX}/bin
   rsync -avz meta:${SRC_ROOT}/3fs/configs/admin_cli.toml ${INSTALL_PREFIX}/etc
   rsync -avz meta:/etc/foundationdb/fdb.cluster ${INSTALL_PREFIX}/etc
   rsync -avz meta:/usr/lib64/libfdb_c.so /usr/lib64/
   ```

2. 修改 `${INSTALL_PREFIX}/etc/admin_cli.toml`：

   ```toml
   cluster_id = "stage"                                        # ${CLUSTER_ID}

   [fdb]
   clusterFile = '/opt/3fs/etc/fdb.cluster'                    # ${INSTALL_PREFIX}/etc/fdb.cluster

   [mgmtd_client]
   mgmtd_server_addresses = ["RDMA://192.0.2.10:8000"]         # RDMA://${META_IP}:8000
   ```

   也可在 meta 上改好后统一分发：
   `rsync -avz meta:${INSTALL_PREFIX}/etc/admin_cli.toml ${INSTALL_PREFIX}/etc/admin_cli.toml`。

3. 验证：

   ```shell
   ${INSTALL_PREFIX}/bin/admin_cli -cfg ${INSTALL_PREFIX}/etc/admin_cli.toml help
   ```

### 5.5 部署 Mgmtd Service（meta 节点）

1. 获取文件：

   ```shell
   mkdir -p /var/log/3fs
   mkdir -p ${INSTALL_PREFIX}/{bin,etc}
   rsync -avz meta:${SRC_ROOT}/3fs/build/bin/mgmtd_main ${INSTALL_PREFIX}/bin
   rsync -avz meta:${SRC_ROOT}/3fs/configs/{mgmtd_main.toml,mgmtd_main_launcher.toml,mgmtd_main_app.toml} ${INSTALL_PREFIX}/etc
   rsync -avz meta:${SRC_ROOT}/3fs/deploy/systemd/mgmtd_main.service /usr/lib/systemd/system
   ```

2. 修改 `mgmtd_main_app.toml`：

   ```toml
   allow_empty_node_id = true
   node_id = 1                    # mgmtd 固定为 1（示例编号方案）
   ```

3. 修改 `mgmtd_main_launcher.toml`：

   ```toml
   cluster_id = "stage"                                   # ${CLUSTER_ID}

   [fdb]
   clusterFile = '/opt/3fs/etc/fdb.cluster'               # ${INSTALL_PREFIX}/etc/fdb.cluster
   ```

4. 修改 `mgmtd_main.toml`（文件中如有多个 `[server.base.groups.listener]`，filter_list 均填本节点 RDMA 网卡名）：

   ```toml
   [server.base.groups.listener]
   filter_list = ['enp43s0f0np0']    # ${RDMA_IFACE}
   listen_port = 8000
   ...
   [server.base.groups.listener]
   filter_list = ['enp43s0f0np0']    # ${RDMA_IFACE}
   listen_port = 9000
   ...
   [common.monitor.reporters.monitor_collector]
   remote_ip = "192.0.2.10:10000"    # ${META_IP}:10000，monitor_collector 节点 RDMA 网 IP 及端口
   ```

5. 初始化集群（chunk size 1MB = 1048576，stripe 16，根目录布局引用 chain table 1）：

   ```shell
   ${INSTALL_PREFIX}/bin/admin_cli -cfg ${INSTALL_PREFIX}/etc/admin_cli.toml \
     "init-cluster --mgmtd ${INSTALL_PREFIX}/etc/mgmtd_main.toml 1 1048576 16"
   ```

   回显：

   ```txt
   Init filesystem, root directory layout: chain table ChainTableId(1), chunksize 1048576, stripesize 16
   Init config for MGMTD version 1
   ```

6. 启动并检查：

   ```shell
   systemctl daemon-reload
   systemctl start mgmtd_main
   systemctl status mgmtd_main
   ss -tuln    # 应看到 ${META_IP}:8000 与 :9000（或 0.0.0.0:8000/9000）监听
   ${INSTALL_PREFIX}/bin/admin_cli -cfg ${INSTALL_PREFIX}/etc/admin_cli.toml "list-nodes"
   ```

   回显（ReleaseVersion 随所用 commit 变化）：

   ```txt
   Id     Type     Status               Hostname  Pid      Tags  LastHeartbeatTime    ConfigVersion  ReleaseVersion
   1      MGMTD    PRIMARY_MGMTD        meta      2281735  []    N/A                  1(UPTODATE)    250228-dev-1-999999-49794159
   ```

### 5.6 部署 Meta Service（meta 节点）

1. 获取文件：

   ```shell
   mkdir -p /var/log/3fs
   mkdir -p ${INSTALL_PREFIX}/{bin,etc}
   rsync -avz meta:${SRC_ROOT}/3fs/build/bin/meta_main ${INSTALL_PREFIX}/bin
   rsync -avz meta:${SRC_ROOT}/3fs/configs/{meta_main_launcher.toml,meta_main.toml,meta_main_app.toml} ${INSTALL_PREFIX}/etc
   rsync -avz meta:${SRC_ROOT}/3fs/deploy/systemd/meta_main.service /usr/lib/systemd/system
   ```

2. 修改 `meta_main_app.toml`：

   ```toml
   allow_empty_node_id = true
   node_id = 100                  # meta 节点（示例编号方案）
   ```

3. 修改 `meta_main_launcher.toml`：

   ```toml
   cluster_id = "stage"                                       # ${CLUSTER_ID}

   [mgmtd_client]
   mgmtd_server_addresses = ["RDMA://192.0.2.10:8000"]        # RDMA://${META_IP}:8000
   ```

4. 修改 `meta_main.toml`：

   ```toml
   [server.mgmtd_client]
   mgmtd_server_addresses = ["RDMA://192.0.2.10:8000"]        # RDMA://${META_IP}:8000

   [common.monitor.reporters.monitor_collector]
   remote_ip = "192.0.2.10:10000"                             # ${META_IP}:10000

   [server.fdb]
   clusterFile = '/opt/3fs/etc/fdb.cluster'                   # ${INSTALL_PREFIX}/etc/fdb.cluster

   [server.base.groups.listener]
   filter_list = ['enp43s0f0np0']    # ${RDMA_IFACE}
   listen_port = 8001
   ...
   [server.base.groups.listener]
   filter_list = ['enp43s0f0np0']    # ${RDMA_IFACE}
   listen_port = 9001
   ```

5. 向 Mgmtd 更新 Meta 节点配置并启动：

   ```shell
   ${INSTALL_PREFIX}/bin/admin_cli -cfg ${INSTALL_PREFIX}/etc/admin_cli.toml \
     "set-config --type META --file ${INSTALL_PREFIX}/etc/meta_main.toml"
   systemctl daemon-reload
   systemctl start meta_main
   systemctl status meta_main
   ${INSTALL_PREFIX}/bin/admin_cli -cfg ${INSTALL_PREFIX}/etc/admin_cli.toml "list-nodes"
   ```

   `list-nodes` 中应出现 `100  META  HEARTBEAT_CONNECTED`。

### 5.7 部署 Storage Service（各 storage 节点）

**SSD 盘准备**

1. 查看可用硬盘，**先确认磁盘名，避免格式化错误磁盘**：

   ```shell
   lsblk
   ```

   > **注记（实录坑位）**：tester 集群 storage2 的 `/dev/nvme3n1` 是 7T 盘且包含 LVM（系统盘卷组），
   > 不参与 3FS；该节点实际使用 nvme0n1、nvme1n1、nvme2n1、**nvme4n1**。
   > stage 集群 4 块 NVMe 为 nvme0n1~nvme3n1 连续编号（每块 2.9T）。盘位以各节点 `lsblk` 实况为准。

2. 创建目录、格式化并挂载（示例为 4 块盘，按实际盘位调整；`-L` 打标签后按标签挂载，
   `noatime,nodiratime` 必带）：

   ```shell
   mkdir -p /storage/data{0..3}
   mkdir -p /var/log/3fs
   mkfs.xfs -f -L data0 /dev/nvme0n1
   mkfs.xfs -f -L data1 /dev/nvme1n1
   mkfs.xfs -f -L data2 /dev/nvme2n1
   mkfs.xfs -f -L data3 /dev/nvme3n1        # storage2 示例中此盘为 LVM 盘，应改用 /dev/nvme4n1
   mount -o noatime,nodiratime -L data0 /storage/data0
   mount -o noatime,nodiratime -L data1 /storage/data1
   mount -o noatime,nodiratime -L data2 /storage/data2
   mount -o noatime,nodiratime -L data3 /storage/data3
   mkdir -p /storage/data{0..3}/3fs
   lsblk        # 核对挂载结果
   ```

   盘名连续时可用循环写法：

   ```shell
   for i in {0..3}; do
     mkfs.xfs -L data${i} /dev/nvme${i}n1
     mount -o noatime,nodiratime -L data${i} /storage/data${i}
   done
   ```

3. 增大 aio 请求上限（如需重启后保持，另行写入 sysctl 配置）：

   ```shell
   sysctl -w fs.aio-max-nr=67108864
   ```

**启动 Storage Service**

1. 获取文件（在各 storage 节点执行）：

   ```shell
   mkdir -p ${INSTALL_PREFIX}/{bin,etc}
   mkdir -p /var/log/3fs
   rsync -avz meta:${SRC_ROOT}/3fs/build/bin/storage_main ${INSTALL_PREFIX}/bin
   rsync -avz meta:${SRC_ROOT}/3fs/configs/{storage_main_launcher.toml,storage_main.toml,storage_main_app.toml} ${INSTALL_PREFIX}/etc
   rsync -avz meta:${SRC_ROOT}/3fs/deploy/systemd/storage_main.service /usr/lib/systemd/system
   rsync -avz meta:/usr/lib64/libfdb_c.so /usr/lib64
   ```

2. 修改 `storage_main_app.toml`（**各 storage 节点 node_id 不同**）：

   ```toml
   allow_empty_node_id = true
   node_id = 10001        # storage1 为 10001、storage2 为 10002（示例编号方案）
   ```

3. 修改 `storage_main_launcher.toml`：

   ```toml
   cluster_id = "stage"                                       # ${CLUSTER_ID}

   [mgmtd_client]
   mgmtd_server_addresses = ["RDMA://192.0.2.10:8000"]        # RDMA://${META_IP}:8000
   ```

4. 修改 `storage_main.toml`（多个 listener 段的 filter_list 均填本节点 RDMA 网卡名）：

   ```toml
   [server.base.groups.listener]
   filter_list = ['enp43s0f0np0']    # ${RDMA_IFACE}
   listen_port = 8000
   ...
   [server.base.groups.listener]
   filter_list = ['enp43s0f0np0']    # ${RDMA_IFACE}
   listen_port = 9000
   ...
   [server.mgmtd]
   mgmtd_server_addresses = ["RDMA://192.0.2.10:8000"]        # RDMA://${META_IP}:8000
   ...
   [common.monitor.reporters.monitor_collector]
   remote_ip = "192.0.2.10:10000"                             # ${META_IP}:10000
   ...
   [server.targets]
   target_paths = ["/storage/data0/3fs","/storage/data1/3fs","/storage/data2/3fs","/storage/data3/3fs"]
   ```

5. 向 Mgmtd 更新 Storage 配置。`set-config` 用的配置文件先放到 meta 节点
   `${INSTALL_PREFIX}/etc/storage_main.toml`（内容须与 storage 节点一致）：

   ```shell
   # meta 节点执行：从 storage1 拉取（需 storage1 → meta 方向可 rsync/SSH）
   rsync -avz storage1:${INSTALL_PREFIX}/etc/storage_main.toml ${INSTALL_PREFIX}/etc/storage_main.toml
   ${INSTALL_PREFIX}/bin/admin_cli -cfg ${INSTALL_PREFIX}/etc/admin_cli.toml \
     "set-config --type STORAGE --file ${INSTALL_PREFIX}/etc/storage_main.toml"
   ```

   若未配置 storage1 到 meta 的免密 SSH，也可直接在 meta 上从
   `${SRC_ROOT}/3fs/configs/storage_main.toml` 复制生成，并按上文要求改成同内容后再执行 set-config。

6. 启动并检查（各 storage 节点）：

   ```shell
   systemctl daemon-reload
   systemctl start storage_main
   systemctl status storage_main
   ```

7. meta 节点确认节点状态：

   ```shell
   ${INSTALL_PREFIX}/bin/admin_cli -cfg ${INSTALL_PREFIX}/etc/admin_cli.toml "list-nodes"
   ```

   回显（storage1/storage2 均为 HEARTBEAT_CONNECTED）：

   ```txt
   Id     Type     Status               Hostname  Pid      Tags  LastHeartbeatTime    ConfigVersion  ReleaseVersion
   1      MGMTD    PRIMARY_MGMTD        meta      2281735  []    N/A                  1(UPTODATE)    250228-dev-1-999999-49794159
   100    META     HEARTBEAT_CONNECTED  meta      2281842  []    2025-03-12 17:01:32  1(UPTODATE)    250228-dev-1-999999-49794159
   10001  STORAGE  HEARTBEAT_CONNECTED  storage1  3294593  []    2025-03-12 17:38:13  1(UPTODATE)    250228-dev-1-999999-49794159
   10002  STORAGE  HEARTBEAT_CONNECTED  storage2  476286   []    2025-03-12 17:38:12  1(UPTODATE)    250228-dev-1-999999-49794159
   ```

### 5.8 创建 admin user、storage targets 和 chain table（meta 节点）

1. 创建 admin user：

   ```shell
   ${INSTALL_PREFIX}/bin/admin_cli -cfg ${INSTALL_PREFIX}/etc/admin_cli.toml "user-add --root --admin 0 root"
   ```

   回显包含运行时生成的 Token（**每次生成不同，必须使用实际输出值，不能照抄任何示例**）：

   ```txt
   Uid                0
   Name               root
   Token              <运行时生成的 token>
   IsRootUser         true
   IsAdmin            true
   Gid                0
   ```

   将 token 写入文件并收紧权限：

   ```shell
   vim ${INSTALL_PREFIX}/etc/token.txt        # 粘贴实际 token
   chmod 600 ${INSTALL_PREFIX}/etc/token.txt
   ```

2. 安装 data placement Python 依赖：

   ```shell
   pip3 install -r ${SRC_ROOT}/3fs/deploy/data_placement/requirements.txt
   ```

3. 生成 data placement 模型（工作目录任意，输出产生于当前目录 `output/` 下；
   示例以 2 storage 节点、2 副本为例）：

   ```shell
   mkdir -p /opt/src/chaintable && cd /opt/src/chaintable
   python3 ${SRC_ROOT}/3fs/deploy/data_placement/src/model/data_placement.py \
     -ql -relax -type CR --num_nodes 2 --replication_factor 2 --min_targets_per_disk 6
   ls -d output/DataPlacementModel-v_*        # 查看实际生成目录名
   ```

   参数：`--num_nodes` 存储节点数；`--replication_factor` 副本因子；`--min_targets_per_disk` 每盘最少 target 数。

4. 计算 chain（`DataPlacementModel-v_...` 替换为上一步实际目录名）：

   ```shell
   python3 ${SRC_ROOT}/3fs/deploy/data_placement/src/setup/gen_chain_table.py \
     --chain_table_type CR --node_id_begin 10001 --node_id_end 10002 \
     --num_disks_per_node 4 --num_targets_per_disk 6 \
     --target_id_prefix 1 --chain_id_prefix 9 \
     --incidence_matrix_path output/DataPlacementModel-v_2-b_6-r_6-k_2-λ_6-lb_1-ub_0/incidence_matrix.pickle
   ```

   参数：`--node_id_begin/--node_id_end` storage 节点 NodeId 起止；`--num_disks_per_node` 每节点盘数；
   `--num_targets_per_disk` 每盘 target 数；`--target_id_prefix`/`--chain_id_prefix` 编号前缀
   （**示例编号方案，按集群规划替换**：前缀 1/9 生成 target 101000100101、chain 900100001；
   实录 stage 集群用前缀 3/8，生成 target 301000100101、chain 800100001）；
   `--incidence_matrix_path` 上一步生成的 pickle 路径。

   执行成功后 `output/` 下应有：

   ```txt
   generated_chains.csv
   generated_chain_table.csv
   create_target_cmd.txt
   remove_target_cmd.txt
   ```

5. 创建 storage target（批量执行命令文件；实录环境的 create_target_cmd.txt 中每条 create-target
   命令均带 `--use-new-chunk-engine` 参数以启用新 chunk engine）：

   ```shell
   ${INSTALL_PREFIX}/bin/admin_cli --cfg ${INSTALL_PREFIX}/etc/admin_cli.toml \
     --config.user_info.token $(<"${INSTALL_PREFIX}/etc/token.txt") < output/create_target_cmd.txt
   ```

6. 上传 chains 与 chain table 到 mgmtd：

   ```shell
   ${INSTALL_PREFIX}/bin/admin_cli --cfg ${INSTALL_PREFIX}/etc/admin_cli.toml \
     --config.user_info.token $(<"${INSTALL_PREFIX}/etc/token.txt") \
     "upload-chains output/generated_chains.csv"
   ${INSTALL_PREFIX}/bin/admin_cli --cfg ${INSTALL_PREFIX}/etc/admin_cli.toml \
     --config.user_info.token $(<"${INSTALL_PREFIX}/etc/token.txt") \
     "upload-chain-table --desc ${CLUSTER_ID} 1 output/generated_chain_table.csv"
   ```

   > **注记（chain table 编号）**：全新集群首张表用 table id 1（如上）。实录 stage 集群因历史演进
   > 存在多张表：table 1 为 3 副本历史表（依赖已停机的第 3 台 storage），当前活跃表为 table 2
   > （24 条 chain、2 副本，desc 为 stage-2replica，HEAD 集中在 storage2），上传命令为
   > `"upload-chain-table --desc stage-2replica 2 output/generated_chain_table.csv"`。
   > 日常读写目录需通过 mkdir 指定布局所用 chain table：
   >
   > ```shell
   > ${INSTALL_PREFIX}/bin/admin_cli -cfg ${INSTALL_PREFIX}/etc/admin_cli.toml \
   >   "mkdir --chain-table-id 2 <目录名>"
   > ```

7. 查看 chain 状态：

   ```shell
   ${INSTALL_PREFIX}/bin/admin_cli -cfg ${INSTALL_PREFIX}/etc/admin_cli.toml "list-chains"
   # 或指定表： "list-chains --table-id 2"
   ```

   回显（2 副本示例：24 条 chain，每条含 2 个 target，分别位于 node 10001 与 10002）：

   ```txt
   ChainId    ReferencedBy  ChainVersion  Status   PreferredOrder  Target                          Target
   900100001  1             26            SERVING  []              101000100101(SERVING-UPTODATE)  101000200101(SERVING-UPTODATE)
   900100002  1             26            SERVING  []              101000100102(SERVING-UPTODATE)  101000200102(SERVING-UPTODATE)
   ...
   ```

### 5.9 部署 FUSE 客户端（每台 FUSE 客户端）

> **注记**：实录 stage 集群 FUSE 客户端共 3 台（含 meta 兼任的一台），需逐台执行；
> tester 集群 FUSE 与 meta 共节点。新增客户端到既有集群请改走 `fuse-client-expansion.md`。

1. 从编译节点获取二进制、配置与库：

   ```shell
   mkdir -p /var/log/3fs
   mkdir -p ${INSTALL_PREFIX}/{bin,etc}
   mkdir -p ${MOUNT_POINT}
   rsync -avz meta:${SRC_ROOT}/3fs/build/bin/hf3fs_fuse_main ${INSTALL_PREFIX}/bin
   rsync -avz meta:${SRC_ROOT}/3fs/build/bin/admin_cli ${INSTALL_PREFIX}/bin
   rsync -avz meta:${SRC_ROOT}/3fs/configs/{hf3fs_fuse_main_launcher.toml,hf3fs_fuse_main.toml,hf3fs_fuse_main_app.toml} ${INSTALL_PREFIX}/etc
   rsync -avz meta:${SRC_ROOT}/3fs/deploy/systemd/hf3fs_fuse_main.service /usr/lib/systemd/system
   rsync -avz meta:${SRC_ROOT}/3fs/build/src/lib/api/libhf3fs_api_shared.so /usr/lib
   rsync -avz meta:${INSTALL_PREFIX}/etc/token.txt ${INSTALL_PREFIX}/etc
   rsync -avz meta:/usr/lib64/libfdb_c.so /usr/lib64
   rsync -avz meta:${SRC_ROOT}/3fs/benchmarks/fio_usrbio/hf3fs_usrbio.so /usr/lib64
   rsync -avz meta:/usr/local/bin/fio /usr/local/bin       # 仅 USRBIO fio 测试需要
   chmod 600 ${INSTALL_PREFIX}/etc/token.txt
   ldconfig
   ```

2. 修改 `hf3fs_fuse_main_launcher.toml`：

   ```toml
   cluster_id = "stage"                                       # ${CLUSTER_ID}
   mountpoint = '/3fs/stage'                                  # ${MOUNT_POINT}
   token_file = '/opt/3fs/etc/token.txt'                      # ${INSTALL_PREFIX}/etc/token.txt

   [mgmtd_client]
   mgmtd_server_addresses = ["RDMA://192.0.2.10:8000"]        # RDMA://${META_IP}:8000
   ```

3. 修改 `hf3fs_fuse_main.toml`（文件中如存在 listener 网卡配置，filter_list 填**本节点**实际 RDMA 网卡名）：

   ```toml
   [mgmtd]
   mgmtd_server_addresses = ["RDMA://192.0.2.10:8000"]        # RDMA://${META_IP}:8000

   [common.monitor.reporters.monitor_collector]
   remote_ip = "192.0.2.10:10000"                             # ${META_IP}:10000
   ```

4. 向 Mgmtd 更新 FUSE 配置（任一带 admin_cli 的节点执行一次即可）：

   ```shell
   ${INSTALL_PREFIX}/bin/admin_cli -cfg ${INSTALL_PREFIX}/etc/admin_cli.toml \
     "set-config --type FUSE --file ${INSTALL_PREFIX}/etc/hf3fs_fuse_main.toml"
   ```

5. **修改 systemd 单元，显式指定 --cfg 参数**（关键坑位）：

   ```shell
   vim /usr/lib/systemd/system/hf3fs_fuse_main.service
   ```

   将 ExecStart 改为：

   ```ini
   ExecStart=/opt/3fs/bin/hf3fs_fuse_main --cfg /opt/3fs/etc/hf3fs_fuse_main.toml --launcher_cfg /opt/3fs/etc/hf3fs_fuse_main_launcher.toml
   ```

   （即 `${INSTALL_PREFIX}/bin/hf3fs_fuse_main --cfg ${INSTALL_PREFIX}/etc/hf3fs_fuse_main.toml
   --launcher_cfg ${INSTALL_PREFIX}/etc/hf3fs_fuse_main_launcher.toml`）

   ```shell
   systemctl daemon-reload
   ```

   > **注记（实录坑位）**：若不显式指定 --cfg，FUSE 客户端将不加载 mgmtd 下发的配置。
   > stage 集群曾有两台客户端因此写性能只有正常节点的约 **1/4**，且服务状态看起来完全正常。

6. 启动并验证：

   ```shell
   systemctl start hf3fs_fuse_main
   systemctl status hf3fs_fuse_main
   df -h            # 应看到 hf3fs.${CLUSTER_ID} 挂载于 ${MOUNT_POINT}
   ```

   回显示例：

   ```txt
   Filesystem                         Size  Used Avail Use% Mounted on
   ...
   hf3fs.stage                         12T  1.1G   12T   1% /3fs/stage
   ```

   > **容量核算**：实录环境 2 个 storage 节点各 4 块 2.9T NVMe、2 副本，
   > 可用容量约 (2 × 4 × 2.9T) / 2 ≈ 12T。

## 6. 验收清单

| 检查项 | 命令 | 预期 |
|--------|------|------|
| 节点心跳 | `admin_cli ... "list-nodes"` | MGMTD 为 PRIMARY_MGMTD；META 与全部 STORAGE 为 HEARTBEAT_CONNECTED |
| chain 状态 | `admin_cli ... "list-chains"`（必要时 `--table-id N`） | 全部 SERVING，target 均 SERVING-UPTODATE |
| chain 状态统计 | `admin_cli ... "list-chains" \| awk '{print $4}' \| sort \| uniq -c` | 无 UNAVAILABLE/WAITING |
| FUSE 挂载 | 各客户端 `df -h \| grep ${MOUNT_POINT}` | hf3fs.${CLUSTER_ID} 已挂载，容量符合副本核算 |
| 端口监听 | meta `ss -tuln` | ${FDB_PORT}、8000、8001、9000、9001、10000、${CK_PORT} 按规划监听 |
| 监控入库 | `clickhouse-client --port ${CK_PORT} --password "${CK_PASS}" --database 3fs --query "SHOW TABLES"` | 3fs-monitor.sql 建表成功 |
| 读写冒烟 | `mkdir ${MOUNT_POINT}/<测试目录>` 后 dd/fio 小流量读写 | 无 IO 错误 |

## 7. 逐节点必改文件汇总表

| 节点 | 文件 | 必改内容 |
|------|------|---------|
| 所有节点 | `/etc/hosts` | 角色名解析到 `${RDMA_NET}` 内的 RDMA IP |
| 所有节点 | `/etc/selinux/config` | `SELINUX=permissive`（仅测试环境；生产见 §3.1 安全说明） |
| 所有节点 | `${INSTALL_PREFIX}/etc/admin_cli.toml` | `cluster_id`、`clusterFile = '${INSTALL_PREFIX}/etc/fdb.cluster'`、`mgmtd_server_addresses = ["RDMA://${META_IP}:8000"]` |
| meta | `/etc/foundationdb/foundationdb.conf` | `public-address = ${META_IP}:$ID` |
| meta | `/etc/foundationdb/fdb.cluster` | `127.0.0.1:${FDB_PORT}` 改为 `${META_IP}:${FDB_PORT}` |
| meta | `/etc/clickhouse-server/config.xml` | `<tcp_port>${CK_PORT}</tcp_port>` |
| meta | `${INSTALL_PREFIX}/etc/monitor_collector_main.toml` | `filter_list`（本节点 RDMA 网卡名）、ClickHouse 端口与密码（IB 模式另有 `custom_tcp_nic_prefix = "ibp"`） |
| meta | `${INSTALL_PREFIX}/etc/mgmtd_main_app.toml` | `allow_empty_node_id = true`、`node_id = 1` |
| meta | `${INSTALL_PREFIX}/etc/mgmtd_main_launcher.toml` | `cluster_id`、FDB clusterFile |
| meta | `${INSTALL_PREFIX}/etc/mgmtd_main.toml` | `filter_list`、listener 8000/9000、monitor_collector `${META_IP}:10000` |
| meta | `${INSTALL_PREFIX}/etc/meta_main_app.toml` | `allow_empty_node_id = true`、`node_id = 100` |
| meta | `${INSTALL_PREFIX}/etc/meta_main_launcher.toml` | `cluster_id`、mgmtd `RDMA://${META_IP}:8000` |
| meta | `${INSTALL_PREFIX}/etc/meta_main.toml` | mgmtd、monitor_collector、FDB clusterFile、listener 8001/9001 与 `filter_list` |
| meta | `${INSTALL_PREFIX}/etc/token.txt` | 写入 `user-add` 实际输出的 token，`chmod 600` |
| storageN | `${INSTALL_PREFIX}/etc/storage_main_app.toml` | `allow_empty_node_id = true`、`node_id = 10001/10002/…`（逐节点不同） |
| storageN | `${INSTALL_PREFIX}/etc/storage_main_launcher.toml` | `cluster_id`、mgmtd `RDMA://${META_IP}:8000` |
| storageN | `${INSTALL_PREFIX}/etc/storage_main.toml` | `filter_list`、listener 8000/9000、mgmtd、monitor_collector、`target_paths`（4 个数据盘路径） |
| storageN | sysctl | `fs.aio-max-nr=67108864` |
| FUSE 客户端 | `${INSTALL_PREFIX}/etc/hf3fs_fuse_main_launcher.toml` | `cluster_id`、`mountpoint`、`token_file`、mgmtd RDMA 地址 |
| FUSE 客户端 | `${INSTALL_PREFIX}/etc/hf3fs_fuse_main.toml` | mgmtd、monitor_collector、可能存在的 listener `filter_list`（本节点网卡名） |
| FUSE 客户端 | `/usr/lib/systemd/system/hf3fs_fuse_main.service` | ExecStart 显式加 `--cfg` 与 `--launcher_cfg`（§5.9 步骤 5） |

## 8. 环境差异与坑位注记汇总

1. **操作系统 SP 混部**：实录集群为 openEuler 24.03 SP2/SP3/SP4 混合部署，统一按 SP3 安装即可（§1.2）。
2. **RDMA 网卡名异构**：IB 模式设备名形如 ibp129s0f0（此时 monitor_collector 需配
   `custom_tcp_nic_prefix = "ibp"`），以太/RoCE 模式形如 enp43s0f0np0，扩容客户端甚至可能是 eth5。
   所有 toml 的 `filter_list` 必须填**本节点实际网卡名**，逐节点用 `rdma link`/`ibdev2netdev` 确认。
3. **storage2 避开 LVM 盘**：格式化前必须 `lsblk` 核对，tester 集群 storage2 的 nvme3n1 为 7T LVM 盘，
   误格式化将破坏系统卷组（§5.7）。
4. **FUSE systemd 缺 --cfg**：写性能降为约 1/4 且状态"正常"，极易漏检（§5.9）。
5. **历史节点与多 chain table**：stage 集群存在已停机的第 3 台 storage 与历史 3 副本 chain table 1；
   活跃表为 table 2，读写目录用 `mkdir --chain-table-id 2` 指定布局（§5.8）。chain table 切换技巧与
   HEAD 分散实测结论见 `cluster-operations.md` §5。
6. **token 为运行时生成**：`user-add` 输出的 token 每台集群不同，必须用实际值写入 token.txt，
   不得照抄文档示例或外传。
