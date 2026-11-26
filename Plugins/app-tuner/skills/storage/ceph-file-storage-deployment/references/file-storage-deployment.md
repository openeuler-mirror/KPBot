# Ceph 文件存储部署指南（CentOS 7.6 & openEuler 20.03 & openEuler 22.03，ceph-deploy）

> 来源：《鲲鹏 BoostKit 分布式存储使能套件 Ceph 编译&部署&调优&测试指南》（文档版本 07，
> 2025-06-30）第 6 章「Ceph 文件存储 部署指南（CentOS 7.6&openEuler 20.03&openEuler 22.03）」
> 全文蒸馏（§6.2 环境要求 / §6.3.1 镜像源配置与 ceph-deploy 适配 / §6.3.2 系统配置 /
> §6.3.3 部署前环境分析与配置 / §6.4 安装 Ceph 与部署节点（含 §6.4.5 MDS 节点部署）/
> §6.5 部署文件存储功能 / §6.6 常见问题 / §6.7 故障排除）。本文件按 SOP 步骤组织，
> 命令与配置片段逐字保留（PDF 换行断词已复原为完整命令）。TLS 校验、磁盘核准清单、
> 限权身份与回退小节的安全/正确性加固为本仓口径补充（见各节「修正说明」与 §6）。

## 0. 版本与适用矩阵

| 项 | 版本 |
|---|---|
| Ceph | 14.2.8 Nautilus / 16.2.7 Pacific（其他版本安装也可参考本文档） |
| ceph-deploy | 2.0.1 / 2.1.0 |
| OS | CentOS Linux release 7.6.1810；openEuler 20.03 LTS SP1（Kernel: 4.19.90-2012.4.0.0053.oe1）；openEuler 22.03 LTS SP4（Kernel: 5.10.0-216.0.0.115.oe2203sp4 / ...oe2203sp4.aarch64） |
| 硬件 | 鲲鹏服务器：鲲鹏920 5220 处理器，2*32核 2600MHz，内存 8*16GB 2933MHz，IN200 网卡 4*25GE；系统盘 RAID1（2*960GB SATA SSD）；数据盘 RAID 模式下使能 JBOD（12*4TB SATA HDD）；NVMe SSD 缓存盘 1*ES3000 V5 3.2TB；RAID 控制卡 Avago SAS 3508 |

集群规划：服务端 ceph1~ceph3（管理 192.0.2.166~168、Public 198.51.100.166~168、Cluster
203.0.113.166~168），客户端 client1~client3（管理 192.0.2.160~162、Public 198.51.100.160~162）。
管理 IP 用于远程 SSH 管理；Cluster Network 用于集群内部节点间同步数据（任选一个 25GE 网口）；
Public Network 供其他节点访问（任选一个 25GE 网口）；客户端当做压力机时，需保证客户端业务口
IP 地址与集群的外部访问 IP 地址在同一个网段，建议选用 25GE 网口配置。Ceph 客户端和 Ceph
服务端都需安装 Ceph 软件。全新安装操作系统建议最小化安装；在线安装软件包需接入互联网。
示例网段采用 RFC 5737 文档保留地址（192.0.2.0/24 管理，198.51.100.0/24 Public，
203.0.113.0/24 Cluster），按现场规划替换；后文 hosts、NTP、ceph.conf、mount 挂载等命令中的
示例 IP 同样为文档保留地址。

硬盘划分：Ceph 14.2.8 以后都采用了 BlueStore 作为后端存储引擎，没有了 Jewel 版本的 Journal 盘
分区划分，而是变成 DB 分区（元数据分区）和 WAL 分区。每个 Ceph 节点配置 12 块 4TB 数据盘和
1 块 3.2TB 的 NVMe 盘：每个 4TB 数据盘作为一个 OSD 的数据分区，单个 NVMe 盘作为 12 个 OSD 的
DB、WAL 分区。一般 WAL 分区大于 10GB 就足够使用，Ceph 官方文档建议每个 DB 分区不小于每个数据
盘容量的 4%。本方案以 WAL 分区设置为 60GB、DB 分区设置为 180GB 为例：

| 数据盘 | DB分区 | WAL分区 |
|---|---|---|
| 4TB | 180GB | 60GB |

## 1. 镜像源配置与 ceph-deploy 适配（§6.3.1）

### 1.1 安装 liboath（按 OS 版本在集群内所有服务端和客户端节点选择对应操作）

CentOS 7.6：

```bash
# 步骤1 将 OS 对应的 everything 镜像源文件（CentOS-***aarch64-everything.iso）上传到 /root
# 步骤2 创建本地文件夹用于挂载本地镜像
mkdir -p /iso
# 步骤3 将 iso 文件挂载到本地文件夹
mount /root/CentOS-***aarch64-everything.iso /iso
# 步骤4 创建镜像 Yum 源：创建 /etc/yum.repos.d/centos.repo，加入以下内容
# [Base]
# name=Base
# baseurl=file:///iso
# enabled=1
# gpgcheck=0
# priority=1
# 步骤5 安装 epel-release 依赖包
yum install epel-release -y
# 步骤6 安装 liboath liboath-devel
yum install liboath liboath-devel -y
```

openEuler 20.03 & openEuler 22.03：openEuler 22.03 无需配置 epel 源，直接执行：

```bash
yum install liboath liboath-devel -y
```

openEuler 20.03 可参考 skill `ceph-build` 或 `build-guide.md` §2.2「编译 liboath-devel 软件包」手动编译。

### 1.2 配置 Ceph 镜像源（在集群内所有服务端和客户端节点配置）

在线源（Ceph 16.2.7，建议 openEuler 22.03 用户使用）：由于操作系统差异，Ceph 14.2.8 版本不建议
使用在线源进行安装，建议使用本地源编译后进行安装。

