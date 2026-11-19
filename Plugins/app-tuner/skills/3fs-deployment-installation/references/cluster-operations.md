# 3FS 集群运维命令参考

> **何时读我**：对已部署的 3FS 集群做日常巡检、配置变更、chain/target 管理、chain table 切换、日志与 coredump 排查时。本文是**命令参考文档**（按主题组织，按需取用），不是顺序执行的手册体脚本。部署流程见 `installation-guide.md`，二进制替换见 `binary-replacement.md`。
>
> 命令引用集群变量表（见 `installation-guide.md` 开头）。除特别说明外在 meta 节点执行。

## 0. 约定

```shell
# admin_cli 简写（本文后续用 $ADMIN_CLI 指代）
ADMIN_CLI="${INSTALL_PREFIX}/bin/admin_cli -cfg ${INSTALL_PREFIX}/etc/admin_cli.toml"

# 需要 token 的管理操作（create-target/upload-chains 等）追加：
#   --config.user_info.token $(<"${INSTALL_PREFIX}/etc/token.txt")
```

远程节点操作凭据（安全基线，见 SKILL.md）：**优先 SSH key 免密**；必须用密码时先
`read -s -p 'Password: ' SSHPASS; export SSHPASS`，再统一走 `sshpass -e`，
严禁用 sshpass 的 `-p` 选项携带明文密码：

```shell
SSH_OPTS='-o StrictHostKeyChecking=no'
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@${STORAGE1_IP} "systemctl status storage_main | head -10"
# SSH key 免密时去掉 sshpass -e 前缀即可，下同
```

## 1. 集群状态查看

```shell
# 所有节点状态（心跳、配置版本、Release 版本）
$ADMIN_CLI "list-nodes"

# 所有 chain 状态（副本状态、角色分配）
$ADMIN_CLI "list-chains"
$ADMIN_CLI "list-chains --table-id 2"                 # 指定 chain table

# chain 状态统计（按状态分组计数；输出第 4 列为 Status）
$ADMIN_CLI "list-chains" | awk '{print $4}' | sort | uniq -c

# 查看所有 targets / 指定 chain 的 target 详情
$ADMIN_CLI "list-targets"
$ADMIN_CLI "list-targets --chain-id 900100001"        # 示例编号方案，按集群规划替换

# 所有 chain table
$ADMIN_CLI "list-chain-tables"

# admin_cli 全部可用命令
$ADMIN_CLI help
```

## 2. 服务启停顺序

**停止顺序（从客户端到服务端）**：FUSE → storage → meta → mgmtd → monitor_collector。
**启动顺序（反序）**：monitor_collector → mgmtd → meta → storage → FUSE。

```shell
# meta 节点本地服务（mgmtd/meta/monitor_collector）
systemctl start|stop|restart|status mgmtd_main
systemctl start|stop|restart|status meta_main
systemctl start|stop|restart|status monitor_collector_main

# FoundationDB / ClickHouse（二进制替换等场景默认不动它们）
systemctl start|stop|restart|status foundationdb
clickhouse start
clickhouse stop

# 修改 systemd 单元后必须重载
systemctl daemon-reload

# 远程 storage / FUSE 节点
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@${STORAGE1_IP} "systemctl restart storage_main"
for ip in ${STORAGE1_IP} ${STORAGE2_IP}; do
  sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@$ip "systemctl restart storage_main"
done
```

> **注记**：实录重编译 SOP 的启动顺序为 monitor_collector → mgmtd → meta → storage → FUSE；
> folly 重编译流程中 monitor_collector 在 mgmtd/meta 之后启动（mgmtd → meta → monitor_collector →
> storage → FUSE），两种顺序均验证可用；monitor_collector 仅收监控数据，位置相对宽松，
> 但 mgmtd → meta → storage → FUSE 的相对顺序必须保持。
> FUSE 服务停止后挂载点可能残留，需 `fusermount3 -u ${MOUNT_POINT}`（或 `umount -l`）释放，
> 见 `scripts/replace_3fs_bin.sh` 的 release_mount。

## 3. 配置管理范式（get-config 实际导出为准）

**铁律（实录经验）**：

1. 任何 storage / FUSE 节点的生效配置，**一律以在对应节点上实际导出（get-config）为准**；
   直接看本地缓存文件可能并不准确。
2. 更改了某类节点的参数后，除 `set-config` 上传 mgmtd 外，还要**同步修改该类每个节点
   `${INSTALL_PREFIX}/etc/` 下的对应文件**——该目录下展示的配置必须与运行实例一致。

标准范式：**get-config 导出 → 修改 → set-config 上传 → get-config 回读确认**。

