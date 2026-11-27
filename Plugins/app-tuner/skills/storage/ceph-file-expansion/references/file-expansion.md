# Ceph 文件存储扩容指南（鲲鹏节点加入 x86 集群）

> 来源：《鲲鹏 BoostKit 分布式存储使能套件 Ceph 编译&部署&调优&测试指南》（文档版本 07，
> 2025-06-30）第 12 章「Ceph 扩容指南」全文蒸馏（§12.2 环境要求 / §12.3 扩容机器配置与安装 /
> §12.4.2 文件存储扩容路径）。本文件按 SOP 步骤组织，命令与配置片段以原文为基础，
> 检视修复引入的偏差（防火墙收敛、密钥受限身份下发、yum 源 https、建盘前设备核对、
> PG 公式官方口径等）均以「安全增强」或注释标注；扩容含删除 Monitor 等破坏性操作，
> 执行前须二次确认（见 SKILL.md 铁律 4）。

## 0. 版本与适用矩阵

| 项 | 版本 |
|---|---|
| Ceph | 12.2.11（建议版本） |
| ceph-deploy | 2.0.1 |
| OS | CentOS Linux release 7.6.1810 或 RHEL 7.5 |
| 硬件 | 鲲鹏服务器：鲲鹏920 5220 处理器，2*32核 2600MHz，内存 12*16GB 2666MHz，IN200 网卡 4*25GE；系统盘 RAID1（2*960GB SATA SSD）；数据盘 RAID 模式下使能 JBOD（12*4TB SATA HDD）；RAID 卡 Avago SAS 3508 |
| 场景 | 使用鲲鹏服务器对原本以 x86 服务器为节点的 Ceph 集群进行扩容：扩容前 3 台 x86 服务器（本文档以华为 FusionServer Pro 2288H V5 为例），新扩容 ceph4、ceph5 为鲲鹏服务器 |

集群规划（扩容前 → 扩容后）：ceph1~ceph3 保持 192.0.2.156~158（管理）/ 198.51.100.156~158
（Public）/ 203.0.113.156~158（Cluster）；扩容后新增 ceph4 = 192.0.2.197 / 198.51.100.197 /
203.0.113.197，ceph5 = 192.0.2.198 / 198.51.100.198 / 203.0.113.198；客户端 client1 =
192.0.2.192 / 198.51.100.192。管理 IP 用于 BMC 带外管理；Cluster Network 用于集群间同步数据
（任选一个 25GE 网口）；Public Network 供计算节点访问块设备等（任选一个 25GE 网口）；客户端
作为压力机需与集群前端网络同网段。示例网段采用 RFC 5737 文档保留地址（192.0.2.0/24 管理，
198.51.100.0/24 Public，203.0.113.0/24 Cluster），按现场规划替换；后文 hosts、NTP、ceph.conf、
mount 挂载等命令中的示例 IP 同样为文档保留地址。

## 1. 扩容机器配置与安装（§12.3）

### 1.1 防火墙策略（收敛范围，需授权）

原指南要求在所有节点停止 firewalld。安全增强：不得为扩容整体关闭防火墙，收敛为两类
动作，且均须先取得变更授权并记录变更窗口。风险标注：本节与 §1.5 的降防护动作仅限
受控内网与限变更窗口内执行，窗口结束前必须恢复（见 SKILL.md 输出契约「回退步骤」）。

新增节点 ceph4/ceph5：保留 firewalld 并放通 Ceph 所需端口（MON 6789/3300、
OSD/MDS/MGR 6800-7568）：

```bash
firewall-cmd --permanent --add-port=6789/tcp
firewall-cmd --permanent --add-port=3300/tcp
firewall-cmd --permanent --add-port=6800-7568/tcp
firewall-cmd --reload
```

若现场评估确需停止防火墙，仅在授权与变更窗口内对新增节点执行（不得扩散到其他节点）：

```bash
systemctl stop firewalld
systemctl disable firewalld
systemctl status firewalld
```

现有节点 ceph1~ceph3 与 client1：不停止防火墙，仅添加本次扩容所需规则（rich rule
示例，按现场网段替换）：

```bash
firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="198.51.100.197/32" accept'
firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="198.51.100.198/32" accept'
firewall-cmd --reload
```

### 1.2 配置主机名