```bash
# 步骤1 备份原有 openEuler.repo 文件
cp /etc/yum.repos.d/openEuler.repo /etc/yum.repos.d/openEuler.repo.bak
# 步骤2/3 新建 /etc/yum.repos.d/openEuler.repo，添加 openEuler-22.03-LTS-SP4 的
# [OS]/[everything]/[EPOL]/[debuginfo]/[source]/[update]/[update-source] 七个 repo 段
# （完整内容与姊妹 skill `ceph-install` 的 installation-guide.md §1.2 在线源完全一致，
#   baseurl 均指向 http://repo.openeuler.org/openEuler-22.03-LTS-SP4/...，gpgcheck=1）
```

本地源：

- 编译后安装（针对 Ceph 14.2.8 版本）：参考 skill `ceph-build` 的「配置编译环境」完成编译环境配置，
  再参考其「Ceph 的编译与部署」完成 Ceph 软件的本地安装。
- 挂载本地镜像源（针对 Ceph 16.2.7 版本，建议 openEuler 22.03 用户）：

```bash
# 步骤1 备份原有 openEuler.repo 文件
mv /etc/yum.repos.d/openEuler.repo /etc/yum.repos.d/openEuler.repo.bak
# 步骤2 下载 openEuler-22.03-LTS-SP4 系统镜像，上传 iso 到 /root，挂载本地镜像源
mount /root/openEuler-22.03-LTS-SP4-everything-aarch64-dvd.iso /media/
# 步骤3 配置 repo 源：新建 /etc/yum.repos.d/local.repo，添加以下内容
# [local]
# name=system local
# baseurl=file:///media/
# enabled=1
# gpgcheck=0
```

### 1.3 安装 ceph-deploy（仅需在主节点 ceph1 执行）

CentOS 7.6：

```bash
# 步骤1 在 ceph1 节点安装 ceph-deploy
yum -y install ceph-deploy
# 步骤2 查看版本（若安装 2.0.1 版本，则应显示 2.0.1）
ceph-deploy --version
```

openEuler 20.03 & openEuler 22.03：

```bash
# 步骤1 安装 Git 工具
yum install -y git
# 步骤2 在 ceph1 获取 ceph-deploy 源码：固定 v2.1.0 tag 浅克隆（第三方源码须固定版本，
#       禁止 clone 默认分支随远端漂移；github.com 为公开源，走系统信任库 CA 校验，
#       不得关闭证书校验）
cd && git clone --branch v2.1.0 --depth 1 https://github.com/ceph/ceph-deploy.git
# （若改经内网自签名镜像站获取源码：先把内部 CA 装入系统信任库，保持 TLS 校验开启
#   cp <internal-ca>.crt /etc/pki/ca-trust/source/anchors/
#   update-ca-trust extract
#   确需临时绕过时仅用命令级 -c 参数且仅限内网自签名地址——仅单条命令生效，不得写入
#   全局 .gitconfig，并在台账记录；公开源 github.com 一律走系统信任库 CA）
# 步骤3 安装前核对版本：rev-parse HEAD 摘要与供应链台账记录的 v2.1.0 commit 摘要比对，
#       一致并记入台账后方可安装；不一致立即中止，重新核查下载渠道
cd /root/ceph-deploy && git rev-parse HEAD
# 步骤4 在隔离虚拟环境中安装（避免 root 直接源码安装污染系统 Python；两 OS 统一口径）
python3 -m venv /root/ceph-deploy-venv
/root/ceph-deploy-venv/bin/pip install /root/ceph-deploy
# 步骤5 查看版本（并将 venv bin 目录追加到 PATH，供后文各节 ceph-deploy 直接调用）
echo 'export PATH=/root/ceph-deploy-venv/bin:$PATH' >> /root/.bashrc
/root/ceph-deploy-venv/bin/ceph-deploy --version
```

修正说明（TLS 与供应链）：源文档建议通过修改 `~/.gitconfig` 的 `[http]` 段全局关闭 git 证书
校验来绕过 clone 失败，该做法会全局关闭证书校验，扩大中间人攻击暴露面，本仓不采用。改为：
优先把内部 CA 装入系统信任库（update-ca-trust）；确需临时绕过时用命令级
`git -c http.sslVerify=false`（仅单条命令生效、仅限内网自签名镜像源地址，公开源一律走系统
信任库 CA），执行后在台账记录。供应链加固：源文档直接 clone 默认分支（版本随远端漂移）且
以 root 直装系统 Python，本仓改为固定 `v2.1.0` tag `--depth 1` 克隆，安装前 `git rev-parse
HEAD` 摘要与供应链台账记录比对（不一致立即中止），并在隔离虚拟环境（venv）内 pip 安装，
避免污染系统 Python。

## 2. 系统配置（§6.3.2）

### 2.1 （可选）关闭 SMMU

部署 Ceph 前需要关闭 SMMU，以防其对 Ceph 性能造成影响。步骤：进入 BIOS（参见《TaiShan 服务器
BIOS 参数参考（鲲鹏920处理器）》中"进入 BIOS 界面"相关内容）→ 进入 "Advanced->MISC Config"
配置页面 → 将 "Support Smmu" 设置为 "Disabled"，按 F10 键保存退出。

### 2.2 关闭防火墙（所有服务端节点和客户端节点依次执行）

```bash
systemctl stop firewalld
systemctl disable firewalld
systemctl status firewalld
```

### 2.3 配置主机名

```bash
# 步骤1 配置永久静态主机名：服务端 ceph1~ceph3，客户端 client1~client3（其余节点以此类推）
hostnamectl --static set-hostname ceph1
hostnamectl --static set-hostname client1
# 步骤2 修改域名解析文件（集群内各服务端与客户端节点的 /etc/hosts 中添加）
vi /etc/hosts
# 添加如下内容：
# 198.51.100.166   ceph1
# 198.51.100.167   ceph2
# 198.51.100.168   ceph3
# 198.51.100.160   client1
# 198.51.100.161   client2
# 198.51.100.162   client3
```