```shell
# 导出当前生效配置（推荐方式，--node-type + --output-file）
$ADMIN_CLI "get-config --node-type STORAGE --output-file /tmp/storage.current.toml"
$ADMIN_CLI "get-config --node-type FUSE --output-file /tmp/fuse.current.toml"
vim /tmp/storage.current.toml          # 修改

# 上传到 mgmtd（--type 取 META / STORAGE / FUSE / MGMTD）
$ADMIN_CLI "set-config --type STORAGE --file /tmp/storage.current.toml"
$ADMIN_CLI "set-config --type STORAGE --file ${INSTALL_PREFIX}/etc/storage_main.toml"
$ADMIN_CLI "set-config --type META --file ${INSTALL_PREFIX}/etc/meta_main.toml"
$ADMIN_CLI "set-config --type FUSE --file ${INSTALL_PREFIX}/etc/hf3fs_fuse_main.toml"

# 查看当前配置（stdout）
$ADMIN_CLI "get-config --type STORAGE"
$ADMIN_CLI "get-config --type META"

# 回读确认修改已生效
$ADMIN_CLI "get-config --node-type STORAGE --output-file /tmp/storage.verify.toml"
```

远程查看/修改 storage 节点本地配置文件：

```shell
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@${STORAGE1_IP} "cat ${INSTALL_PREFIX}/etc/storage_main.toml"

# 修改 filter_list（网卡过滤；网卡名以该节点实际为准）
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@${STORAGE1_IP} \
  "sed -i \"s/filter_list = \[\]/filter_list = ['${RDMA_IFACE}']/\" ${INSTALL_PREFIX}/etc/storage_main.toml"

# 修改 target_paths
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@${STORAGE1_IP} \
  "sed -i 's|target_paths = \[.*\]|target_paths = [\"/storage/data0/3fs\",\"/storage/data1/3fs\",\"/storage/data2/3fs\",\"/storage/data3/3fs\"]|' ${INSTALL_PREFIX}/etc/storage_main.toml"
```

## 4. Chain / Target 管理

以下 chain_id/target_id/node_id 均为**示例编号方案**（chain 前缀 9、target 前缀 1），按集群规划替换。
写操作需带 token（见 §0）。

```shell
# 创建单个 target
$ADMIN_CLI "create-target --node-id 10001 --disk-index 0 --target-id 101000100101 --chain-id 900100001 --use-new-chunk-engine"

# 批量创建（从 gen_chain_table.py 生成的命令文件读取，见 installation-guide.md §5.8）
grep "node-id 10001" <工作目录>/output/create_target_cmd.txt | $ADMIN_CLI
$ADMIN_CLI --config.user_info.token $(<"${INSTALL_PREFIX}/etc/token.txt") < <工作目录>/output/create_target_cmd.txt

# 在指定节点所有磁盘上创建 targets
$ADMIN_CLI "create-targets --node-id 10001 --allow-existing-target --use-new-chunk-engine"

# 强制下线 / 删除 target
$ADMIN_CLI "offline-target --node-id 10001 --target-id 101000100101 --force"
$ADMIN_CLI "remove-target --node-id 10001 --target-id 101000100101 --force"

# 上传 chains / chain table 到 mgmtd
$ADMIN_CLI "upload-chains <工作目录>/output/generated_chains.csv"
$ADMIN_CLI "upload-chain-table --desc ${CLUSTER_ID} 1 <工作目录>/output/generated_chain_table.csv"

# 更新 chain 中的 target（add/remove 模式）
$ADMIN_CLI "update-chain --mode add 900100001 101000100101"
$ADMIN_CLI "update-chain --mode remove 900100001 101000100101"

# 设置 chain 的 preferred target order（实录中另见空格分隔写法 "<chainId> <target1> <target2>"）
$ADMIN_CLI "set-preferred-target-order 900100001 101000100101,101000200101,101000300101"

# 按 preferred order 轮转 / 轮转末位 target（chain 恢复用，见 binary-replacement.md §9）
$ADMIN_CLI "rotate-as-preferred-order 900100001"
$ADMIN_CLI "rotate-lastsrv 900100001"
```

## 5. Chain table 管理与切换技巧

实录 stage 集群共 3 张 chain table：

| ChainTableId | 版本 | Chain 数 | 副本数 | 描述 | 状态 |
|---|---|---|---|---|---|
| 1 | 1 | 24 | 3 | 原 3 storage 布局 | 可用（需启动已停机的第 3 台 storage） |
| 2 | 26 | 24 | 2 | 2 副本，HEAD 集中在 storage2 | **活跃** |
| 3 | 34 | 24 | 2 | HEAD 分散到两台 storage | 不可用（双向转发问题） |