```bash
# 步骤1 配置永久静态主机名（ceph4~ceph5）
hostnamectl --static set-hostname ceph4
hostnamectl --static set-hostname ceph5
# 步骤2 编辑各个服务端和客户端节点的 /etc/hosts，添加如下内容
198.51.100.156   ceph1
198.51.100.157   ceph2
198.51.100.158   ceph3
198.51.100.197   ceph4
198.51.100.198   ceph5
198.51.100.192   client1
```

### 1.3 配置 NTP

```bash
# 步骤1 安装 NTP 服务（所有集群和客户端节点）
yum -y install ntp ntpdate
# 备份旧配置
cd /etc && mv ntp.conf ntp.conf.bak
# 以 ceph1 为 NTP 服务端节点，在 ceph1 新建 /etc/ntp.conf，新增（restrict 网段为 ceph1 的网段与掩码）
restrict 127.0.0.1
restrict ::1
restrict 198.51.100.0 mask 255.255.255.0
server 127.127.1.0
fudge 127.127.1.0 stratum 8
# 在 ceph4、ceph5 及所有客户机节点新建 /etc/ntp.conf，新增
server 198.51.100.156
```

```bash
# 步骤2 启动 NTP 服务
# 1. 在 ceph1 节点启动 NTP 服务并检查状态
systemctl start ntpd
systemctl enable ntpd
systemctl status ntpd
# 2. 在除 ceph1 的所有节点强制同步 server（ceph1）时间
ntpdate ceph1
# 3. 在除 ceph1 的所有节点写入硬件时钟，避免重启后失效
hwclock -w
# 4. 在除 ceph1 的所有节点安装并启动 crontab 工具
yum install -y crontabs
chkconfig crond on
systemctl start crond
crontab -e
# 5. 添加以下内容，每隔 10 分钟自动与 ceph1 同步时间
*/10 * * * * /usr/sbin/ntpdate 198.51.100.156
```

### 1.4 配置免密登录（安全基线）

为 ceph1 与 client1 补齐对扩容节点 ceph4/ceph5 的免密（对既有节点的免密沿用部署期配置，
不在本节范围）。安全基线：

- 私钥不裸奔：ssh-keygen 生成时设置 passphrase 并用 ssh-agent 托管，禁止直接回车生成
  无口令私钥（确需免口令运行时须在产出中标注风险）；
- 主机指纹校验：首次连接前用 `ssh-keyscan <host> >> ~/.ssh/known_hosts` 预填已知主机，
  ssh/ssh-copy-id 显式带 `-o StrictHostKeyChecking=yes`（本 skill OS 矩阵 CentOS 7.6/RHEL 7.5
  自带的 OpenSSH 7.4p1 过旧，不支持 OpenSSH 7.5 起才引入的 StrictHostKeyChecking 新取值，
  原样执行报 Unsupported option 拒连，禁用）；
  禁止 `StrictHostKeyChecking=no` 与 `UserKnownHostsFile=/dev/null`；主机指纹变更（如重装系统）
  时先 `ssh-keygen -R <host>` 清除旧记录再重新发放，不得清空 known_hosts 绕过校验；
- 来源限定：公钥写入目标节点 authorized_keys 时加 `from="管理网段"` 前缀（下例以 Public
  网段 198.51.100.0/24 为例，按现场管理网段替换）；
- 收尾撤销：扩容收尾从 ceph4/ceph5 的 authorized_keys 移除本次下发条目（见 SKILL.md
  回退步骤）。

```bash
# 步骤1 在 ceph1 节点生成专用密钥对（推荐 ed25519 + KDF 轮次；需 RSA 时用 rsa -b 4096；
#        提示设置 passphrase 时输入口令，不要回车留空）并托管给 agent
ssh-keygen -t ed25519 -a 100 -f ~/.ssh/id_ed25519_ceph_exp -C "ceph-exp-key@ceph1"
eval "$(ssh-agent -s)" && ssh-add ~/.ssh/id_ed25519_ceph_exp
# 步骤2 ceph1 发放公钥到扩容节点（先 ssh-keyscan 预填新节点主机指纹，再以
#        StrictHostKeyChecking=yes 发放；目标节点 authorized_keys 条目加 from="198.51.100.0/24"
#        来源限定）
for i in 4 5; do ssh-keyscan ceph$i >> ~/.ssh/known_hosts; done
for i in 4 5; do ssh-copy-id -i ~/.ssh/id_ed25519_ceph_exp.pub -o StrictHostKeyChecking=yes ceph$i; done
# 步骤3 在 client1 节点重复步骤1~2（密钥注释改为 ceph-exp-key@client1，仅发放到 ceph4/ceph5）
# 主机指纹变更时：ssh-keygen -R ceph4 && ssh-keygen -R ceph5 后重新发放
```