验证：如登录 ceph1 检查与 ceph2 的网络连通，`ping ceph2` 返回
`64 bytes from ceph2 (198.51.100.167): icmp_seq=1 ttl=64 time=0.175 ms` 且
`3 packets transmitted, 3 received, 0% packet loss, time 2043ms` 即为域名解析配置成功。

### 2.4 配置 NTP

```bash
# 步骤1 安装 NTP 服务（所有服务端节点和客户端节点）
yum -y install ntp ntpdate
# 所有服务端节点和客户端节点备份旧配置
cd /etc && mv ntp.conf ntp.conf.bak
# 以 ceph1 为 NTP 服务端节点，在 ceph1 新建 /etc/ntp.conf，新增如下内容作为 NTP 服务端
# restrict 127.0.0.1
# restrict ::1
# restrict 198.51.100.0 mask 255.255.255.0
# server 127.127.1.0
# fudge 127.127.1.0 stratum 8
# 说明：restrict 198.51.100.0 mask 255.255.255.0 是 ceph1 的网段与掩码
# 在 ceph2、ceph3 及所有客户端节点新建 /etc/ntp.conf，新增：server 198.51.100.166
# 步骤2 在 ceph1 节点启动 NTP 服务，并检查服务状态为 Active
systemctl start ntpd
systemctl enable ntpd
systemctl status ntpd
# 在除 ceph1 的所有节点强制同步 server（ceph1）时间（成功示例：
# 15 Oct 14:43:39 ntpdate[32973]: adjust time server 198.51.100.166 offset -0.000034 sec）
ntpdate ceph1
# 在除 ceph1 的所有节点写入硬件时钟，避免重启后失效
hwclock -w
# 在除 ceph1 的所有节点安装并启动 crontab 工具
yum install -y crontabs
chkconfig crond on
systemctl start crond
crontab -e
# 添加以下内容，每隔 10 分钟自动与 ceph1 同步时间（此 IP 为 ceph1 节点 Public Network 的 IP）
# */10 * * * * /usr/sbin/ntpdate 198.51.100.166
```

注意：ceph1 节点启动 ntp 进程 5 分钟后，再在其他节点进行时间同步，否则会报错
（no server suitable for synchronization found）。

### 2.5 配置免密登录

需配置 ceph1 节点对所有服务端/客户端节点的免密（包括 ceph1 本身），及 client1 节点对所有
服务端/客户端节点的免密（包括 client1 本身）：

```bash
# 步骤1 在 ceph1 节点生成公钥，并发放到各个服务端/客户端节点（按 Enter 使用默认配置）
ssh-keygen -t rsa
for i in {1..3}; do ssh-copy-id ceph$i; done
for i in {1..3}; do ssh-copy-id client$i; done
# 步骤2 在 client1 节点生成公钥，并发放到各个服务端/客户端节点
ssh-keygen -t rsa
for i in {1..3}; do ssh-copy-id ceph$i; done
for i in {1..3}; do ssh-copy-id client$i; done
```

### 2.6 设置 SELINUX 模式（所有节点）

```bash
# 步骤1 临时修改（下次重启自动失效）
setenforce permissive
# 步骤2 永久设置（下次重启自动生效）：修改 /etc/selinux/config 为 SELINUX=permissive
vi /etc/selinux/config
```

### 2.7 修改 umask（仅 openEuler，CentOS 跳过）

openEuler 的安全性很高，需要修改 umask 为 0022 才可以正常安装 Ceph：

```bash
# 步骤1 在所有服务端节点打开 /etc/bashrc，将最后一行修改为 "umask 0022"
vi /etc/bashrc
# 步骤2 使配置更改生效
source /etc/bashrc
# 步骤3 确认配置是否生效
umask
```

## 3. 部署前环境分析与配置（§6.3.3）

### 3.1 节点本地源同步

各节点同步主节点本地源，将本地源配置拷贝至各个主机与客户机：

```bash
# 拷贝配置给主机ceph1、ceph2、ceph3
scp /etc/yum.repos.d/* root@ceph1:/etc/yum.repos.d/
scp /etc/yum.repos.d/* root@ceph2:/etc/yum.repos.d/
scp /etc/yum.repos.d/* root@ceph3:/etc/yum.repos.d/
# 拷贝配置给客户机client1、client2、client3
scp /etc/yum.repos.d/* root@client1:/etc/yum.repos.d/
scp /etc/yum.repos.d/* root@client2:/etc/yum.repos.d/
scp /etc/yum.repos.d/* root@client3:/etc/yum.repos.d/
```

### 3.2 配置 MON 节点（仅需在主节点 ceph1 上执行）

```bash
# 步骤1 新建 "/root/ceph-mycluster/" 集群目录，利用 ceph-deploy 生成节点配置文件
mkdir /root/ceph-mycluster && cd /root/ceph-mycluster
ceph-deploy new ceph1 ceph2 ceph3
# 步骤2 配置 MON 节点：修改 /root/ceph-mycluster/ceph.conf 为如下内容
vi /root/ceph-mycluster/ceph.conf
```

```
[global]
fsid = 0207ddea-2150-4509-860d-365e87420b3e
mon_initial_members = ceph1, ceph2, ceph3
mon_host = 198.51.100.166,198.51.100.167,198.51.100.168
auth_cluster_required = cephx
auth_service_required = cephx
auth_client_required = cephx
public_network = 198.51.100.0/24
cluster_network = 203.0.113.0/24
```

危险开关说明：基础 ceph.conf 不预置 mon_allow_pool_delete（源文档模板曾预置 true，删池权限长期
敞开会放大误删风险，本仓不采用）；仅删除存储池（§6.3）执行前临时开启该开关，删除完成后立即
复位为 false（危险开关用后即复位）。