**切换技巧**：

- 不同 chain table 对应不同目录布局，用 `mkdir --chain-table-id N` 创建各自的测试目录：

  ```shell
  $ADMIN_CLI "mkdir --chain-table-id 2 ${MOUNT_POINT}/fio_2replica"
  ```

- 切换 chain table **无需重启任何服务**，只需把业务/测试路径换到对应目录
  （实录测试脚本通过 `FIO_DIR` 环境变量切换）。

**HEAD 分散实测结论（反例，勿重蹈）**：曾尝试 chain table 3 把 HEAD 均匀分散到两台 storage
（预期两张网卡各承担一半流量），通过 `set-preferred-target-order` + `rotate-as-preferred-order`
设置 HEAD 分布。结果带宽仅 **41 MiB/s**：两个 storage 节点之间的双向 RDMA 转发不稳定，
产生大量 forward timeout。**结论：2 storage 节点环境下 HEAD 分散不可行**，保持 HEAD 集中在
单节点（table 2）。参数级调优另见 skill `3fs-parameter-tuning`。

## 6. 用户与 token 管理

```shell
# 创建 root admin 用户（输出含运行时 token）
$ADMIN_CLI "user-add --root --admin 0 root"

# token 文件（含凭据：权限必须 600，内容不得粘贴到文档/日志/工单）
ls -l ${INSTALL_PREFIX}/etc/token.txt
```

## 7. data_placement / gen_chain_table 流程

完整参数说明见 `installation-guide.md` §5.8。3 节点 3 副本示例（对比 2 节点 2 副本）：

```shell
cd <工作目录>
python3 ${SRC_ROOT}/3fs/deploy/data_placement/src/model/data_placement.py \
  -ql -relax -type CR --num_nodes 3 --replication_factor 3 --min_targets_per_disk 6

python3 ${SRC_ROOT}/3fs/deploy/data_placement/src/setup/gen_chain_table.py \
  --chain_table_type CR --node_id_begin 10001 --node_id_end 10003 \
  --num_disks_per_node 4 --num_targets_per_disk 6 \
  --target_id_prefix 1 --chain_id_prefix 9 \
  --incidence_matrix_path output/DataPlacementModel-v_3-b_6-r_6-k_3-λ_3-lb_3-ub_3/incidence_matrix.pickle

ls -lh output/
```

## 8. 网络与端口诊断

**端口规划表**：

| 端口 | 用途 |
|------|------|
| 4500（`${FDB_PORT}`） | FoundationDB |
| 8000 | MGMTD Service / Storage Service（主服务） |
| 8001 | META Service（主服务） |
| 9000 | Core Service（MGMTD/Storage） |
| 9001 | Core Service（META） |
| 9123（`${CK_PORT}`） | ClickHouse TCP |
| 10000 | MonitorCollector |

```shell
# 本机监听端口（对照上表）
ss -tuln | grep -E "4500|8000|9000|10000"

# 网络接口与 IP
ip link show
ip addr show ${RDMA_IFACE}

# TCP 端口连通性
timeout 3 bash -c "echo > /dev/tcp/${STORAGE1_IP}/8000" 2>&1 && echo "Connected" || echo "Failed"

# 远程节点监听端口
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@${STORAGE2_IP} "ss -tuln | grep -E '8000|9000'"

# RDMA 设备状态
ibv_devices
ibv_devinfo -d mlx5_0
rdma link
ibdev2netdev        # IB 设备名 ↔ 网卡名映射（filter_list 依据）
ip addr show | grep -E "ibp|inet.*192\."    # RDMA 接口 IP（网段按实际替换）
```

## 9. 日志与 coredump 排查入口

```shell
# 服务日志（meta 节点本地；storage 节点同构，路径 /var/log/3fs/storage_main*.log）
tail -100 /var/log/3fs/mgmtd_main.log                 # mgmtd 主日志
tail -100 /var/log/3fs/mgmtd_main-err.log             # mgmtd 错误日志
tail -100 /var/log/3fs/meta_main.log
tail -100 /var/log/3fs/meta_main-err.log
tail -100 /var/log/3fs/monitor_collector_main.log
tail -100 /var/log/3fs/monitor_collector_main-err.log

# 远程 storage 节点日志
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@${STORAGE1_IP} "tail -100 /var/log/3fs/storage_main.log"
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@${STORAGE1_IP} "tail -100 /var/log/3fs/storage_main-err.log"

# journalctl 按时间范围过滤错误
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@${STORAGE1_IP} \
  "journalctl -u storage_main --since '2026-06-06 13:00:00' | strings | grep -iE 'error|fail' | head -30"

# core dump
ls -lh /var/lib/systemd/coredump/

# gdb 分析（示例：以 mgmtd 的启动参数复跑）
gdb --batch -ex "run" -ex "bt" --args ${INSTALL_PREFIX}/bin/mgmtd_main \
    --launcher_cfg ${INSTALL_PREFIX}/etc/mgmtd_main_launcher.toml \
    --app-cfg ${INSTALL_PREFIX}/etc/mgmtd_main_app.toml
```