### 1.5 设置 permissive 模式（仅新增节点，执行前须授权确认）

原指南要求在所有主客户机节点设置 permissive；扩容场景收敛为仅在新增节点 ceph4/ceph5
执行，执行前必须获得用户授权确认；变更前备份原值（cp -n 不覆盖已有备份）。
风险标注：SELinux 降防护限受控内网、限变更窗口，窗口结束前必须恢复（见 SKILL.md
输出契约「回退步骤」）：

```bash
# 仅在 ceph4、ceph5 执行；执行前备份原配置（cp -n 已有备份不覆盖）
cp -n /etc/selinux/config /etc/selinux/config.bak
# 临时修改，下次重启自动失效
setenforce permissive
# 永久设置（下次重启自动生效）：vim /etc/selinux/config 修改 SELINUX=permissive
# 扩容收尾恢复（见 SKILL.md 回退步骤）：setenforce 1；/etc/selinux/config 恢复
# SELINUX=enforcing（从备份恢复或改回）
```

### 1.6 配置 epel 源

在所有集群和客户端节点执行：

```bash
yum install epel-release -y
```

### 1.7 配置 Ceph 镜像源

在所有集群和客户端节点建立 `/etc/yum.repos.d/ceph.repo`，加入如下内容
（注意：扩容基线 12.2.11 对应 rpm-luminous；安全增强：baseurl 由明文 http 收敛为
https，新增 repo_gpgcheck=1 校验仓库元数据签名，并删除原指南的源码仓段——源码包
不用于部署，不得保留）：

```ini
[Ceph]
name=Ceph packages for $basearch
baseurl=https://download.ceph.com/rpm-luminous/el7/$basearch
enabled=1
gpgcheck=1
repo_gpgcheck=1
type=rpm-md
gpgkey=https://download.ceph.com/keys/release.asc
priority=1

[Ceph-noarch]
name=Ceph noarch packages
baseurl=https://download.ceph.com/rpm-luminous/el7/noarch
enabled=1
gpgcheck=1
repo_gpgcheck=1
type=rpm-md
gpgkey=https://download.ceph.com/keys/release.asc
priority=1
```

导入并固定 GPG key 指纹（核对与官方 keys 页面公布指纹一致后方可启用该源，指纹入台账）：

```bash
rpm --import https://download.ceph.com/keys/release.asc
rpm -q gpg-pubkey --qf '%{NAME}-%{VERSION}-%{RELEASE}\t%{SUMMARY}\n'
```

更新 yum 源：

```bash
yum clean all && yum makecache
```

### 1.8 安装 Ceph 软件

```bash
# 步骤1 在原 Ceph 节点查看当前使用的版本
ceph -v
# 步骤2 在新扩容的节点安装相应版本的 Ceph 软件
# 安全增强：钉死精确包名，禁止通配安装、不装 debuginfo 包；<release> 按仓库实际
# rpm release 填写（yum list ceph-12.2.11* 查询后取精确值）
yum -y install ceph-12.2.11-<release>
# 步骤3 扩容节点安装结束后查看版本并复核签名与指纹（版本与指纹入台账）
ceph -v
rpm -q ceph
# rpm -K 复核 yum 下载缓存中 rpm 的 GPG 签名（输出签名与 digest OK 即通过）
rpm -K /var/cache/yum/x86_64/7/Ceph/packages/ceph-12.2.11-<release>.x86_64.rpm
```

## 3. 文件存储扩容（§12.4.2）

### 3.1 扩容机器添加 Monitor

修改 ceph1 的 /etc/ceph/ceph.conf，在 mon_initial_members 和 mon_host 中添加 ceph4 和
ceph5 的 IP 地址：