说明：配置节点命令以及使用 ceph-deploy 操作 ceph 集群时，建议在专门的集群目录下进行，而不是在
`/etc/ceph/` 目录下进行（如在 `/root/ceph-mycluster/` 目录下执行），以避免不必要的冲突。修改
ceph.conf 内容是为了将内部集群间的网络与外部访问的网络隔离：网络 203.0.113.0 用于内部存储集群
之间的数据同步（仅在存储节点间使用），网络 198.51.100.0 用于存储节点与计算节点的数据交互。

核对说明：`mon_initial_members` 与 `mon_host` 必须逐项与 §0 集群规划及 §2.3 的 /etc/hosts
表核对——三个 MON 节点为 ceph1~ceph3，对应 Public 地址依次为 198.51.100.166、198.51.100.167、
198.51.100.168，与 mon_initial_members 的 ceph1, ceph2, ceph3 一一对应；不得混入客户端地址
（client1 的 Public 地址为 198.51.100.160），也不得遗漏任一 MON 节点；该 mon_host 列表须与
后文 §5.2 挂载示例中的 MON IP:6789 列表一致。填写后逐项核对确认无误再继续。

### 3.3 配置 MGR 节点

若非 CentOS 7.6 环境，请跳过该步骤。若基于 CentOS 7.6 环境，请在各服务端节点执行：

```bash
pip install pecan==1.4.2 parse==1.20.0
```

### 3.4 配置 OSD 节点

步骤 1 在所有准备部署 OSD 的服务端节点逐机核准待清盘设备清单（写盘前必做，核准结果供
§4.4 OSD 创建与 §7.2 快速部署脚本共用；禁止对盘符区间如 sda~sdl 做范围式直清）：

```bash
# 1a. 查看块设备拓扑与文件系统类型，列出候选数据盘与 NVMe DB/WAL 盘
# （偏离标注：源文档原文为 lsblk 的 -T 参数形式，因 CentOS 7.6.1810（util-linux 2.23.2）与
#   openEuler 20.03 SP1（util-linux 2.35.2）不支持/需参数，改为三 OS 通用 -f 口径）
lsblk -f
# 回显中 "/dev/sda"、"/dev/sdc" 和 "/dev/sdi" 为系统盘，可用的 HDD 磁盘有 "/dev/sdd"、
# "/dev/sde" 等，可用的 SSD 磁盘有 "/dev/nvme0n1" 等。
# 1b. 用稳定设备 ID 对照候选盘（sda/sdb 盘符重启后可能漂移，写盘一律使用 by-id 路径）
ls -l /dev/disk/by-id/
# 1c. 从候选中排除根盘：反查 / 所在设备，其所在物理盘整块排除（lsblk 反查该盘全部分区）
df -l /
# 1d. 排除挂载盘：当前所有挂载点中的设备整块排除
findmnt
# 1e. 排除现役 OSD：已建 OSD 的数据盘与 DB/WAL 分区整块排除
ceph-volume lvm list 2>/dev/null || ceph-disk list 2>/dev/null || lsblk
# （BlueStore OSD 数据分区特征：lsblk 中出现 ceph--<fsid>-osd--block--* 的 lvm 卷，
#   或分区标注 osd.wal/osd.db/OSD 结构特征；旧集群残盘按残留分区清理流程处理后再入清单）
# 1f. 汇总生成该节点的核准清单 approved_disks.txt（每行一个 /dev/disk/by-id/ 路径），
#     打印清单并与 §0 集群规划（每节点 12 块 4TB 数据盘 + 1 块 NVMe 盘）逐项核对
cat approved_disks.txt
```

修正说明（清盘核准）：源文档对三台节点以 for 循环直接 `disk zap /dev/sda~/dev/sdl`，盘符
直清不可逆且可能命中根盘/挂载盘/现役 OSD，本仓不采用。改为逐机核准稳定设备 ID 清单
（lsblk + /dev/disk/by-id 对照），排除根盘、挂载盘与现役 OSD 后按核准清单逐盘执行；
首次写盘前必须打印清单并与 §0 集群规划核对。

说明：有一些硬盘可能是以前 Ceph 集群里的数据盘或者曾经安装过操作系统，这些硬盘上可能存在
未清理的分区，需先按残留数据分区清理流程处理。

步骤 2 划分 OSD 分区（以下操作需在部署 OSD 的所有节点均执行，重置磁盘并按 NVMe 盘空间
规划 WAL 分区和 DB 分区；目标盘取自本节点 §3.4 步骤 1 核准清单，禁止按盘符写盘）：

```bash
# WAL_NVME/DB_NVME 取自本节点核准清单 approved_disks.txt（每行一个 /dev/disk/by-id/ 路径）
# 中的 NVMe 盘条目；源文档示例盘符 /dev/nvme0n1、/dev/nvme1n1 仅为示意，实际执行必须替换为
# 核准清单中的 /dev/disk/by-id/ 路径（APPROVED 变量直接取清单条目，禁止按盘符写盘）
WAL_NVME=$(grep -m1 'nvme' approved_disks.txt)
DB_NVME=$(grep 'nvme' approved_disks.txt | sed -n '2p')
parted "$WAL_NVME" mklabel gpt
parted "$DB_NVME" mklabel gpt
# "$WAL_NVME" 划分 1 个 60GB WAL 分区、"$DB_NVME" 划分 1 个 180GB DB 分区
# （演示用两块 NVMe，生产建议同盘划出两个分区）
parted "$WAL_NVME" mkpart primary ext4 1 60G
parted "$DB_NVME" mkpart primary ext4 1 180G
```

说明：这里不需要 update，parted 命令是实时生效的，分区命令完成后已经将分区表数据写入了硬盘；
/etc/fstab 是系统自动挂载分区的配置，目的是让系统重启后能够自动将某个分区挂载到指定挂载点。
实际生产环境建议将 DB 分区与 WAL 分区放置在一个盘上以提高性能，此处仅用作演示指导。
快速划分多个 WAL 和 DB 分区参见 §7.1。划分出的 WAL/DB 分区同样以 /dev/disk/by-id/ 形式引用
与记录（分区路径形如 `<NVMe 盘 by-id>-part1`），多 OSD 批量部署时汇总为
approved_nvme_parts_<node>.txt 分区清单（见 §7.2），供 §4.4 分支 B 与 §7.2 消费；分区写盘同样
禁止按盘符。

