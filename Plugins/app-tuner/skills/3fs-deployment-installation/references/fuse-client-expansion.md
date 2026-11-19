# FUSE 客户端扩容 SOP

> **何时读我**：向**既有** 3FS 集群新增一台 FUSE 客户端时（例如从 1 client 扩到 2 client）。全新集群的 FUSE 部署见 `installation-guide.md` §5.9。
>
> 提炼自真实扩容实录（新增节点 client2，形成 2 client + 1 meta + 2 storage）。命令引用集群变量表（见 `installation-guide.md` 开头），新节点相关变量：
>
> | 变量 | 含义 | 示例 |
> |------|------|------|
> | `${NEW_CLIENT}` | 新客户端主机名 | client2 |
> | `${NEW_CLIENT_IP}` | 新客户端 RDMA 网 IP | 192.0.2.20 |
> | `${NEW_CLIENT_IFACE}` | 新客户端 RDMA 网卡名 | eth5 |
>
> 命令默认以 `${SSH_USER}`（示例 root）执行；文件推送方向为 meta（编译节点）→ 新客户端。

## 0. 扩容前确认

- mgmtd 地址：`RDMA://${META_IP}:8000`；monitor_collector 地址：`${META_IP}:10000`；挂载点 `${MOUNT_POINT}`。
- meta 节点上已有：编译产物（hf3fs_fuse_main、admin_cli、libhf3fs_api_shared.so、hf3fs_usrbio.so、fio）、
  `${INSTALL_PREFIX}/etc/token.txt`、`${INSTALL_PREFIX}/etc/admin_cli.toml`、`${INSTALL_PREFIX}/etc/fdb.cluster`。
- **异构网卡名坑位（实录）**：新客户端的 RDMA 网卡名可能与存量节点完全不同
  （实录：存量节点为 enp43s0f0np0/mlx5_0，新客户端为 **eth5/mlx5_1**）。
  所有 toml 中的 `filter_list` 必须填**新节点本机实际网卡名**，严禁照抄存量节点配置。

## 1. 所有节点补充 hosts

在 meta、storage1、storage2、`${NEW_CLIENT}` 都编辑 `/etc/hosts`，确保包含：

```txt
192.0.2.10 meta
192.0.2.11 storage1
192.0.2.12 storage2
192.0.2.20 client2
```

（示例 IP 为文档保留网段，替换为 `${RDMA_NET}` 内实际规划；角色名必须解析到 RDMA IP。）

所有节点验证：

```shell
ping -c 3 meta
ping -c 3 storage1
ping -c 3 storage2
ping -c 3 client2
```

## 2. 新客户端 RDMA 检查

在 `${NEW_CLIENT}` 执行：

```shell
hostname                            # 应为规划的主机名（client2）
ip addr show ${NEW_CLIENT_IFACE}    # 应为 ${NEW_CLIENT_IP}/24（示例 192.0.2.20/24）
rdma link                           # 对应 RDMA 设备（示例 mlx5_1）应为 ACTIVE，netdev 为 ${NEW_CLIENT_IFACE}
ping -c 3 ${META_IP}
ping -c 3 ${STORAGE1_IP}
ping -c 3 ${STORAGE2_IP}
```

确认三项：hostname 正确；网卡 IP/掩码正确；`rdma link` 中对应设备 `state ACTIVE` 且 netdev 匹配。
网卡名不确定时用 `ibdev2netdev` 查询 IB 设备与 netdev 的映射。

## 3. 新客户端基础环境

如新节点未做过基础环境准备（详见 `installation-guide.md` §3.1）：

```shell
# 关闭防火墙（仅可信内网/测试环境；生产环境应放行端口而非关闭）
systemctl stop firewalld
systemctl disable firewalld
systemctl status firewalld

# SELinux permissive（即时生效 + 持久化）
setenforce 0
getenforce
vim /etc/selinux/config        # SELINUX=permissive
```

## 4. 新客户端时间同步

实录采用 ntpdate + crontab 方案（指向 meta 节点作内网 NTP 源；chrony 方案见
`installation-guide.md` §3.3，二选一）：

```shell
yum install -y ntp ntpdate crontabs

# 备份旧配置并新建 /etc/ntp.conf
cd /etc
mv ntp.conf ntp.conf.bak
vim /etc/ntp.conf              # 写入: server ${META_IP}

# 立即同步并写回硬件时钟
ntpdate meta
hwclock -w
chkconfig crond on
systemctl start crond

# 定时任务：crontab -e 写入
*/10 * * * * /usr/sbin/ntpdate 192.0.2.10
```

（cron 行中 IP 为 `${META_IP}` 示例值。）

## 5. 新客户端运行依赖

```shell
yum install -y libibverbs libibverbs-devel rsync vim numactl-libs jemalloc fuse3 fuse3-devel
```

如果 `fuse3` 或 `fuse3-devel` 包不存在：

```shell
yum search fuse
```

然后安装系统实际提供的 fuse3 包。