```bash
# 步骤1 编辑 ceph.conf
cd /etc/ceph/
vim ceph.conf
# 1. 将 mon_initial_members=ceph1,ceph2,ceph3 修改为
#    mon_initial_members=ceph1,ceph2,ceph3,ceph4,ceph5
# 2. 将 mon_host=198.51.100.156,198.51.100.157,198.51.100.158 修改为
#    mon_host=198.51.100.156,198.51.100.157,198.51.100.158,198.51.100.197,198.51.100.198
# 步骤2 将 ceph.conf 从 ceph1 节点推送至各个节点（安全增强：config push 仅同步配置，
#        不向各节点分发 admin 密钥；管理密钥只保留在管理主机 ceph1）
ceph-deploy --overwrite-conf config push ceph1 ceph2 ceph3 ceph4 ceph5
# 步骤3 在 ceph1 创建 monitor
ceph-deploy mon create ceph4 ceph5
# 步骤4 查看 mon 状态（mon stat 中出现扩容机器信息即创建成功）
ceph mon stat
```

（可选）删除 Monitor：删除 monitor 对集群影响较大，一般需提前规划好，如不需要可以不删除。
以删除 monitor ceph2 和 ceph3 为例（破坏性操作，执行前二次确认）：

```bash
# 步骤1 编辑 ceph.conf：将 mon_initial_members=ceph1,ceph2,ceph3,ceph4,ceph5 修改为
#        mon_initial_members=ceph1,ceph4,ceph5；将 mon_host 修改为
#        mon_host=198.51.100.156,198.51.100.197,198.51.100.198
# 步骤2 将 ceph.conf 从 ceph1 节点推送至各个节点（同上：config push 仅同步配置，
#        管理密钥不随配置分发）
ceph-deploy --overwrite-conf config push ceph1 ceph2 ceph3 ceph4 ceph5
# 步骤3 删除 monitor ceph2 和 ceph3
ceph-deploy mon destroy ceph2 ceph3
```

### 3.2 客户端重新挂载

删除 Monitor 后（或扩容变更后）在 client1 节点执行。安全增强：不得打印/回显任何
keyring 与 key 内容，密钥不得进入命令行参数或 shell 历史，统一走 secretfile（从 keyring
提取的纯 key 文件，权限 600）；推荐使用受限客户端身份而非 admin 身份挂载。
本节步骤编号从步骤1 重新起号：

```bash
# 步骤1 在 ceph1 创建受限客户端身份（仅授予 CephFS 所需最小权限），生成 keyring 并收紧
#       权限为 600。keyring 与纯 key 文件角色分开标注：keyring 留在管理主机 ceph1，供
#       集群侧受限身份验证（ceph -s --name client.fs-expansion -k
#       /etc/ceph/ceph.client.fs-expansion.keyring）；不回显、不入台账
ceph auth get-or-create client.fs-expansion mon 'allow r' osd 'allow rwx pool=fs_data' mds 'allow rw path=/' -o /etc/ceph/ceph.client.fs-expansion.keyring
chmod 600 /etc/ceph/ceph.client.fs-expansion.keyring
# 步骤2 在 client1 提取纯 key 文件并完成 ceph1→client1 传输（不回显方式：key 值只重定向
#       落盘，不在终端输出，不进 argv/shell 历史/台账；ssh 口令由 tty 输入不回显）。
#       注意：12.2.11 的 mount.ceph secretfile 只取文件首行的纯 base64 key，keyring 文件
#       首行是段头（[client.fs-expansion]），整 keyring 文件作 secretfile 会报
#       secret is not valid base64，必须提取 key= 行的值另存 0600 纯 key 文件。
#       方式一（client1 经 ssh 到 ceph1 远端提取，key 值直接落盘到 client1）：
ssh ceph1 "grep -oP '(?<=key = ).*' /etc/ceph/ceph.client.fs-expansion.keyring" > /root/.ceph-fs-key
chmod 600 /root/.ceph-fs-key
#       方式二（scp 传输：在 ceph1 提取并收紧权限后传到 client1，client1 侧再收紧权限）：
#       (ceph1)   grep -oP '(?<=key = ).*' /etc/ceph/ceph.client.fs-expansion.keyring > /root/.ceph-fs-key
#       (ceph1)   chmod 600 /root/.ceph-fs-key && scp /root/.ceph-fs-key client1:/root/.ceph-fs-key
#       (client1) chmod 600 /root/.ceph-fs-key
# 步骤3 将 ceph1 的根目录以 Ceph 为 type 挂载至 client1 的 /mnt/cephfs
#       （secretfile 指向 0600 纯 key 文件，不指向 keyring 文件）
umount -t ceph /mnt/cephfs
mount -t ceph 198.51.100.156:6789:/  /mnt/cephfs -o name=fs-expansion,secretfile=/root/.ceph-fs-key,sync
```

