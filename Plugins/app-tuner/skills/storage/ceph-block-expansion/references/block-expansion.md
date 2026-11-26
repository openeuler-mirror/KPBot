# Ceph 块存储扩容指南（鲲鹏节点加入 x86 集群）

> 来源：《鲲鹏 BoostKit 分布式存储使能套件 Ceph 编译&部署&调优&测试指南》（文档版本 07，
> 2025-06-30）第 12 章「Ceph 扩容指南」全文蒸馏（§12.2 环境要求 / §12.3 扩容机器配置与安装 /
> §12.4.1 块存储扩容路径）。本文件按 SOP 步骤组织，命令与配置片段
> 保留自原文，检视修正项以文中标注为准；扩容含删除 Monitor 等破坏性操作，
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

**扩容基线独立声明**：本 skill 的示例环境与部署姊妹 skill `ceph-block-storage-deployment`
（同批 PR #446，尚未合入）的示例环境相互独立、不通用——本 skill 扩容基线为 Ceph 12.2.11
（rpm-luminous 源），示例 IP 家族 192.0.2.x / 198.51.100.x / 203.0.113.x 末段为
.156~.158 + 192/197/198（client1=.192）；部署姊妹 #446 的示例环境为 Ceph 14.2.8 /
IP 末段 .166~168（client1=.160）。两套示例环境不得跨用：不得把本 skill 的命令与示例 IP
套到 #446 的 14.2.8 环境上，也不得把 #446 的示例版本/IP 代入本 skill 的扩容步骤。

## 1. 扩容机器配置与安装（§12.3）

### 1.1 防火墙调整（最小化范围，执行前须授权确认）

原指南要求在所有 Ceph 节点和 Client 节点停用防火墙；扩容场景按最小化原则收敛执行范围：
仅新增节点 ceph4/ceph5 按需停用或放行端口，现有节点 ceph1~ceph3 与客户端 client1 不停用
整体防火墙、只追加本次扩容所需规则。执行前必须获得用户授权确认，并记录变更窗口
（开始/结束时间）。

- 新增节点 ceph4/ceph5（本次扩容操作对象，方式二选一）：

```bash
# 方式A 按需停用（仅新增节点 ceph4/ceph5）
systemctl stop firewalld
systemctl disable firewalld
systemctl status firewalld
# 方式B 保持运行并放行 Ceph 端口（mon 6789/3300、OSD 6800-7568）
firewall-cmd --permanent --add-port=6789/tcp
firewall-cmd --permanent --add-port=3300/tcp
firewall-cmd --permanent --add-port=6800-7568/tcp
firewall-cmd --reload
```

- 现有节点 ceph1~ceph3：保持防火墙运行，仅追加放行来自新节点 ceph4/ceph5 的 Ceph 端口
  （rich rule 示例，source 地址按现场 ceph4/ceph5 的 Public 与 Cluster 地址替换）：

```bash
# ceph4 Public 地址
firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="198.51.100.197/32" port port="6789" protocol="tcp" accept'
firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="198.51.100.197/32" port port="3300" protocol="tcp" accept'
firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="198.51.100.197/32" port port="6800-7568" protocol="tcp" accept'
# ceph5 Public 地址
firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="198.51.100.198/32" port port="6789" protocol="tcp" accept'
firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="198.51.100.198/32" port port="3300" protocol="tcp" accept'
firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="198.51.100.198/32" port port="6800-7568" protocol="tcp" accept'
# ceph4 Cluster 地址（OSD peering/回填流量走 cluster_network）
firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="203.0.113.197/32" port port="6789" protocol="tcp" accept'
firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="203.0.113.197/32" port port="3300" protocol="tcp" accept'
firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="203.0.113.197/32" port port="6800-7568" protocol="tcp" accept'
# ceph5 Cluster 地址
firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="203.0.113.198/32" port port="6789" protocol="tcp" accept'
firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="203.0.113.198/32" port port="3300" protocol="tcp" accept'
firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="203.0.113.198/32" port port="6800-7568" protocol="tcp" accept'
firewall-cmd --reload
```

> 注意：Public 与 Cluster 网段都须放行，漏 Cluster 段会导致 OSD peering/回填被拦，
> 与扩容完成判据（ceph -s 数据迁移结束恢复健康）矛盾。

- 客户端 client1：不停用防火墙；client1 作为压力机通常仅需出站访问集群（默认放行），
  如现场策略要求入站限制，参照上面 rich rule 示例按需追加并登记变更窗口。

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
  回退说明）。

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
执行，执行前必须获得用户授权确认；变更前备份原值（cp -n 不覆盖已有备份）：

```bash
# 仅在 ceph4、ceph5 执行；执行前备份原配置（cp -n 已有备份不覆盖）
cp -n /etc/selinux/config /etc/selinux/config.bak
# 临时修改，下次重启自动失效
setenforce permissive
# 永久设置（下次重启自动生效）：vim /etc/selinux/config 修改 SELINUX=permissive
# 扩容收尾恢复（见 SKILL.md 回退说明）：setenforce 1；/etc/selinux/config 恢复
# SELINUX=enforcing（从备份恢复或改回）；若曾停用 firewalld：systemctl enable --now firewalld
# 并放行 Ceph 端口 6789/3300/6800-7568
```

### 1.6 配置 epel 源

在所有集群和客户端节点执行：

```bash
yum install epel-release -y
```

### 1.7 配置 Ceph 镜像源

在所有集群和客户端节点建立 `/etc/yum.repos.d/ceph.repo`，加入如下内容
（注意：扩容基线 12.2.11 对应 rpm-luminous）：