## 6. meta 节点准备凭据与 sshpass

```shell
yum install -y sshpass        # 如无 sshpass 包，先配置 yum 源或手动安装
```

凭据注入（**优先配置 SSH key 免密：`ssh-copy-id ${SSH_USER}@${NEW_CLIENT}`，配置成功后可跳过 sshpass**；
必须用密码时交互输入一次，不进 shell history、不写入任何文件）：

```shell
read -s -p "Password for ${NEW_CLIENT}: " SSHPASS; export SSHPASS; printf '\n'
```

设置 SSH/rsync 变量并测试：

```shell
export SSH_OPTS='-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'
export RSYNC_RSH="sshpass -e ssh ${SSH_OPTS}"      # SSH key 免密时改为 RSYNC_RSH="ssh ${SSH_OPTS}"

sshpass -e ssh ${SSH_OPTS} ${NEW_CLIENT} 'hostname && ip addr show eth5 && rdma link'
```

## 7. meta 推送 FUSE 文件到新客户端

创建目录：

```shell
sshpass -e ssh ${SSH_OPTS} ${NEW_CLIENT} 'mkdir -p /var/log/3fs'
sshpass -e ssh ${SSH_OPTS} ${NEW_CLIENT} "mkdir -p ${INSTALL_PREFIX}/{bin,etc}"
sshpass -e ssh ${SSH_OPTS} ${NEW_CLIENT} "mkdir -p ${SRC_ROOT}/fio"
sshpass -e ssh ${SSH_OPTS} ${NEW_CLIENT} "mkdir -p ${MOUNT_POINT}"
```

推送二进制、配置和库（**文件清单**，与 `installation-guide.md` §5.9 一致）：

```shell
rsync -avz -e "${RSYNC_RSH}" ${SRC_ROOT}/3fs/build/bin/hf3fs_fuse_main ${NEW_CLIENT}:${INSTALL_PREFIX}/bin/
rsync -avz -e "${RSYNC_RSH}" ${SRC_ROOT}/3fs/build/bin/admin_cli ${NEW_CLIENT}:${INSTALL_PREFIX}/bin/
rsync -avz -e "${RSYNC_RSH}" ${SRC_ROOT}/3fs/configs/hf3fs_fuse_main_launcher.toml ${NEW_CLIENT}:${INSTALL_PREFIX}/etc/
rsync -avz -e "${RSYNC_RSH}" ${SRC_ROOT}/3fs/configs/hf3fs_fuse_main.toml ${NEW_CLIENT}:${INSTALL_PREFIX}/etc/
rsync -avz -e "${RSYNC_RSH}" ${SRC_ROOT}/3fs/configs/hf3fs_fuse_main_app.toml ${NEW_CLIENT}:${INSTALL_PREFIX}/etc/
rsync -avz -e "${RSYNC_RSH}" ${SRC_ROOT}/3fs/deploy/systemd/hf3fs_fuse_main.service ${NEW_CLIENT}:/usr/lib/systemd/system/
rsync -avz -e "${RSYNC_RSH}" ${SRC_ROOT}/3fs/build/src/lib/api/libhf3fs_api_shared.so ${NEW_CLIENT}:/usr/lib/
rsync -avz -e "${RSYNC_RSH}" ${INSTALL_PREFIX}/etc/token.txt ${NEW_CLIENT}:${INSTALL_PREFIX}/etc/
rsync -avz -e "${RSYNC_RSH}" ${INSTALL_PREFIX}/etc/admin_cli.toml ${NEW_CLIENT}:${INSTALL_PREFIX}/etc/
rsync -avz -e "${RSYNC_RSH}" ${INSTALL_PREFIX}/etc/fdb.cluster ${NEW_CLIENT}:${INSTALL_PREFIX}/etc/
rsync -avz -e "${RSYNC_RSH}" /usr/lib64/libfdb_c.so ${NEW_CLIENT}:/usr/lib64/
rsync -avz -e "${RSYNC_RSH}" ${SRC_ROOT}/3fs/benchmarks/fio_usrbio/hf3fs_usrbio.so ${NEW_CLIENT}:/usr/lib64/
rsync -avz -e "${RSYNC_RSH}" ${SRC_ROOT}/fio/fio ${NEW_CLIENT}:${SRC_ROOT}/fio/
```

设置权限并刷新动态库：

```shell
sshpass -e ssh ${SSH_OPTS} ${NEW_CLIENT} "chmod 0755 ${INSTALL_PREFIX}/bin/hf3fs_fuse_main ${INSTALL_PREFIX}/bin/admin_cli"
sshpass -e ssh ${SSH_OPTS} ${NEW_CLIENT} "chmod 0755 /usr/lib/libhf3fs_api_shared.so /usr/lib64/hf3fs_usrbio.so ${SRC_ROOT}/fio/fio"
sshpass -e ssh ${SSH_OPTS} ${NEW_CLIENT} "chmod 600 ${INSTALL_PREFIX}/etc/token.txt"
sshpass -e ssh ${SSH_OPTS} ${NEW_CLIENT} 'ldconfig'
```