## 4. 安装 Ceph 与部署节点（§6.4）

### 4.1 安装 Ceph 软件

使用 Ceph 14.2.8 版本的用户，参见《Ceph 14.2.8 编译指南（CentOS 7.6&openEuler 20.03&openEuler
22.03）》（即 skill `ceph-build` 的 `build-guide.md`）对 Ceph 进行安装；使用 Ceph 16.2.7 版本
的用户，参见《Ceph 16.2.7 安装指南（openEuler 22.03）》（即姊妹 skill `ceph-install` 的
`installation-guide.md`，同批姊妹 PR，同批交付）对 Ceph 进行安装。

### 4.2 部署 MON 节点（仅需在主节点 ceph1 执行，需先完成 §3.2）

使用 ceph-deploy 操作 ceph 集群时，需在 `/root/ceph-mycluster/` 目录下执行，否则会报错。

```bash
# 步骤1 初始化监视器并收集密钥
ceph-deploy mon create-initial
# 步骤2 将 "ceph.client.admin.keyring" 拷贝到各服务端节点上（client1~client3 不下发
#       client.admin，改由步骤4 签发限权身份）
ceph-deploy --overwrite-conf admin ceph1 ceph2 ceph3
# 步骤3 查看是否配置成功
ceph -s
```

`ceph -s` 返回应包含：`cluster: id: 0207ddea-2150-4509-860d-365e87420b3e / health: HEALTH_OK /
services: mon: 3 daemons, quorum ceph1,ceph2,ceph3 (age 25h)`。

步骤 4 为 CephFS 客户端签发限权身份并只分发该身份的 keyring（不得将 client.admin 下发到
业务客户端——admin 拥有全集群管理权限，下发到业务机会扩大凭据暴露面）：

```bash
# 模板（<cfs-app> 为业务自定义用户名，<cfs 数据池> 按后文 §5.1 创建的数据池名代入，
# 权限按需收敛）
#   ceph auth get-or-create client.<cfs-app> mon 'allow r' mds 'allow rw' osd 'allow rw pool=<cfs 数据池>'
# 以本文环境的文件系统 cephfs、数据池 fs_data、业务用户名 cfs-app 为例：
ceph auth get-or-create client.cfs-app mon 'allow r' mds 'allow rw' osd 'allow rw pool=fs_data'
# keyring 落盘为 600 权限文件（umask 077 保证新建文件不放大权限；keyring 不入库、不进 git、
# 不落日志、不在命令行打印密钥）
umask 077; ceph auth print_key client.cfs-app > /etc/ceph/ceph.client.cfs-app.keyring
# 只分发该限权身份的 keyring 到三台 CephFS 客户端（client1~client3），并统一收紧为 600 权限
for i in {1..3}; do scp /etc/ceph/ceph.client.cfs-app.keyring root@client$i:/etc/ceph/; done
for i in {1..3}; do ssh client$i "chmod 600 /etc/ceph/ceph.client.cfs-app.keyring"; done
# 客户端以该身份验证集群连通（--name + --keyfile 方式读取 keyring 文件，不在命令行出现密钥）
ceph -s --name client.cfs-app --keyfile /etc/ceph/ceph.client.cfs-app.keyring
```

说明：`ceph auth print_key` 落盘的是裸密钥文件，供 `--keyfile`（ceph 命令）与挂载参数
`secretfile=`（见 §5.2）读取；如需标准 keyring 格式文件（含 [client.cfs-app] 段），可改用
`ceph auth get-or-create client.cfs-app ... -o /etc/ceph/ceph.client.cfs-app.keyring` 生成，
两种文件任选其一并保持各客户端一致。密钥文件不入库、不进 git、不落日志；确需在命令行使用
密钥串时用 `read -s` 读入环境变量，避免密钥落入 shell history。

说明：若出现警告 "mon is allowing insecure global_id reclaim"，这是由于启用了允许使用不安全的
全局 ID 回收配置导致，启用该配置可能导致数据不一致，可执行以下命令以不使用不安全的全局 ID
回收，确保数据一致性，消除该警告：

```bash
ceph config set mon auth_allow_insecure_global_id_reclaim false
```

### 4.3 部署 MGR 节点（仅需在主节点 ceph1 执行）

使用 CentOS 操作系统的用户需先完成 §3.3 的 MGR 节点配置。

```bash
# 步骤1 部署 MGR 节点
ceph-deploy mgr create ceph1 ceph2 ceph3
# 步骤2 查看 MGR 是否部署成功
ceph -s
```

`ceph -s` 回显 `mgr: ceph1(active, since 2d), standbys: ceph2, ceph3` 代表部署成功。

### 4.4 部署 OSD 节点（仅需在主节点 ceph1 执行，需先完成 §3.4）

步骤 1 部署 OSD 前先选定布局——分支 A（普通）与分支 B（外置 DB/WAL）互斥，只能选其一并贯穿
本次部署：不得先按分支 A 建完 OSD 再执行分支 B 的 zap（分支 B 首条 `disk zap` 会把分支 A
刚建好的 OSD 数据盘连同数据一并清掉）；两分支的目标盘一律取自 §3.4 步骤 1 逐机核准的
approved_disks.txt 设备清单。示例命令一律以核准清单 by-id 变量形式书写（源文档示例盘符
/dev/sdb、/dev/nvme0n1p1、/dev/nvme1n1p1 仅为示意，实际执行必须替换为核准清单中的
/dev/disk/by-id/ 路径，禁止按盘符写盘）。

**分支 A（普通 OSD：数据盘直建，不外置 DB/WAL）**