```ini
[Ceph]
name=Ceph packages for $basearch
baseurl=http://download.ceph.com/rpm-luminous/el7/$basearch
enabled=1
gpgcheck=1
type=rpm-md
gpgkey=https://download.ceph.com/keys/release.asc
priority=1

[Ceph-noarch]
name=Ceph noarch packages
baseurl=http://download.ceph.com/rpm-luminous/el7/noarch
enabled=1
gpgcheck=1
type=rpm-md
gpgkey=https://download.ceph.com/keys/release.asc
priority=1

[ceph-source]
name=Ceph source packages
baseurl=http://download.ceph.com/rpm-luminous/el7/SRPMS
enabled=1
gpgcheck=1
type=rpm-md
gpgkey=https://download.ceph.com/keys/release.asc
priority=1
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
yum -y install ceph*12.2.11 --exclude ceph-debuginfo
# 步骤3 扩容节点安装结束后查看版本
ceph -v
```

## 2. 块存储扩容（§12.4.1）

### 2.1 扩容机器添加 Monitor

修改 ceph1 的 /etc/ceph/ceph.conf，在 mon_initial_members 和 mon_host 中增量添加 ceph4 和
ceph5（在现有值末尾追加，以现场 conf 实际内容为准，不依赖本文示例的 before 值）：

```bash
# 步骤1 编辑 ceph.conf（增量式修改，避免覆盖现场已有配置）
cd /etc/ceph/
vim ceph.conf
# 1. 在现有 mon_initial_members 末尾追加 ceph4、ceph5，形如
#    mon_initial_members=ceph1,ceph2,ceph3 → 追加后 mon_initial_members=ceph1,ceph2,ceph3,ceph4,ceph5
# 2. 在现有 mon_host 末尾追加 ceph4、ceph5 的 Public 地址，形如
#    <现有 mon_host>,198.51.100.197,198.51.100.198
# 步骤2 将 ceph.conf 从 ceph1 节点推送至各个节点（config push 仅同步配置，不下发
#        client.admin keyring；如现场确需下发管理密钥，仅限获准的管理主机并登记变更窗口）
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
# 步骤2 将 ceph.conf 从 ceph1 节点推送至各个节点（config push 仅同步配置，不下发
#        client.admin keyring；管理密钥仅限获准的管理主机）
ceph-deploy --overwrite-conf config push ceph1 ceph2 ceph3 ceph4 ceph5
# 步骤3 删除 monitor ceph2 和 ceph3
ceph-deploy mon destroy ceph2 ceph3
```

### 2.2 部署 MGR 与 OSD

```bash
# 为扩容的节点 ceph4 和 ceph5 创建 MGR
ceph-deploy mgr create ceph4 ceph5
# 给扩容进来的服务器创建 OSD（每台服务器 12 块硬盘）
# 注意：禁止按盘符循环盲建 OSD；逐机核对设备标识后仅对确认的数据盘执行，
# 写盘用 /dev/disk/by-id 路径；单盘失败即停止并报告，不得带错继续。
# 步骤1 在 ceph4 上核对数据盘：lsblk 查看块设备拓扑；df -l / 确认根盘设备并排除；
#        findmnt 排除已挂载盘；ls -l /dev/disk/by-id/ 生成盘符 → by-id 映射
lsblk
df -l /
findmnt
ls -l /dev/disk/by-id/
# 步骤2 仅对 ceph4 上确认为数据盘的设备逐盘创建 OSD（<DATA_DISK_N> 用步骤1 核对的
#        by-id 路径替换；遇错即停：任一盘创建失败即停止后续盘并报告）
ceph-deploy osd create ceph4 --data /dev/disk/by-id/<DATA_DISK_1>
ceph-deploy osd create ceph4 --data /dev/disk/by-id/<DATA_DISK_2>
# …… 逐盘执行至 <DATA_DISK_12>，每盘创建后用 ceph osd tree 核对 OSD 上线状态
# 步骤3 在 ceph5 上重复步骤1~2（核对 ceph5 的数据盘后逐盘创建）
```

### 2.3 配置存储池

```bash
# 步骤1 查看存储池信息
ceph osd lspools
# 步骤2 修改相应的 pgnum 和 pgpnum（pg 计算规则——官方口径：
# Total PGs = (OSD 总数 × 100) / 最大副本数（EC 为 K+M），向上取整到最近的 2 的整数次幂；
# 多池按各池数据占比分摊。修正说明：源指南原文在公式中额外按 pool 数均分作除法，
# 与官方口径不一致，以官方口径为准。
# 变更限制：Ceph 12.2.11 拒绝调低 pg_num，变更前核准目标 PG 数并入变更窗口台账，
# 调整前原值仅作防呆记录，不承诺用同款命令还原）
ceph osd pool set poolname pg_num 2048
ceph osd pool set poolname pgp_num 2048
```

### 2.4 添加块设备

```bash
# 步骤1 列出存储池信息
ceph osd lspools
# 步骤2 在 ceph1 上创建 image：在 RBD 存储池中创建 5 个块设备，每个 200G，脚本命令如下
#!/bin/bash
pool="vdbench"
size="204800"
createimages()
{
for image in {0..4}
do
rbd create image${image} --size ${size} --pool ${pool} --image-format 2 --image-feature layering
sleep 1
done
}
createimages
# 步骤3 查看存储池的 image
rbd ls vdbench
```

### 2.5 验证扩容

扩容后，Ceph 会通过迁移来自其他 OSD 的一些 pg 到新添进的 OSD，再次平衡数据。

```bash
# 步骤1 确认数据迁移结束后集群是否恢复健康
ceph -s
# 步骤2 确定集群的存储容量是否增加
ceph osd df
```