## 8. 新客户端修改配置

### admin_cli.toml

`vim ${INSTALL_PREFIX}/etc/admin_cli.toml`，确认内容：

```toml
cluster_id = "stage"                                   # ${CLUSTER_ID}

[fdb]
clusterFile = '/opt/3fs/etc/fdb.cluster'               # ${INSTALL_PREFIX}/etc/fdb.cluster

[mgmtd_client]
mgmtd_server_addresses = ["RDMA://192.0.2.10:8000"]    # RDMA://${META_IP}:8000
```

检查：

```shell
${INSTALL_PREFIX}/bin/admin_cli -cfg ${INSTALL_PREFIX}/etc/admin_cli.toml help
```

### hf3fs_fuse_main_launcher.toml

```toml
cluster_id = "stage"                                   # ${CLUSTER_ID}
mountpoint = '/3fs/stage'                              # ${MOUNT_POINT}
token_file = '/opt/3fs/etc/token.txt'                  # ${INSTALL_PREFIX}/etc/token.txt

[mgmtd_client]
mgmtd_server_addresses = ["RDMA://192.0.2.10:8000"]    # RDMA://${META_IP}:8000
```

### hf3fs_fuse_main.toml

```toml
[mgmtd]
mgmtd_server_addresses = ["RDMA://192.0.2.10:8000"]    # RDMA://${META_IP}:8000

[common.monitor.reporters.monitor_collector]
remote_ip = "192.0.2.10:10000"                         # ${META_IP}:10000
```

**把文件里所有 `filter_list` 改成新客户端本机网卡名**（示例 eth5，即 `${NEW_CLIENT_IFACE}`）：

```toml
filter_list = ['eth5']
```

修改后一键核对：

```shell
grep -nE "filter_list|mgmtd|monitor_collector|mountpoint|token|cluster_id" ${INSTALL_PREFIX}/etc/hf3fs_fuse_main*.toml
```

> **systemd 单元检查**：确认 `/usr/lib/systemd/system/hf3fs_fuse_main.service` 的 ExecStart 显式带
> `--cfg` 与 `--launcher_cfg`（缺 `--cfg` 会导致写性能只有正常节点约 1/4，见
> `installation-guide.md` §5.9 步骤 5）。

## 9. 新客户端 ldd 检查

```shell
ldd ${INSTALL_PREFIX}/bin/hf3fs_fuse_main | grep 'not found' || true
ldd ${INSTALL_PREFIX}/bin/admin_cli | grep 'not found' || true
ldd /usr/lib/libhf3fs_api_shared.so | grep 'not found' || true
```

**如有任何 `not found`，先补依赖（yum 安装对应库或从 meta 拷贝 so + ldconfig），绝不启动服务。**

## 10. meta 更新 FUSE 配置到 mgmtd

```shell
${INSTALL_PREFIX}/bin/admin_cli -cfg ${INSTALL_PREFIX}/etc/admin_cli.toml \
  "set-config --type FUSE --file ${INSTALL_PREFIX}/etc/hf3fs_fuse_main.toml"
${INSTALL_PREFIX}/bin/admin_cli -cfg ${INSTALL_PREFIX}/etc/admin_cli.toml "list-nodes"
```

## 11. 新客户端启动 FUSE 并验证

```shell
systemctl daemon-reload
systemctl start hf3fs_fuse_main
systemctl status hf3fs_fuse_main --no-pager -l

df -h | grep ${MOUNT_POINT}
mount | grep ${MOUNT_POINT}
```

meta 节点确认新节点加入：

```shell
${INSTALL_PREFIX}/bin/admin_cli -cfg ${INSTALL_PREFIX}/etc/admin_cli.toml "list-nodes"
# 预期看到新增 FUSE 节点 ${NEW_CLIENT}
```

## 12. 失败排查清单

新客户端执行：

```shell
systemctl status hf3fs_fuse_main --no-pager -l
journalctl -u hf3fs_fuse_main -n 200 --no-pager
grep -nE "mgmtd|monitor_collector|filter_list|listen_port|mountpoint|token|cluster_id|rdma" ${INSTALL_PREFIX}/etc/hf3fs_fuse_main*.toml
ip addr show ${NEW_CLIENT_IFACE}
rdma link
ping -c 3 ${META_IP}
```

meta 执行：

```shell
systemctl status mgmtd_main --no-pager -l
journalctl -u mgmtd_main -n 100 --no-pager
${INSTALL_PREFIX}/bin/admin_cli -cfg ${INSTALL_PREFIX}/etc/admin_cli.toml "list-nodes"
```

排查优先级：filter_list 网卡名是否为本机实际名 → token/fdb.cluster 是否推送且权限正确 →
ldd 是否缺库 → RDMA link 是否 ACTIVE → mgmtd 地址/端口是否可达。

## 13. 清理 meta 上的凭据变量

```shell
unset SSHPASS
unset RSYNC_RSH
```