```bash
cd /root/ceph-mycluster/
# 目标盘取自 §3.4 步骤 1 核准清单 approved_disks.txt（每行一个 /dev/disk/by-id/ 路径）；
# 下例从清单取第一块非 NVMe 数据盘（实际执行以核准清单为准，禁止按盘符写盘）
APPROVED_DISK=$(grep -m1 -v 'nvme' approved_disks.txt)
ceph-deploy disk zap ceph1 "${APPROVED_DISK}"
ceph-deploy osd create ceph1 --data "${APPROVED_DISK}"
```

**分支 B（外置 DB/WAL：DB/WAL 分区已在 §3.4 步骤 2 划分，zap 与 osd create 同一事务连续执行）**

```bash
cd /root/ceph-mycluster/
# 目标盘与 WAL/DB 分区均取自 §3.4 核准口径（禁止按盘符写盘）：APPROVED_DISK 为核准清单
# approved_disks.txt 中的数据盘；WAL/DB 分区为清单中两块 NVMe 盘按 §3.4 步骤 2 划分出的
# 分区，路径形如 <NVMe 盘 by-id>-part1（/dev/disk/by-id/ 形式）
APPROVED_DISK=$(grep -m1 -v 'nvme' approved_disks.txt)
WAL_PART=$(grep -m1 'nvme' approved_disks.txt)-part1
DB_PART=$(grep 'nvme' approved_disks.txt | sed -n '2p')-part1
ceph-deploy disk zap ceph1 "${APPROVED_DISK}"
ceph-deploy disk zap ceph1 "${WAL_PART}"
ceph-deploy disk zap ceph1 "${DB_PART}"
ceph-deploy osd create ceph1 --data "${APPROVED_DISK}" --block-wal "${WAL_PART}" --block-db "${DB_PART}"
```

（可选）添加 DB 与 WAL 分区以提高写入性能：DB 和 WAL 通常部署在 NVMe SSD 上以提高写入性能；
如需配置，选择分支 B 并在添加核准清单数据盘的基础上额外设置 --block-wal 与 --block-db 参数。
如果没有配置 NVMe SSD 或者直接使用 NVMe SSD 作为数据盘，选择分支 A，则不需要 --block-db 和
--block-wal，只需要 --data 指定数据盘即可。

步骤 2 创建成功后查看是否正常，即 OSD 是否都为 up：

```bash
ceph -s
```

`ceph -s` 回显应包含 `osd: 1 osds: 1 up(since 2s), 1 in(since 2s)`。

说明：`ceph-deploy disk zap` 用来清除目标磁盘，${node} 是节点的 hostname；
`ceph-deploy osd create` 用来生成 osd 节点，--data 选项后面是作为数据盘的设备，--block-db
选项后面是 DB 分区，--block-wal 选项后面是 WAL 分区。部署多个 OSD 节点参见 §7.2。

### 4.5 部署 MDS 节点

MDS（Metadata Server）即元数据 Server，主要负责 Ceph FS 集群中文件和目录的管理。

```bash
# 步骤1 创建 MDS（在 ceph1 节点执行）
cd /root/ceph-mycluster/
ceph-deploy mds create ceph1 ceph2 ceph3
# 步骤2 在部署了 mds 的节点上查看是否成功创建 MDS 进程
ps -ef | grep ceph-mds | grep -v grep
# 若有类似如下字段输出则说明 MDS 进程启动成功
# root  ...  ceph-mds ...
```

## 5. 部署文件存储功能（§6.5）

### 5.1 创建存储池和文件系统

CephFS 需要使用两个 Pool 分别存储数据和元数据：fs_data 与 fs_metadata。

```bash
# 步骤1 在 ceph1 上创建存储池
cd /root/ceph-mycluster/
ceph osd pool create fs_data 1024 1024
ceph osd pool create fs_metadata 128 128
# 步骤2 基于上述存储池创建新的文件系统（cephfs 为文件系统名称，fs_metadata 和 fs_data 为
# 存储池名称，注意先后顺序：元数据池在前，数据池在后）
ceph fs new cephfs fs_metadata fs_data
# 步骤3 （可选）存储池使能 zlib 压缩
ceph osd pool set fs_data compression_algorithm zlib
ceph osd pool set fs_data compression_mode force
ceph osd pool set fs_data compression_required_ratio .99
# 步骤4 查看创建的 CephFS
ceph fs ls
```

说明：`ceph osd pool create fs_data 1024 1024` 中最后的两个数字分别为存储池的 pg_num 和
pgp_num。Ceph 官方文档给出单池基线公式 Total PGs = (OSD 数 × 100) / 副本数（EC 模式为 K+M，
即数据块+校验块之和，三副本 = 3，EC4+2 = 6），多池时按各池数据量占比分摊、
各向上取整 2 的整数次幂。口径说明：源指南按「整个集群所有存储池的 pg 数量之和」表述该公式，
官方原文为单池基线公式，二者数值结论等价。例如集群 3 台服务器、每台 12 个 OSD 共 36 个 OSD，
三副本模式下按公式 (36 × 100) / 3 = 1200，本文取 fs_data 的 pg 数量为 1024、fs_metadata 的
pg 数量为 128，合计约 1200，与源指南结论一致（fs_data 存放的数据量远大于 fs_metadata，故按
数据量占比分摊更多 pg，各向上取整为 2 的整数次幂）。命令中的 1024/128 为按 §0 示例集群
（36 OSD、三副本）代入公式重算的结果，现场部署须按实际 OSD 数与副本数重新计算，不得照抄
示例值。

### 5.2 客户机挂载文件系统