### 3.3 部署 MGR、MDS 与 OSD

```bash
# 为扩容的节点 ceph4 和 ceph5 创建 MGR
ceph-deploy mgr create ceph4 ceph5
# 如果需要添加 MDS 服务器，执行：
ceph-deploy mds create ceph4 ceph5
# 安全增强：新建 MDS 默认 standby；若需增加 active MDS 数量，须调大 max_mds
# （luminous 多 active MDS 属实验特性，需评估稳定性并在变更核准后启用；
#  <fs> 为 ceph fs ls 中的文件系统名，<n> 为目标 active MDS 数）
ceph fs set <fs> max_mds <n>
```

安全增强（建 OSD 前逐机核对设备标识）：原指南按盘符 a~l 循环建盘，盘符在不同机型与
RAID 配置下可能对应系统盘、已挂载盘或现役 OSD 已占用盘，盲按盘符建 OSD 存在毁盘风险；
改为逐机核对后仅对确认的数据盘执行、遇错即停：

```bash
# 步骤1 逐机列出块设备与稳定标识（在 ceph4、ceph5 分别执行）
lsblk
ls -l /dev/disk/by-id
# 步骤2 排除非数据盘：df -l / 排除根盘；findmnt 排除已挂载盘；
#       ceph osd tree 对照现役 OSD 已占用设备（扩容节点首次建盘通常无占用）
df -l /
findmnt
ceph osd tree
# 步骤3 仅对核对确认的数据盘（12*4TB SATA HDD 数据盘，非系统盘 RAID1 卷、无挂载、
#       未被现役 OSD 占用）逐块执行建盘，命令写 /dev/disk/by-id/ 稳定路径；
#       任一步报错立即停止并核查，不得继续
ceph-deploy osd create ceph4 --data /dev/disk/by-id/<数据盘by-id>
ceph-deploy osd create ceph5 --data /dev/disk/by-id/<数据盘by-id>
# ……（示例每台 12 块数据盘，按步骤2核对结果逐块替换 <数据盘by-id>）
```

### 3.4 配置存储池

```bash
# 步骤1 采集存储池原值并回显入台账（fs_data 同理；用于变更后防呆对比——
#       Ceph 12.2.11 的 mon 拒绝调低 pg_num，原值不构成回退承诺，
#       目标 PG 数须在变更前核准）
ceph osd pool get fs_metadata pg_num
ceph osd pool get fs_metadata pgp_num
ceph osd pool get fs_data pg_num
ceph osd pool get fs_data pgp_num
# 步骤2 查看存储池信息
ceph fs ls
# 步骤3 修改相应的 pgnum 和 pgpnum（pg 计算规则官方口径：
# Total PGs = (OSD 总数 × 100) / 最大副本数，向上取整到最近的 2 的整数次幂；
# 多池场景按各池数据量占比分摊）
ceph osd pool set fs_metadata pg_num 256
ceph osd pool set fs_metadata pgp_num 256
ceph osd pool set fs_data pg_num 2048
ceph osd pool set fs_data pgp_num 2048
```

示例数字与官方口径吻合说明：扩容后集群共 60 个 OSD（ceph1~ceph3 与 ceph4/ceph5
各 12 块数据盘），最大副本数取 3 时，(60 × 100) / 3 = 2000，向上取整到 2 的整数次幂
为 2048（2^11），与 fs_data 2048 示例一致；fs_metadata 为元数据池，按数据量占比
分摊取较小值 256（2^8）。若计算结果低于当前 pg_num（12.2.11 不支持调低 pg_num），须
回到变更前重新核准目标 PG 数。

### 3.5 验证扩容

扩容后，Ceph 会通过迁移来自其他 OSD 的一些 pg 到新添进的 OSD，再次平衡数据。
集群侧验证后，还需在 client1 确认重新挂载的 CephFS 生效：

```bash
# 步骤1 确认数据迁移结束后集群是否恢复健康
ceph -s
# 步骤2 确定集群的存储容量是否增加
ceph osd df
# 步骤3 确认 MDS 扩容生效（active MDS 数与 max_mds 一致）
ceph fs status
ceph mds stat
# 步骤4 在 client1 确认 CephFS 挂载生效（文件系统类型为 ceph、容量与集群一致）
stat -f /mnt/cephfs
df -h /mnt/cephfs
```