## 10. 磁盘与存储诊断

```shell
lsblk                                  # 分区与挂载
df -h                                  # 磁盘使用
blkid /dev/nvme0n1                     # 磁盘 UUID/标签
iostat -x /dev/nvme0n1 1 3             # 磁盘 I/O

# 远程查看 storage 节点 target 目录结构与 target 元数据
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@${STORAGE1_IP} "ls -la /storage/data0/3fs/"
sshpass -e ssh ${SSH_OPTS} ${SSH_USER}@${STORAGE1_IP} "cat /storage/data0/3fs/101000100101/target.toml"
```

## 11. ClickHouse 操作

```shell
# 密码交互注入，不出现在命令行明文
read -s -p 'ClickHouse password: ' CK_PASS; export CK_PASS; printf '\n'

clickhouse-client --port ${CK_PORT} --password "${CK_PASS}"
clickhouse-client --port ${CK_PORT} --password "${CK_PASS}" --query "SHOW DATABASES"
clickhouse-client --port ${CK_PORT} --password "${CK_PASS}" --database 3fs --query "SHOW TABLES"

# 重新初始化监控表
clickhouse-client --port ${CK_PORT} --password "${CK_PASS}" -n < ${SRC_ROOT}/3fs/deploy/sql/3fs-monitor.sql
```

## 12. FoundationDB 操作

```shell
cat /etc/foundationdb/foundationdb.conf
cat /etc/foundationdb/fdb.cluster
fdbcli --exec status
```

## 13. 编译与构建速查

```shell
source <毕昇环境脚本>                                        # 加载毕昇编译器环境变量
cd ${SRC_ROOT}/3fs && cmake --build build --target mgmtd_main -j$(nproc)   # 单目标
cd ${SRC_ROOT}/3fs && cmake --build build -j$(nproc)                       # 全部目标
ls -lh ${BUILD_DIR}/bin/                                                   # 产物
```

依赖安装、补丁、三处必需 CMake 修改等细节见 `build-notes.md`。

## 14. 关键配置文件路径

meta 节点：

```txt
${INSTALL_PREFIX}/etc/mgmtd_main.toml              - mgmtd 主配置
${INSTALL_PREFIX}/etc/mgmtd_main_launcher.toml     - mgmtd 启动配置
${INSTALL_PREFIX}/etc/mgmtd_main_app.toml          - mgmtd 应用配置（node_id）
${INSTALL_PREFIX}/etc/meta_main.toml               - meta 主配置
${INSTALL_PREFIX}/etc/meta_main_launcher.toml      - meta 启动配置
${INSTALL_PREFIX}/etc/meta_main_app.toml           - meta 应用配置（node_id）
${INSTALL_PREFIX}/etc/monitor_collector_main.toml    - 监控采集配置（含 ClickHouse 凭据，注意权限）
${INSTALL_PREFIX}/etc/admin_cli.toml               - admin_cli 配置
${INSTALL_PREFIX}/etc/fdb.cluster                  - FDB 集群文件
${INSTALL_PREFIX}/etc/token.txt                    - admin 用户 token（chmod 600）
```

storage 节点（每台）：

```txt
${INSTALL_PREFIX}/etc/storage_main.toml            - storage 主配置（target_paths/filter_list）
${INSTALL_PREFIX}/etc/storage_main_launcher.toml   - storage 启动配置
${INSTALL_PREFIX}/etc/storage_main_app.toml        - storage 应用配置（node_id）
```

FUSE 客户端（每台）：

```txt
${INSTALL_PREFIX}/etc/hf3fs_fuse_main.toml             - FUSE 主配置
${INSTALL_PREFIX}/etc/hf3fs_fuse_main_launcher.toml    - FUSE 启动配置（mountpoint/token_file）
${INSTALL_PREFIX}/etc/hf3fs_fuse_main_app.toml         - FUSE 应用配置
/usr/lib/systemd/system/hf3fs_fuse_main.service        - systemd 单元（ExecStart 必须显式 --cfg）
```