```bash
# 步骤1 使用 §4.2 步骤4 签发并分发到各客户端的限权身份 keyring 文件
# （/etc/ceph/ceph.client.cfs-app.keyring，600 权限；不得使用 client.admin，也不得将密钥串
#   写在命令行——避免密钥落入 shell history）
# 步骤2 创建文件系统挂载点（在所有 Client 节点执行）
mkdir /mnt/cephfs
# 步骤3 在所有 Client 节点执行挂载（MON 默认端口号为 6789；-o 指定限权身份名与密钥文件，
#   密钥从 keyring 文件读取，不在命令行出现）
mount -t ceph 198.51.100.166:6789,198.51.100.167:6789,198.51.100.168:6789:/ /mnt/cephfs \
  -o name=client.cfs-app,secretfile=/etc/ceph/ceph.client.cfs-app.keyring,sync
# 步骤4 在所有 Client 节点检查是否挂载成功，文件系统类型是否为 ceph
stat -f /mnt/cephfs
```

说明：挂载 MON IP 列表须与 §3.2 ceph.conf 的 mon_host 及 /etc/hosts 中 ceph1~ceph3 的
Public 地址（198.51.100.166/167/168）一致。如个别场景确需以密钥串挂载（不推荐），用
`read -s` 将密钥读入变量后引用，避免密钥明文落入 shell history；规范做法始终是从 keyring
文件读取（secretfile=）。

## 6. 回退与清理（本仓口径补充，与 SKILL.md 输出契约「回退步骤」一一对应）

> 本节为破坏性操作的回退路径，按部署逆序执行；每一步执行前必须与用户二次确认目标集群与
> 变更窗口。系统配置类还原动作（防火墙恢复、SELINUX 原值、repo 备份还原）随各部署步骤
> 的备份记录执行。

### 6.1 客户端卸载与 /etc/fstab 清理

```bash
# 在所有 CephFS 客户端（client1~client3）执行：先停止业务读写，再卸载
umount /mnt/cephfs
# 若设备忙，先确认无进程占用（lsof /mnt/cephfs 或 fuser -vm /mnt/cephfs）后再卸载
# 检查 /etc/fstab 中 CephFS 挂载行（若配置了自动挂载）并逐行删除
grep ceph /etc/fstab
vi /etc/fstab        # 删除对应的 CephFS 挂载行后保存
# 确认卸载成功（无输出即已卸载）
findmnt /mnt/cephfs
```

### 6.2 删除 CephFS 文件系统（ceph fs rm）

```bash
# 前置1：已按 §6.1 umount 全部客户端
# 前置2：fail 掉该文件系统的 MDS（按官方口径逐字可执行；<who> 为 active/standby MDS 的
#        daemon 名或 GID，用 ceph mds stat 查询，如 3 up: ceph1.a ceph2.b ceph3.c 时
#        逐个执行 ceph mds fail ceph1.a 等；或直接执行 ceph fs fail cephfs 一并停止全部 rank）
ceph mds stat
ceph mds fail <who>
# （等价批量方式：ceph fs fail cephfs）
# 删除文件系统（cephfs 为 §5.1 创建的文件系统名；rm 必须带显式确认参数）
ceph fs rm cephfs --yes-i-really-mean-it
# 验证：不再显示 cephfs
ceph fs ls
```

### 6.3 删除存储池（ceph osd pool delete）

```bash
# 前置：基础 ceph.conf 不预置该危险开关（§3.2）；删池前临时开启，删除完成后立即复位
ceph config set mon mon_allow_pool_delete true
ceph osd pool delete fs_data fs_data --yes-i-really-mean-it
ceph osd pool delete fs_metadata fs_metadata --yes-i-really-mean-it
# 删除完成立即复位安全开关，避免误删池长期可用（危险开关用后即复位）
ceph config set mon mon_allow_pool_delete false
# 验证：不再显示 fs_data/fs_metadata
ceph osd pool ls
```

### 6.4 销毁 MON（ceph-deploy mon destroy）

```bash
# 在 /root/ceph-mycluster/ 目录下执行；按官方 mon destroy 口径逐台销毁
# （逐台执行并确认 quorum 状态后再销毁下一台）
cd /root/ceph-mycluster/
ceph-deploy mon destroy ceph1
ceph-deploy mon destroy ceph2
ceph-deploy mon destroy ceph3
# OSD 下线与数据盘清理：先确认 OSD 已 out 并移除（ceph osd purge），数据盘 zap 复按 §3.4
# 核准清单复核目标盘后执行；MGR/MDS 进程用 ceph-deploy mgr destroy/mds destroy 下线
```

## 7. 常见问题（§6.6）

### 7.1 如何快速划分多个 WAL 与 DB 分区

目标盘取自 §3.4 步骤 1 核准清单 approved_disks.txt 中的 /dev/disk/by-id/ NVMe 路径（禁止按
盘符写盘）；如果有多块 NVMe SSD 或 SATA/SAS 接口 SSD，将脚本中 device 变量替换为核准清单中
对应 NVMe 盘的 by-id 路径即可。根据 NVMe 盘空间规划：NVMe 盘划分为 12 个 60GB 分区、
12 个 180GB 分区，分别对应 WAL 分区、DB 分区。

```bash
# 1. 创建一个 partition.sh 脚本
vi partition.sh
# 2. 添加如下内容
#!/bin/bash
# device 取 §3.4 步骤 1 核准清单 approved_disks.txt 中的 /dev/disk/by-id/ NVMe 路径
# （下例取清单中第一块 NVMe 盘；禁止按盘符写盘）
device=$(grep -m1 'nvme' approved_disks.txt)
parted $device mklabel gpt -s
for j in `seq 1 12`
do
((b = $(( $j * 8 ))))
((a = $(( $b - 8 ))))
((c = $(( $b - 6 ))))
str="%"
echo $a
echo $b
echo $c
parted $device mkpart primary ${a}${str} ${c}${str}
parted $device mkpart primary ${c}${str} ${b}${str}
done
# 3. 保存退出后执行快速划分脚本
bash partition.sh
```

### 7.2 如何快速部署多个 OSD 节点

使用脚本前，需完成 OSD 节点的配置（§3.4）。此处假设用户有 3 台服务器，每台服务器有 12 块
硬盘，每个硬盘都要部署 OSD。

**禁止范围式直清**：不得对盘符区间（如 sda~sdl）做 for 循环直清——disk zap 不可逆，可能命中
根盘、挂载盘或现役 OSD。必须先按 §3.4 步骤 1 逐机核准稳定设备 ID 清单（排除根盘、挂载盘与
现役 OSD，写盘用 /dev/disk/by-id 路径），首次写盘前打印各节点清单并与 §0 集群规划核对通过，
再按核准清单逐盘执行。

```bash
# 1. 按 §3.4 步骤 1 逐机核准并生成每台节点的核准清单（approved_disks_<node>.txt 数据盘
#    清单 + approved_nvme_parts_<node>.txt WAL/DB 分区清单，每行一个 /dev/disk/by-id/
#    路径，分区清单奇数行 WAL、偶数行 DB），打印并与 §0 集群规划逐项核对：
cat approved_disks_ceph1.txt approved_disks_ceph2.txt approved_disks_ceph3.txt
cat approved_nvme_parts_ceph1.txt approved_nvme_parts_ceph2.txt approved_nvme_parts_ceph3.txt
# 2. 在 ceph1 上创建脚本 create_osd.sh，内容见下方脚本块（按核准清单逐盘执行；
#    本脚本为分支 B 外置 DB/WAL 布局，j/k 为分区清单中 WAL/DB 分区的行号；
#    采用分支 A 布局时去掉 --block-wal/--block-db 参数）
cd /root/ceph-mycluster/
vi /root/ceph-mycluster/create_osd.sh
# 3. 保存退出后在 ceph1 上运行
bash create_osd.sh
```

脚本内容（approved_disks_${node}.txt 与 approved_nvme_parts_${node}.txt 为 §3.4 步骤 1/步骤 2
核准通过并打印核对的设备与分区清单文件，均为每行一个 /dev/disk/by-id/ 路径）：

```bash
#!/bin/bash
set -eu
for node in ceph1 ceph2 ceph3
do
    j=1
    k=2
    # 只消费核准清单，清单外一概不写盘：数据盘每行一个 /dev/disk/by-id/ 稳定设备路径，
    # WAL/DB 分区取自 approved_nvme_parts_${node}.txt（每行一个 /dev/disk/by-id/nvme-...-partN
    # 稳定分区路径，奇数行 WAL、偶数行 DB）
    while read -r disk
    do
        [ -n "$disk" ] || continue
        wal_part=$(sed -n "${j}p" approved_nvme_parts_${node}.txt)
        db_part=$(sed -n "${k}p" approved_nvme_parts_${node}.txt)
        ceph-deploy disk zap ${node} "${disk}"
        ceph-deploy osd create ${node} --data "${disk}" --block-wal "${wal_part}" --block-db "${db_part}"
        ((j=${j}+2))
        ((k=${k}+2))
        sleep 3
    done < approved_disks_${node}.txt
done
```

说明：此脚本内容只适用于当前硬件配置，其他硬件配置可参考此脚本修改（每台节点的核准清单与
分区清单须按 §3.4 步骤 1/步骤 2 独立核准；--data 为数据盘设备，--block-db 为 DB 分区，
--block-wal 为 WAL 分区，均取 /dev/disk/by-id/ 路径；写盘走 by-id 可避免盘符漂移清错盘）。

## 8. 故障排除（§6.7，共三则）

### 8.1 执行 ceph -s 提示 no module named xxx

- 现象：执行 `ceph -s` 提示 no module named xxx 错误。
- 解决：根据报错安装对应的依赖包：`pip2 install xxx`。

### 8.2 执行 ceph -s 提示 auth: unable to find a keyring

- 现象：提示 Auth: unable to find a keyring。
- 根因：当以非 root 用户运行 ceph 集群时，往往会出现当前用户读权限不够而导致 ceph 读取文件
  报错的问题。
- 解决（以 ceph-adm 用户为例）：安装 acl 软件包，签发一次性只读身份 client.ro-status 并对该
  身份 keyring 授读权限（对齐 skill `ceph-build` §4.2 口径；不得对 client.admin keyring 授
  持久 ACL——admin 拥有全集群管理权限，排障凭据按需最小化并事后回收）：

```bash
# 安装acl软件包（偏离标注：源文档此处为上游 Ubuntu 排障示例（apt），RHEL 系改用 yum；
# 提示符按本文集群规划节点写法）
root@ceph1:~# sudo yum install -y acl
# 管理员上下文签发一次性只读身份 client.ro-status（仅 mon allow r），keyring 落盘 600 权限
root@ceph1:~# umask 077; ceph auth get-or-create client.ro-status mon 'allow r' --name client.admin --keyring /etc/ceph/ceph.client.admin.keyring -o /etc/ceph/ceph.client.ro-status.keyring
# 设置ceph-adm对只读身份 keyring 的读权限（admin keyring 不授 ACL）
root@ceph1:~# sudo setfacl -m u:ceph-adm:r /etc/ceph/ceph.client.ro-status.keyring
# 排障结束后回收：移除 ACL 授权并删除一次性只读身份
root@ceph1:~# sudo setfacl -x u:ceph-adm /etc/ceph/ceph.client.ro-status.keyring
root@ceph1:~# ceph auth rm client.ro-status
```

### 8.3 提示 Error initializing cluster client: InvalidArgumentError('RADOS invalid argument (error calling conf_read_file)')

- 现象：执行 `ceph -s` 提示 Error initializing cluster client: InvalidArgumentError('RADOS
  invalid argument (error calling conf_read_file)')。
- 解决：确认在正确的用户和正确的 ceph 集群目录下运行 `ceph -s`（如 root 用户 + 集群目录
  /root/ceph-mycluster/），其他 ceph 集群相关操作命令同理。
