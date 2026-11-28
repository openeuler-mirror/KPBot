# Ceph 对象存储部署指南（ceph-deploy 路径 & cephadm 容器化路径）

> 来源：《鲲鹏 BoostKit 分布式存储使能套件 Ceph 编译&部署&调优&测试指南》（文档版本 07，
> 2025-06-30）第 5 章「Ceph 对象存储 部署指南（CentOS 7.6&openEuler 20.03&openEuler 22.03&Ubuntu 22.04）」
> 全文蒸馏。路径一 §5.1 ceph-deploy 对象存储部署（CentOS 7.6 / openEuler 20.03 / openEuler 22.03，
> Ceph 14.2.8/16.2.7）：§5.1.2 环境要求 / §5.1.3 配置部署环境（镜像源与 ceph-deploy 适配、系统配置、
> 部署前环境分析与配置）/ §5.1.4 安装 Ceph 与部署节点 / §5.1.5 部署对象存储功能 / §5.1.6 常见问题 /
> §5.1.7 故障排除。路径二 §5.2 cephadm 容器化部署（Ubuntu 22.04 / openEuler 22.03，Ceph 17.2.8）：
> §5.2.2 环境要求 / §5.2.3 配置部署环境（容器环境安装、cephadm 安装、手动制作 ceph 容器镜像、
> 部署前环境分析与配置）/ §5.2.4 安装 Ceph 与部署节点 / §5.2.5 部署对象存储功能 / §5.2.6 卸载 Ceph /
> §5.2.7 常见问题。本文件按 SOP 步骤组织并标注适用 OS/版本，命令与配置片段逐字保留
> （PDF 换行断词已复原为完整命令；原文档截图类回显以文字说明代替）。

## 0. 两大部署路径选择对照表

| 维度 | 路径一：ceph-deploy 部署（§5.1） | 路径二：cephadm 容器化部署（§5.2） |
|---|---|---|
| Ceph 版本 | 14.2.8 Nautilus / 16.2.7 Pacific | 17.2.8 (quincy) |
| OS | CentOS 7.6.1810 / openEuler 20.03 LTS SP1 / openEuler 22.03 LTS SP4 | Ubuntu 22.04（ubuntu-22.04.5-live-server-arm64）/ openEuler 22.03 LTS SP4（物理机 + 容器镜像） |
| 容器技术 | 无需容器 | Ubuntu 用 Docker（24 及以上版本）；openEuler 用 Podman（Ceph 17.2.x 需 2.0 及以上，本文以 3.4.4 为例） |
| 部署工具 | ceph-deploy 2.0.1 / 2.1.0（CentOS yum 安装；openEuler git clone 源码安装） | cephadm（Ubuntu jammy：基础源 17.1.0-0ubuntu3 / updates 源 17.2.9-0ubuntu0.22.04.x，均与 17.2.8 不同版，需 17.2.8 时改用官方 quincy 脚本或按 §2.4.5 步骤 7 修改 DEFAULT_IMAGE，见 §2.3 说明；openEuler wget 官方 quincy 脚本） |
| Ceph 软件来源 | 本地源编译安装（14.2.8）/ 系统镜像源（16.2.7） | Ubuntu 直接用官方镜像（cephadm 自动下载）；openEuler 推荐自制 Ceph 容器镜像推本地仓库 |
| RGW 网关前端 | civetweb（3 节点 3 实例，端口 10001~10003，网关名 bucket1~bucket3） | beast（`rgw_frontends = beast port=10000` + `rgw_thread_pool_size = 1024`，按 label 批量部署；beast 前端不支持 num_threads 选项，线程池由 rgw_thread_pool_size 控制） |
| 配置文件入口 | `/root/ceph-mycluster/ceph.conf`（ceph-deploy 生成后修改） | `/root/ceph-mycluster/ceph.conf`（bootstrap 时 `--config ceph.conf` 传入） |
| 适用判断 | 已有 ceph-deploy 运维习惯 / 需要部署 14.2.8、16.2.7 版本 | 新建 17.2.8 集群 / 容器化运维（Ubuntu 优先官方镜像，openEuler 优先自制镜像） |

选择路径一 → 阅读第 1 章（环境要求至故障排除）；选择路径二 → 阅读第 2 章（环境要求至常见问题）。
路径一无需容器环境；路径二必须先就绪容器环境（Docker/Podman）与 cephadm。

# 路径一：ceph-deploy 对象存储部署（CentOS 7.6 / openEuler 20.03 / openEuler 22.03）

## 1.1 环境要求（§5.1.2）

硬件要求（表 5-2，鲲鹏服务器）：

| 项 | 规格 |
|---|---|
| 服务器名称 | 鲲鹏服务器 |
| 处理器 | 鲲鹏920 5220处理器 |
| 核数 | 2*32核 |
| 主频 | 2600MHz |
| 内存大小 | 8*16GB |
| 内存频率 | 2933MHz |
| 网卡 | IN200网卡4*25GE |
| 硬盘 | 系统盘：RAID1（2*960GB SATA SSD）；数据盘：RAID模式下使能JBOD（12*4TB SATA HDD）；NVMe SSD 缓存盘：1*ES3000 V5 3.2TB NVMe SSD |
| RAID控制卡 | Avago SAS 3508 |

说明：由于安装过程中需要在线安装 Ceph 软件包及依赖包，请确保服务器可以接入互联网。

软件要求（表 5-3）：

| 项 | 版本 |
|---|---|
| OS | CentOS Linux release 7.6.1810；openEuler 20.03 LTS SP1（Kernel: 4.19.90-2012.4.0.0053.oe1）；openEuler 22.03 LTS SP4（Kernel: 5.10.0-216.0.0.115.oe2203sp4 / 5.10.0-216.0.0.115.oe2203sp4.aarch64） |
| Ceph | 14.2.8 Nautilus / 16.2.7 Pacific |
| ceph-deploy | 2.0.1 / 2.1.0 |

说明：本文档以 Ceph 14.2.8 版本和 Ceph 16.2.7 版本进行说明，其他版本安装也可参考本文档；
全新安装操作系统建议使用最小化安装。

集群环境规划：集群由 Ceph 客户端和 Ceph 服务端组成，客户端为 client1~client3，服务端为
ceph1~ceph3。服务端部署 IP（表 5-4）：ceph1 = 管理 192.0.2.166 / Public 198.51.100.166 /
Cluster 203.0.113.166；ceph2 = 192.0.2.167 / 198.51.100.167 / 203.0.113.167；
ceph3 = 192.0.2.168 / 198.51.100.168 / 203.0.113.168。客户端部署 IP（表 5-5）：
client1 = 192.0.2.160 / 198.51.100.160；client2 = 192.0.2.161 / 198.51.100.161；
client3 = 192.0.2.162 / 198.51.100.162。示例网段采用 RFC 5737 文档保留地址
（192.0.2.0/24 管理、198.51.100.0/24 Public、203.0.113.0/24 Cluster），按现场规划替换；
后文 hosts、NTP、ceph.conf、cephadm 等命令中的示例 IP 同样为文档保留地址。

- 管理IP地址：用于远程SSH机器管理配置使用的IP地址。
- 内部集群IP地址（Cluster Network）：用于集群内部节点之间同步数据的IP地址，选取任意一个25GE网口配置即可。
- 外部访问IP地址（Public Network）：存储节点供其他节点访问的IP地址，选取任意一个25GE网口配置即可。
- 客户端当做压力机，需保证客户端业务口IP地址与集群的外部访问IP地址在同一个网段，建议选用25GE网口进行配置。
- Ceph 客户端和 Ceph 服务端都需安装 Ceph 软件（编译安装参见 skill `ceph-build`）。

硬盘划分：Ceph 14.2.8 采用了 BlueStore 作为后端存储引擎（BlueStore 自 Luminous 版本起即为默认
存储后端，14.2.8 使用 BlueStore），没有了 Jewel 版本的 Journal 盘分区划分，而是变成 DB 分区
（元数据分区）和 WAL 分区。每个 Ceph 节点配置 12 块 4TB 数据盘和 1 块 3.2TB 的 NVMe 盘：每个
4TB 数据盘作为一个 OSD 的数据分区，单个 NVMe 盘作为 12 个 OSD 的 DB、WAL 分区。一般 WAL 分区
大于 10GB 就足够使用，Ceph 官方文档建议每个 DB 分区不小于每个数据盘容量的 4%。本 SOP 统一按
主口径划分：每块 NVMe 盘划分 12 个 60GB 分区作为 WAL、12 个 180GB 分区作为 DB（与 §1.4.3、
§1.7.1 口径一致）；源指南 14.2.8 章节另有 12 个 20GB WAL + 12 个 45GB DB 的示例划分，属于源
指南示例差异，本 SOP 不采用、两套口径不得混用。源指南中"每个 NVMe 盘划分 3 个 700GB 分区作为
RGW 对象存储元数据存储池分区"的做法本 SOP 未使用（对象存储元数据池直接建立在既有 OSD 之上，
见 §1.6.1），如需预留请自行承接。

| NVMe盘 | DB分区 | WAL分区 |
|---|---|---|
| 3.2TB | 12*180GB | 12*60GB |

## 1.2 镜像源配置与 ceph-deploy 适配（§5.1.3.1）

### 1.2.1 安装 liboath（按 OS 在集群内所有服务端和客户端节点执行）

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

openEuler 20.03 可参考 skill `ceph-build` 的 `build-guide.md` §2.2「编译 liboath-devel 软件包」手动编译。

### 1.2.2 配置 Ceph 镜像源（集群内所有服务端和客户端节点）

在线源（Ceph 16.2.7，建议 openEuler 22.03 用户使用）：由于操作系统差异，Ceph 14.2.8 版本不建议
使用在线源进行安装，建议使用本地源编译后进行安装。步骤：备份 openEuler.repo 后新建
`/etc/yum.repos.d/openEuler.repo`，添加 openEuler-22.03-LTS-SP4 的 [OS]/[everything]/[EPOL]/
[debuginfo]/[source]/[update]/[update-source] 七个 repo 段（完整内容与姊妹 skill
`ceph-install` 的 `installation-guide.md` §1.2 在线源完全一致，baseurl 均指向
http://repo.openeuler.org/openEuler-22.03-LTS-SP4/...，gpgcheck=1，gpgkey 指向对应
RPM-GPG-KEY-openEuler）。

```bash
# 步骤1 备份原有 openEuler.repo 文件
cp /etc/yum.repos.d/openEuler.repo /etc/yum.repos.d/openEuler.repo.bak
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

### 1.2.3 安装 ceph-deploy（仅需在主节点 ceph1 执行）

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
# 步骤2 在 ceph1 获取 ceph-deploy 源码（https 证书校验失败时：优先把内部 CA 装入系统信任库
# （cp <internal-ca>.crt /etc/pki/ca-trust/source/anchors/ && update-ca-trust extract）后正常
# git clone；确需临时绕过时用命令级一次性参数（仅单条命令生效，禁止写全局/系统 gitconfig，
# 见 SKILL.md TLS 铁律），豁免操作记入台账：
cd && git clone https://github.com/ceph/ceph-deploy.git
# 步骤3 安装 ceph-deploy（按 Python 版本选择命令）
cd /root/ceph-deploy
# openEuler 20.03 用户：
python setup.py install
# openEuler 22.03 用户：
python3 setup.py install
# 步骤4 查看版本
ceph-deploy --version
```

## 1.3 系统配置（§5.1.3.2）

### 1.3.1 （可选）关闭 SMMU

部署 Ceph 前需要关闭 SMMU，以防其对 Ceph 性能造成影响。步骤：进入 BIOS（参见《TaiShan 服务器
BIOS 参数参考（鲲鹏920处理器）》中"进入 BIOS 界面"相关内容）→ 进入 "Advanced->MISC Config"
配置页面 → 将 "Support Smmu" 设置为 "Disabled"，按 F10 键保存退出。

### 1.3.2 关闭防火墙（所有服务端和客户端节点依次执行）

```bash
systemctl stop firewalld
systemctl disable firewalld
systemctl status firewalld
```

### 1.3.3 配置主机名

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
`3 packets transmitted, 3 received, 0% packet loss` 即为域名解析配置成功。

### 1.3.4 配置 NTP

```bash
# 步骤1 安装 NTP 服务（所有服务端和客户端节点）
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

### 1.3.5 配置免密登录

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

### 1.3.6 设置 SELINUX 模式（所有节点）

```bash
# 步骤1 临时修改（下次重启自动失效）
setenforce permissive
# 步骤2 永久设置（下次重启自动生效）：修改 /etc/selinux/config 为 SELINUX=permissive
vi /etc/selinux/config
```

### 1.3.7 修改 umask（仅 openEuler，CentOS 跳过）

openEuler 的安全性很高，需要修改 umask 为 0022 才可以正常安装 Ceph：

```bash
# 步骤1 在所有服务端节点打开 /etc/bashrc，将最后一行修改为 "umask 0022"
vi /etc/bashrc
# 步骤2 使配置更改生效
source /etc/bashrc
# 步骤3 确认配置是否生效
umask
```

## 1.4 部署前环境分析与配置（§5.1.3.3）

### 1.4.1 配置 MON 节点（仅需在主节点 ceph1 执行）

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
[mon]
mon_allow_pool_delete = true
```

说明：配置节点命令以及使用 ceph-deploy 操作 ceph 集群时，建议在专门的集群目录下进行，而不是在
`/etc/ceph/` 目录下进行（如在 `/root/ceph-mycluster/` 目录下执行），以避免不必要的冲突。修改
ceph.conf 内容是为了将内部集群间的网络与外部访问的网络隔离：网络 203.0.113.0 用于内部存储集群
之间的数据同步（仅在存储节点间使用），网络 198.51.100.0 用于存储节点与计算节点的数据交互。

### 1.4.2 配置 MGR 节点

若非 CentOS 7.6 环境，请跳过该步骤。若基于 CentOS 7.6 环境，请在各服务端节点执行：

```bash
pip install pecan==1.4.2 parse==1.20.0
```

### 1.4.3 配置 OSD 节点

```bash
# 步骤1 在所有准备部署 OSD 的服务端节点确认可用磁盘
lsblk -T
# 回显中 "/dev/sda"、"/dev/sdc" 和 "/dev/sdi" 为系统盘，可用的 HDD 磁盘有 "/dev/sdd"、
# "/dev/sde" 等，可用的 SSD 磁盘有 "/dev/nvme0n1" 等。
# 说明：有一些硬盘可能是以前 Ceph 集群里的数据盘或者曾经安装过操作系统，这些硬盘上可能存在
# 未清理的分区，需先按残留数据分区清理流程处理（见 §2.9.1）。
# 步骤2 划分 OSD 分区（以下操作需在部署 OSD 的所有节点均执行，重置磁盘并按 NVMe 盘空间
# 规划 WAL 分区和 DB 分区）
parted /dev/nvme0n1 mklabel gpt
parted /dev/nvme1n1 mklabel gpt
# "/dev/nvme0n1" 划分出 1 个 60GB 分区和 1 个 180GB 分区，分别对应 WAL 分区、DB 分区
parted /dev/nvme0n1 mkpart primary ext4 1 60G
parted /dev/nvme1n1 mkpart primary ext4 1 180G
```

说明：这里不需要 update，parted 命令是实时生效的，分区命令完成后已经将分区表数据写入了硬盘；
/etc/fstab 是系统自动挂载分区的配置，目的是让系统重启后能够自动将某个分区挂载到指定挂载点。
实际生产环境建议将 DB 分区与 WAL 分区放置在一个盘上以提高性能，此处仅用作演示指导。
快速划分多个 WAL 和 DB 分区参见 §1.7.1。

### 1.4.4 配置 RGW 节点

```bash
# 步骤1.1 在所有服务端节点安装 RGW 组件（ceph-radosgw 需根据对应版本下载）
# ceph 14.2.8 的版本
yum -y install ceph-radosgw-14.2.8
# ceph 16.2.7 的版本
yum -y install ceph-radosgw
# 步骤1.2 修改 ceph.conf：在 ceph1 的 ceph.conf 中添加 RGW 实例的端口配置。本示例在每个节点
# 上创建 1 个 RGW 实例，3 个节点共 3 个 RGW 实例，网关端口分别为 10001~10003，网关名称分别为
# bucket1~bucket3
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


[mon]
mon_allow_pool_delete = true

[client.rgw.bucket1]
rgw_frontends = civetweb port=10001
log file = /var/log/ceph/client.rgw.bucket1.log
[client.rgw.bucket2]
rgw_frontends = civetweb port=10002
log file = /var/log/ceph/client.rgw.bucket2.log
[client.rgw.bucket3]
rgw_frontends = civetweb port=10003
log file = /var/log/ceph/client.rgw.bucket3.log
```

```bash
# 步骤1.3 在所有服务端节点上同步配置文件（在 ceph1 上执行）
ceph-deploy --overwrite-conf config push ceph1 ceph2 ceph3
# 步骤2 在所有部署 RGW 的节点上取消 Proxy 配置
unset http_proxy
unset https_proxy
```

## 1.5 安装 Ceph 与部署节点（§5.1.4）

### 1.5.1 安装 Ceph 软件

使用 Ceph 14.2.8 版本的用户，参见《Ceph 14.2.8 编译指南（CentOS 7.6&openEuler 20.03&openEuler
22.03）》（即 skill `ceph-build` 的 `build-guide.md`）对 Ceph 进行安装；使用 Ceph 16.2.7 版本的
用户，参见《Ceph 16.2.7 安装指南（openEuler 22.03）》（即姊妹 skill `ceph-install` 的
`installation-guide.md`，已合入，PR #444）对 Ceph 进行安装。

### 1.5.2 部署 MON 节点（仅需在主节点 ceph1 执行，需先完成 §1.4.1）

使用 ceph-deploy 操作 ceph 集群时，需在 `/root/ceph-mycluster/` 目录下执行，否则会报错。

```bash
# 步骤1 初始化监视器并收集密钥
ceph-deploy mon create-initial
# 步骤2 将 "ceph.client.admin.keyring" 仅拷贝到各服务端节点（管理动作只在管理节点/
# client.admin 持有者执行；业务客户端 client1~client3 不复制 client.admin keyring，
# 业务侧仅签发对象账户所需身份：RGW S3 用户即 radosgw-admin user create 产物（§1.6.2），
# 无需集群 CephX 身份；如需 RADOS 访问用限权 client.<app>：mon 'allow r'
# osd 'allow rw pool=<rgw 池>'，见 §1.9）
ceph-deploy --overwrite-conf admin ceph1 ceph2 ceph3
# 步骤3 查看是否配置成功
ceph -s
```

`ceph -s` 返回应包含：`cluster: id: 0207ddea-2150-4509-860d-365e87420b3e / health: HEALTH_OK /
services: mon: 3 daemons, quorum ceph1,ceph2,ceph3 (age 25h)`。

说明：若出现警告 "mon is allowing insecure global_id reclaim"，这是由于启用了允许使用不安全的
全局 ID 回收配置导致，启用该配置可能导致数据不一致，可执行以下命令以不使用不安全的全局 ID
回收，确保数据一致性，消除该警告：

```bash
ceph config set mon auth_allow_insecure_global_id_reclaim false
```

### 1.5.3 部署 MGR 节点（仅需在主节点 ceph1 执行）

使用 CentOS 操作系统的用户需先完成 §1.4.2 的 MGR 节点配置。

```bash
# 步骤1 部署 MGR 节点
ceph-deploy mgr create ceph1 ceph2 ceph3
# 步骤2 查看 MGR 是否部署成功
ceph -s
```

`ceph -s` 回显 `mgr: ceph1(active, since 2d), standbys: ceph2, ceph3` 代表部署成功。

### 1.5.4 部署 OSD 节点（仅需在主节点 ceph1 执行，需先完成 §1.4.3）

```bash
# 步骤1 根据磁盘情况部署 OSD 节点（此处以 ceph1 上的 /dev/sdb、/dev/nvme0n1 与 /dev/nvme1n1
# 为例说明；zap/写盘目标必须与本机核准清单一致，系统盘/挂载盘/现役 OSD 不得出现在目标中，
# 逐机核准方法见 §1.7.2 清盘护栏）
cd /root/ceph-mycluster/
ceph-deploy disk zap ceph1 /dev/sdb
ceph-deploy osd create ceph1 --data /dev/sdb
# （可选）添加 DB 与 WAL 分区以提高写入性能。DB 和 WAL 通常部署在 NVMe SSD 上以提高写入性能。
# 如需配置，要在添加数据盘 /dev/sdb 的基础上，额外设置 --block-wal 与 --block-db 参数
cd /root/ceph-mycluster/
ceph-deploy disk zap ceph1 /dev/sdb
ceph-deploy disk zap ceph1 /dev/nvme0n1p1
ceph-deploy disk zap ceph1 /dev/nvme1n1p1
ceph-deploy osd create ceph1 --data /dev/sdb --block-wal /dev/nvme0n1p1 --block-db /dev/nvme1n1p1
# 步骤2 创建成功后查看是否正常，即 OSD 是否都为 up
ceph -s
```

`ceph -s` 回显应包含 `osd: 1 osds: 1 up(since 2s), 1 in(since 2s)`。

说明：`ceph-deploy disk zap` 用来清除目标磁盘，${node} 是节点的 hostname；
`ceph-deploy osd create` 用来生成 osd 节点，--data 选项后面是作为数据盘的设备，--block-db
选项后面是 DB 分区，--block-wal 选项后面是 WAL 分区。如果没有配置 NVMe SSD 或者直接使用
NVMe SSD 作为数据盘，则不需要 --block-db 和 --block-wal，只需要 --data 指定数据盘即可。
部署多个 OSD 节点参见 §1.7.2。

### 1.5.5 部署 RGW 节点（在主节点 ceph1 上执行，配置见 §1.4.4）

```bash
# 步骤1 根据 ceph.conf 的配置，创建 RGW 实例
cd /root/ceph-mycluster
ceph-deploy rgw create ceph1:bucket1
ceph-deploy rgw create ceph2:bucket2
ceph-deploy rgw create ceph3:bucket3
# 步骤2 上述命令执行成功后，查看 3 个 RGW 进程是否在线
ceph -s
```

`ceph -s` 回显应包含 `rgw: 3 daemons active (3 hosts, 1 zones)`。

## 1.6 部署对象存储功能（§5.1.5）

### 1.6.1 创建存储池（仅需在主节点 ceph1 上执行）

对象存储需要用到多个存储池，把元数据等数据量较小的存储池建立在 SSD 上可提高性能：在 SSD 上
创建对象存储元数据存储池，在 HDD 上创建对象存储数据存储池。Ceph 的存储池默认使用三副本模式，
对象存储数据存储池有时会设置为 EC 纠删码模式以节省存储空间。

**副本模式存储池：**

```bash
# 步骤1 设置 crush 规则
# 1. 查看 crush 类（服务器上既有基于 SSD 的 OSD 又有基于 HDD 的 OSD 时，会显示两种类型：
#    ["hdd", "ssd"]）
ceph osd crush class ls
# 2. 为 SSD class 和 HDD class 分别创建 crush rule（如没有基于 SSD 的 OSD 可不进行 rule-ssd 设置）
ceph osd crush rule create-replicated rule-ssd default host ssd
ceph osd crush rule create-replicated rule-hdd default host hdd
# 3. 查看 crush rule（replicated_rule 是集群默认使用的 crush rule，为三副本模式，数据会按比例
#    存到所有存储设备上；rule-ssd / rule-hdd 则分别只把数据存储到 SSD / HDD 上）
ceph osd crush rule ls
# 步骤2 在 ceph1 上创建 Data Pool 和 Index Pool
cd /root/ceph-mycluster/
ceph osd pool create default.rgw.buckets.data 32 32
ceph osd pool create default.rgw.buckets.index 8 8
ceph osd pool application enable default.rgw.buckets.data rgw
ceph osd pool application enable default.rgw.buckets.index rgw
# 步骤3 修改所有存储池的 crush 规则
ceph osd pool set default.rgw.buckets.data crush_rule rule-hdd
# 步骤4 使用 curl 或者 web 节点登录验证（注意 IP 地址与端口的对应关系，以 IP 198.51.100.166、
# 端口 10001 为例，浏览器输入 "198.51.100.166:10001"，出现 RGW 信息字样表示 RGW 创建成功；
# 注：源文档此处为 198.51.100.164:10001，疑似笔误，已按 §1.1 规划表修正为 ceph1 的
# Public Network IP 198.51.100.166）
```

说明：`ceph osd pool create <pool> <pg_num> <pgp_num>` 最后的两个数字分别为 pg_num 和 pgp_num。
官方口径：单个存储池的 PG 基线 Total PGs = (OSD 数 × 100) / pool size，pool size 对副本模式而言
是副本数，对 EC 模式而言是数据块+校验块之和（三副本 = 3，EC4+2 = 6）；该口径与 skill
`ceph-parameter-tuning` 的 PG 分布公式一致（Total PGs = (Total_number_of_OSD × 100) /
max_replication_count，结果向上取整到最近的 2 的整数次幂）。源指南按"整个集群所有存储池的
pg 数量之和约为 (OSD数量 * 100) / 数据冗余因数"口径表述，两种口径数值等价。多个存储池并存时，
按各池数据量占比分摊该基线，各池结果分别向上取整到 2 的整数次幂。一般建议 pg 数取 2 的整数次幂。
default.rgw.buckets.data 存放的数据量远大于其他存储池，因此成比例分配更多 pg：可将
default.rgw.buckets.data 的 pg 数量取 1024，default.rgw.buckets.index 的 pg 数量取 128 或者
256。步骤 2 中 data=32、index=8 为最小可运行口径的示例，生产环境按本节推荐值
（data 1024、index 128/256）设置。

**EC 模式存储池：**

```bash
# 步骤1 设置 crush 规则（前提是环境上 osd 部署在 ssd 磁盘）
ceph osd crush class ls
ceph osd crush rule create-replicated rule-ssd default host ssd
ceph osd crush rule ls
# 步骤2 创建 EC profile（以 EC 4+2 为例，创建名为 myprofile 的 EC profile）
ceph osd erasure-code-profile set myprofile k=4 m=2 crush-failure-domain=osd crush-device-class=ssd
# 步骤3 创建 Data Pool 和 Index Pool（在 ceph1 上执行）
ceph osd pool create default.rgw.buckets.data 2048 2048 erasure myprofile
ceph osd pool create default.rgw.buckets.index 256 256
ceph osd pool application enable default.rgw.buckets.data rgw
ceph osd pool application enable default.rgw.buckets.index rgw
# 步骤4 修改所有存储池的 crush 规则
ceph osd pool set default.rgw.buckets.data crush_rule rule-ssd
```

说明：`ceph osd erasure-code-profile set` 中 k 为数据块数量，m 为校验块数量；最小故障域一般
设置为 host，若 host 数量小于 k+m，则需要将故障域改为 osd，否则会因无法找到足够多的 host 而
报错；crush-device-class=ssd 表示 crush rule 建立在 ssd 上。EC 模式下只需修改
default.rgw.buckets.data 为 EC pool，其他 pool 仍使用默认 3 副本模式。

### 1.6.2 创建 RGW 账户（仅需在主节点 ceph1 上执行）

```bash
# 步骤1 在 ceph1 上创建 RGW 账户
radosgw-admin user create --uid="admin" --display-name="admin user"
# 步骤2 创建账户完成后查询账户信息
radosgw-admin user info --uid=admin
```

### 1.6.3 使能 RGW 压缩（仅需在主节点 ceph1 上执行）

创建完 RGW 账户后就可以使用该账户访问 RGW。若要使能 RGW 数据压缩功能，需要创建压缩数据用的
存储池，新增放置策略并指明压缩算法。

```bash
# 创建压缩存储池（区别于普通的非压缩存储池，分别作为接下来新建放置策略中的 data_pool、
# index_pool 与 data_extra_pool；default.rgw.buckets.data-compress 也可创建 EC 模式的
# 压缩数据存储池，方法同 §1.6.1 EC 存储池）
ceph osd pool create default.rgw.buckets.data-compress 4096 4096
ceph osd pool create default.rgw.buckets.index-compress 256 256
ceph osd pool create default.rgw.buckets.non-ec-compress 64 64
ceph osd pool application enable default.rgw.buckets.data-compress rgw
ceph osd pool application enable default.rgw.buckets.index-compress rgw
ceph osd pool application enable default.rgw.buckets.non-ec-compress rgw
# 新增放置策略：Ceph 对象存储集群有一个默认的放置策略 default-placement，
# 此处创建一个新的放置策略 compress-placement 应用于 RGW 压缩
# 步骤1 创建新的放置策略 compress-placement
radosgw-admin zonegroup placement add --rgw-zonegroup=default --placement-id=compress-placement
# 步骤2 填入 compress-placement 相关信息，包括该放置策略的存储池及压缩算法
radosgw-admin zone placement add --rgw-zone=default --placement-id=compress-placement --index_pool=default.rgw.buckets.index-compress --data_pool=default.rgw.buckets.data-compress --data_extra_pool=default.rgw.buckets.non-ec-compress --compression=zlib
# 说明：--compression=zlib 代表使用的压缩算法是 zlib，可选的压缩算法还有 snappy、LZ4 等
# 使能放置策略
# 步骤1 导出用户元数据（以存入 user.json 文件为例）
radosgw-admin metadata get user:admin > user.json
# 步骤2 修改元数据 user.json，将 "default_placement" 的值改为新增的放置策略 "compress-placement"
vi user.json
# 步骤3 在 ceph1 执行如下命令将修改后的用户元数据导入
radosgw-admin metadata put user:admin < user.json
# 步骤4 在所有服务端节点上重启 radosgw 进程
systemctl restart ceph-radosgw.target
# 步骤5 在 ceph1 检查用户的放置策略是否生效：default_placement 的值为 "compress-placement"
# 即表示放置策略生效
radosgw-admin user info --uid="admin"
```

## 1.7 常见问题（§5.1.6）

### 1.7.1 如何快速划分多个 WAL 与 DB 分区

此处以 /dev/nvme0n1 为例说明；如果有多块 NVMe SSD 或 SATA/SAS 接口 SSD，只需将脚本中的
"/dev/nvme0n1" 盘符替换为对应盘符即可。根据 NVMe 盘空间规划：NVMe 盘划分为 12 个 60GB 分区、
12 个 180GB 分区，分别对应 WAL 分区、DB 分区。

```bash
# 1. 创建一个 partition.sh 脚本
vi partition.sh
# 2. 添加如下内容
#!/bin/bash
device=/dev/nvme0n1
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

### 1.7.2 如何快速部署多个 OSD 节点

使用脚本前，需完成 OSD 节点的配置（§1.4.3）。此处假设用户有 3 台服务器，每台服务器有 12 块
硬盘，每个硬盘都要部署 OSD。

**清盘护栏（禁止范围式直清）**：§1.4.3 已标明本环境部分盘符（如 /dev/sda、/dev/sdc、/dev/sdi）
是系统盘，范围式循环（如 {a..l} 遍历 sda~sdl）会把系统盘一并清掉。执行任何写盘操作前，必须在
每台目标节点上逐机核准稳定设备 ID 清单（勿以一台的输出代表其余节点）：

```bash
# 逐机核准（每台节点分别执行）
lsblk -o NAME,SIZE,TYPE,MOUNTPOINT
ls -l /dev/disk/by-id
# 排除项核对：
df -l /                        # 根分区所在盘必须排除
findmnt -rn -o SOURCE,TARGET   # 所有已挂载盘必须排除
ceph-volume lvm list           # 现役 OSD 及含 BlueStore/LVM 特征的盘必须排除
```

将核准结果整理为该机的数据盘稳定设备 ID 清单（建议使用 /dev/disk/by-id 稳定路径，避免盘符
漂移），写盘前打印清单并与 §0 规划（节点角色与数据盘数量）逐项核对一致后方可执行：

```bash
# 1. 在 ceph1 上创建脚本 create_osd.sh（DATA_DISKS 逐机填入本机核准的数据盘清单；
#    全程不使用盘符范围式循环，系统盘/挂载盘/现役 OSD 不得写入清单）
cd /root/ceph-mycluster/
vi /root/ceph-mycluster/create_osd.sh
# 2. 添加以下内容（以单机清单为例，ceph2/ceph3 按各自核准清单分别生成脚本执行）
#!/bin/bash
node=ceph1
DATA_DISKS=(
  /dev/disk/by-id/<approved-disk-01>
  /dev/disk/by-id/<approved-disk-02>
  # ……逐项填入本机核准清单，数量与内容应与 §0 规划一致
)
j=1
k=2
echo "将清盘并创建 OSD 的设备清单（node=${node}）："
printf '%s\n' "${DATA_DISKS[@]}"   # 写盘前打印清单，人工与规划核对
read -p "确认上述清单与规划一致且不含系统盘/挂载盘/现役 OSD 后输入 yes：" ans
[ "${ans}" = "yes" ] || { echo "清单未通过核对，停止执行"; exit 1; }
for d in "${DATA_DISKS[@]}"
do
ceph-deploy disk zap ${node} ${d}
ceph-deploy osd create ${node} --data ${d} --block-wal /dev/nvme0n1p${j} --block-db /dev/nvme0n1p${k}
((j=${j}+2))
((k=${k}+2))
sleep 3
done
# 3. 保存退出后在 ceph1 上运行脚本
bash create_osd.sh
```

说明：此脚本内容只适用于当前硬件配置，其他硬件配置可参考此脚本修改（DATA_DISKS 为逐机核准的
稳定设备 ID 清单；--data 为数据盘设备，--block-db 为 DB 分区，--block-wal 为 WAL 分区）。

## 1.8 故障排除（§5.1.7，共三则）

### 1.8.1 执行 ceph -s 提示 no module named xxx

- 现象：执行 `ceph -s` 提示 no module named xxx 错误。
- 解决：根据报错安装对应的依赖包：`pip2 install xxx`。

### 1.8.2 执行 ceph -s 提示 auth: unable to find a keyring

- 现象：提示 Auth: unable to find a keyring。
- 根因：当以非 root 用户运行 ceph 集群时，往往会出现当前用户读权限不够而导致 ceph 读取文件
  报错的问题。
- 解决（以 ceph-adm 用户为例；不对 client.admin keyring 做 ACL 放权——client.admin 属管理侧
  身份，放权即把集群管理员能力交给业务用户，违反身份最小化铁律）：
  1. 优先以 sudo/root 执行管理侧查询：`sudo ceph -s`（管理动作只在管理节点/client.admin
     持有者执行）。
  2. 业务用户确需常态直查集群状态时，为其签发限权身份 client.<app>（mon 'allow r'，
     osd 'allow rw pool=<rgw 池>'），仅对该限权身份的 keyring 做 ACL 授权：

```bash
# 安装acl软件包
ceph-adm@mon:~/cluster$ sudo apt install acl -y
# 管理节点为业务用户签发限权身份（<app> 按实际应用名替换，<rgw 池> 按实际池名替换；
# 仅授予所需最小权限，不含 client.admin 能力）
ceph-adm@mon:~/cluster$ sudo ceph auth get-or-create client.<app> mon 'allow r' osd 'allow rw pool=<rgw 池>' -o /etc/ceph/ceph.client.<app>.keyring
# 仅对限权身份 keyring 授权当前用户读权限（禁止对 client.admin keyring 做 setfacl 放权）
ceph-adm@ceph1:~/cluster$ sudo setfacl -m u:ceph-adm:r /etc/ceph/ceph.client.<app>.keyring
# 验证：以限权身份读取集群状态
ceph-adm@ceph1:~/cluster$ ceph -s --name client.<app> -k /etc/ceph/ceph.client.<app>.keyring
```

### 1.8.3 提示 Error initializing cluster client: InvalidArgumentError('RADOS invalid argument (error calling conf_read_file)')

- 现象：执行 `ceph -s` 提示 Error initializing cluster client: InvalidArgumentError('RADOS
  invalid argument (error calling conf_read_file)')。
- 解决：确认在正确的用户和正确的 ceph 集群目录下运行 `ceph -s` 命令。如以 root 用户、集群目录
  /root/ceph-mycluster/ 为例，应以 root 用户进入 /root/ceph-mycluster/ 目录后再执行 ceph -s
  命令，其他 ceph 集群相关操作命令同理。

## 1.9 路径一回退（去 RGW 实例与存储池清理）

用于回退路径一部署的对象存储功能（仅回退 RGW 实例与对象存储存储池，不影响集群内其他业务）。
执行前与用户二次确认目标集群与回退范围，逐条记录操作证据入台账。

**第一步 停止/删除 RGW 实例**（在主节点 ceph1 的集群目录 /root/ceph-mycluster/ 下执行；
ceph-deploy 无 rgw 删除子命令——2.0.1/2.1.0 与 master 分支的 rgw.py 仅提供 add/create，
删除 RGW 实例只能走 systemctl + 数据目录清理）：

```bash
# 停止并禁用 RGW 实例后清理数据目录（<实例> 按实际替换，如 ceph1 节点为 bucket1，
# 实例名与部署时一致，见 §1.5.5）
systemctl stop ceph-radosgw@rgw.<实例>
systemctl disable ceph-radosgw@rgw.<实例>
rm -rf /var/lib/ceph/radosgw/ceph-rgw.<实例>
# 验证：ceph -s 回显中 rgw daemons 减少直至为 0
ceph -s
```

**第二步 删除对象存储存储池**（在主节点 ceph1 执行；前置条件 mon_allow_pool_delete=true，
§1.4.1 的 ceph.conf 已配置该开关——若现场未配置，按下方"开/复位两步"执行）：

```bash
# 前置（若现场未配置 mon_allow_pool_delete=true，第一步：开启）
ceph config set mon mon_allow_pool_delete true
# 第二步：删除存储池（破坏性操作，逐池确认；池名按 §1.6.1/§1.6.3 实际创建的名称替换）
ceph osd pool delete default.rgw.buckets.data default.rgw.buckets.data --yes-i-really-really-mean-it
ceph osd pool delete default.rgw.buckets.index default.rgw.buckets.index --yes-i-really-really-mean-it
# 压缩放置策略涉及的存储池（若执行过 §1.6.3）：
ceph osd pool delete default.rgw.buckets.data-compress default.rgw.buckets.data-compress --yes-i-really-really-mean-it
ceph osd pool delete default.rgw.buckets.index-compress default.rgw.buckets.index-compress --yes-i-really-really-mean-it
ceph osd pool delete default.rgw.buckets.non-ec-compress default.rgw.buckets.non-ec-compress --yes-i-really-really-mean-it
# 第三步：复位（现场未配置而临时开启时，删除完成后复位为 false）
ceph config set mon mon_allow_pool_delete false
# 验证：ceph df 回显中上述存储池已消失
ceph df
```

回退收尾：业务客户端如曾签发限权 client.<app> 身份（见 §1.5.2 步骤 2 说明），一并注销：
`ceph auth del client.<app>`。SKILL.md「输出契约」中的"路径一回退步骤"与本小节一一对应。

# 路径二：cephadm 容器化部署（Ubuntu 22.04 / openEuler 22.03，Ceph 17.2.8）

## 2.1 环境要求（§5.2.2）

硬件要求（表 5-8，鲲鹏服务器）：

| 项 | 规格 |
|---|---|
| 服务器名称 | 鲲鹏服务器 |
| 处理器 | 鲲鹏920处理器 |
| 核数 | 2*32核 |
| 主频 | 2600MHz |
| 内存大小 | 384GB |
| 内存频率 | 2933MHz |
| 网卡 | SP680网卡4*25GE |
| 硬盘 | 系统盘：RAID1（2*960GB SATA SSD）；数据盘：RAID模式下使能JBOD（36*16TB SATA HDD）；NVMe SSD 缓存盘：2*ES3000 V5 8TB NVMe SSD |
| RAID控制卡 | MegaRAID 9560-8i 4GB |

说明：由于安装过程中需要在线安装 Ceph 软件包及依赖包，请确保服务器可以接入互联网。

软件要求（表 5-9）：

| 项 | 版本 | 获取方式 |
|---|---|---|
| OS-Ubuntu | ubuntu-22.04.5-live-server-arm64 | 官方获取链接（见原文档） |
| OS-openEuler-物理机 | openEuler-22.03-LTS-SP4 | 官方获取链接（见原文档） |
| OS-openEuler-容器镜像 | openEuler-22.03-LTS-SP4 | 官方获取链接（见原文档） |
| Ceph | 17.2.8 (quincy) | 自制镜像（见 §2.4）；官方镜像由 cephadm 自动下载安装 |
| Cephadm | Ubuntu jammy：基础源 17.1.0-0ubuntu3 / updates 源 17.2.9-0ubuntu0.22.04.x（arm64，均与 17.2.8 不同版） | `apt install -y cephadm` 安装上述版本；需 17.2.8 时改用官方 quincy 脚本（见 §2.3）或按 §2.4.5 步骤 7 修改 DEFAULT_IMAGE |

说明：

- 本文档以 Ceph 17.2.8 版本进行说明，其他版本安装也可参考本文档。
- 全新安装操作系统建议使用最小化安装。
- 由于网关节点日志较多，网关节点根目录磁盘空间较小，容易导致网关节点异常，故在安装 OS 时需要
  确保系统根目录空间为 500GB 以上。
- 部署 Ceph 时，集群中客户端服务端的 OS 版本应保持一致。

集群环境规划：集群由 Ceph 客户端和 Ceph 服务端组成，客户端为 client1，服务端为 ceph1~ceph3。
服务端部署 IP（表 5-10）：ceph1 = 192.0.2.166 / 198.51.100.166 / 203.0.113.166；
ceph2 = 192.0.2.167 / 198.51.100.167 / 203.0.113.167；ceph3 = 192.0.2.168 / 198.51.100.168 /
203.0.113.168。客户端部署 IP（表 5-11）：client1 = 192.0.2.160 / 198.51.100.160。
管理 IP / Cluster Network / Public Network 的用途说明与路径一相同（§1.1）。

## 2.2 安装容器环境（§5.2.3.1.1）

### 2.2.1 安装 Podman（openEuler 系统，在 ceph1~ceph3 和 client1 节点安装）

Podman 为 Ceph 容器化部署依赖工具，不同 Podman 版本与 Ceph 版本之间存在兼容问题：Ceph 17.2.x
需要使用 Podman 2.0 及以上版本，openEuler-22.03-LTS-SP4 社区源里 Podman 版本为 3.4.+，本文以
Podman 3.4.4 为例。安装过程需要联网；如需使用代理，推荐使用临时环境变量 HTTP_PROXY 和
HTTPS_PROXY 配置代理：

```bash
export HTTP_PROXY=http://USERNAME:PASSWORD@192.0.2.1:3128
export HTTPS_PROXY=http://USERNAME:PASSWORD@192.0.2.1:3128
```

局域网组网时，Podman 拉取镜像时需要配置信任镜像仓库（见下文「配置 Podman 信任镜像仓库」）。

**方式一：yum 源直接安装**

```bash
# 步骤1 直接通过 yum 源安装
yum install podman
# 步骤2 安装 catatonit
yum install autoconf automake libtool -y
git clone https://github.com/openSUSE/catatonit.git
cd catatonit
./autogen.sh
./configure
make
make install
cp catatonit /usr/libexec/podman/catatonit
# 步骤3 检查 Podman 安装情况
podman -v
```

**方式二：手动编译 Podman（以 Podman 3.4.4 为例）**

```bash
# 步骤1 下载依赖工具
yum install rpmdevtools python3-pyyaml -y
# 步骤2 构建 Podman 3.4.4 的 RPM 包（https 校验失败时先按 TLS 铁律装内部 CA，装 CA 后正常 wget）
wget https://repo.openeuler.org/openEuler-22.03-LTS-SP2/source/Packages/podman-3.4.4-1.oe2203sp2.src.rpm
rpmdev-setuptree
rpm -ivUh podman-3.4.4-1.oe2203sp2.src.rpm
yum-builddep -y /root/rpmbuild/SPECS/podman.spec
rpmbuild -bb /root/rpmbuild/SPECS/podman.spec
# 步骤3 构建 crun 1.4.5 的 RPM 包
wget https://repo.openeuler.org/openEuler-22.03-LTS-SP2/source/Packages/crun-1.4.5-1.oe2203sp2.src.rpm
rpm -ivUh crun-1.4.5-1.oe2203sp2.src.rpm
yum-builddep -y /root/rpmbuild/SPECS/crun.spec
rpmbuild -bb /root/rpmbuild/SPECS/crun.spec
# 步骤4 构建 conmon 2.1.0 的 RPM 包
wget https://repo.openeuler.org/openEuler-22.03-LTS-SP2/source/Packages/conmon-2.1.0-1.oe2203sp2.src.rpm
rpm -ivUh conmon-2.1.0-1.oe2203sp2.src.rpm
yum-builddep -y /root/rpmbuild/SPECS/conmon.spec
rpmbuild -bb /root/rpmbuild/SPECS/conmon.spec
# 步骤5 安装所有的 RPM 包
cd /root/
yum install -y rpmbuild/RPMS/noarch/podman-docker-3.4.4-1.noarch.rpm rpmbuild/RPMS/aarch64/podman-remote-3.4.4-1.aarch64.rpm rpmbuild/RPMS/aarch64/podman-3.4.4-1.aarch64.rpm rpmbuild/RPMS/aarch64/crun-help-1.4.5-1.aarch64.rpm rpmbuild/RPMS/aarch64/crun-1.4.5-1.aarch64.rpm rpmbuild/RPMS/aarch64/conmon-2.1.0-1.aarch64.rpm rpmbuild/RPMS/aarch64/podman-help-3.4.4-1.aarch64.rpm rpmbuild/RPMS/aarch64/podman-gvproxy-3.4.4-1.aarch64.rpm rpmbuild/RPMS/aarch64/podman-plugins-3.4.4-1.aarch64.rpm
# 步骤6 安装 catatonit
git clone https://github.com/openSUSE/catatonit.git
cd catatonit
./autogen.sh
./configure
make
make install
cp catatonit /usr/libexec/podman/catatonit
# 步骤7 启动 Podman（回显 Active 为 active（running）则表示成功启动 Podman 服务）
systemctl daemon-reload
systemctl enable podman
systemctl start podman
systemctl status podman
```

**配置 Podman 信任镜像仓库**（局域网组网时需要；推荐只搭建一个镜像仓库，搭建步骤见 §2.4.5）：

```bash
# 步骤1 打开 /etc/containers/registries.conf 文件
vi /etc/containers/registries.conf
# 步骤2 按照如下内容调整配置（本地仓库须按 §2.4.5 步骤 4 启用 htpasswd 认证 + HTTPS）
```

```
unqualified-search-registries = ["[IP1]:5000", "[IP2]:5000", "quay.io"]
short-name-mode="enforcing"
[[registry]]
location = "[IP1]:5000"
[[registry]]
location = "[IP2]:5000"
```

说明：[IP1]、[IP2] 分别为局域网内镜像仓库 IP 地址，根据实际情况设置信任多个仓库或只信任 1 个。
示例地址采用 RFC 5737 文档保留段，按现场规划替换。本地仓库启用认证 + HTTPS 后不配置 insecure
明文通道：客户端把仓库证书的 CA 装入系统信任库（见下方"配置安全证书"），并先 `podman login
<仓库地址> -u deploy`（口令交互输入，账户见 §2.4.5 步骤 4）再拉取/推送。若现场受控内网确需
保留明文 HTTP 直连本地仓库（无认证无 TLS，不推荐），须额外配置 insecure 信任项并在台账记录
风险（凭据与镜像内容明文传输，可被同网段嗅探/篡改）与替代方案（认证 + HTTPS），并经安全评审。

**配置安全证书（可选）**：若下载 Podman 镜像时没有出现安全证书的问题则无需配置。安全证书异常时
会有类似如下报错：`/usr/bin/podman: stderr Error: parsing image configuration: Get "https://xxxxx.xxx": x509: certificate signed by unknown authority`。出现安全证书问题时，请联系当前
IT 网格管理部获取正确的安全证书：

```bash
# 步骤1 获取安全证书放到 root 目录下
# 步骤2 拷贝证书到 /usr/local/share/ca-certificates/ 目录下
mkdir -p /usr/local/share/ca-certificates/
cp HuaweiWebSecureInternetGatewayCA.crt /usr/local/share/ca-certificates/
# 步骤3 更新系统证书信息
update-ca-certificates
# 步骤4 重启 Podman 服务
systemctl daemon-reload
systemctl restart podman
```

### 2.2.2 安装 Docker（Ubuntu 系统，在 ceph1~ceph3 和 client1 节点安装）

```bash
# 步骤1 在所有节点安装 Docker（当前命令会安装 Ubuntu 官方仓库中适合当前系统版本的最新稳定
# Docker 版本，实际版本会略有差异，需确保 Docker 版本为 24 及以上版本即可）
apt install -y docker.io
# 步骤2 查看 Docker 的版本
docker -v
```

**配置 Docker 代理（可选）**：若网络可以访问外网则无需配置。

```bash
# 步骤1 为 Docker 创建配置文件夹
mkdir -p /etc/systemd/system/docker.service.d
# 步骤2 创建 HTTP/HTTPS 网络代理的配置文件
vi /etc/systemd/system/docker.service.d/http-proxy.conf
```

```
[Service]
Environment="HTTP_PROXY=http://proxy.example.com:8080/"
Environment="HTTPS_PROXY=http://proxy.example.com:8080/"
Environment="NO_PROXY=localhost,[IP],127.0.0.1,.example.com"
```

说明：将 proxy.example.com 替换成实际使用的代理 IP 地址；[IP] 替换成实际使用的本地镜像仓库
IP 地址（镜像仓库搭建见 §2.4.5）。

```bash
# 步骤3 重启 Docker 服务
systemctl daemon-reload
systemctl restart docker
```

**配置 Docker 信任镜像仓库**（局域网组网时需要）：

本地仓库须按 §2.4.5 步骤 4 启用 htpasswd 认证 + HTTPS；Docker 端不保留 insecure 类明文 HTTP
信任通道，改为信任仓库证书的 CA 并登录：

```bash
# 步骤1 装入本地仓库证书的 CA（证书/CA 路径按现场替换；装入后 Docker 对该仓库走 HTTPS 校验）
cp <registry-ca>.crt /usr/local/share/ca-certificates/registry-ca.crt
update-ca-certificates
# 步骤2 登录本地仓库（口令交互输入，不落文档/台账，账户见 §2.4.5 步骤 4）
docker login [IP]:5000 -u deploy
# 步骤3 重启 Docker 使 CA 生效
systemctl restart docker
```

说明：若现场受控内网确需保留明文 HTTP 直连本地仓库（无认证无 TLS，不推荐），才需要为 Docker
额外配置 insecure 信任项并在台账记录风险（凭据与镜像内容明文传输，可被同网段嗅探/篡改）与
替代方案（认证 + HTTPS），并经安全评审。

**配置安全证书（可选）**：步骤同 §2.2.1 Podman 的安全证书配置（拷贝证书到
/usr/local/share/ca-certificates/、update-ca-certificates、重启 Docker 服务）。自建镜像仓库
使用 Docker 时，装入本地仓库证书的 CA 即可正常访问（认证 + HTTPS，无需 insecure 明文通道）。

## 2.3 安装 cephadm（§5.2.3.1.2，在 ceph1~ceph3 节点物理机上安装，其他节点无需安装）

```bash
# 步骤1 在 ceph1~ceph3 节点安装 cephadm
# 1. 在 Ubuntu 系统安装 cephadm
apt-get update
apt install -y cephadm
# 2. 在 openEuler 系统安装 cephadm（https 校验失败时先按 TLS 铁律装内部 CA，装 CA 后正常
# wget；Ubuntu 亦可用同一官方 quincy 脚本路径安装指定版本 cephadm）
wget https://github.com/ceph/ceph/raw/quincy/src/cephadm/cephadm
cp -r cephadm /usr/sbin/
chmod 755 /usr/sbin/cephadm
# 步骤2 检查是否安装成功
cephadm -h
```

说明：物理机操作系统为 Ubuntu 时推荐使用 apt 安装 cephadm；物理机操作系统为 openEuler 时推荐
自制 Ceph 镜像，自制镜像会自动安装正确版本的 cephadm（见 §2.4）。

Ubuntu 22.04（jammy）apt 源中的 cephadm 实际版本：基础源（main）为 17.1.0-0ubuntu3，updates
源为 17.2.9-0ubuntu0.22.04.x（均以 apt 策略查询结果为准），两者均与本 SOP 目标 17.2.8 不同版；
不同版本 cephadm 内置的 DEFAULT_IMAGE / DEFAULT_CEPH_RELEASE 存在差异，bootstrap 拉起的容器
镜像 tag 可能与本 SOP 的 v17.2.8 不一致（可用 `cephadm version` 与
`grep DEFAULT_IMAGE /usr/sbin/cephadm` 核对实际值）。需要与本 SOP 一致的 17.2.8 时：apt 装完
后按 §2.4.5 步骤 7 修改 DEFAULT_IMAGE，或改用上方官方 quincy cephadm 脚本安装（Ubuntu/openEuler
通用）。

## 2.4 手动制作 ceph 容器镜像（§5.2.3.2，openEuler 路径）

说明：Ubuntu 系统推荐直接使用官方镜像部署，无需再手动制作 Ceph 容器镜像，可跳过本节；
openEuler 系统推荐自制镜像部署，本节以 openEuler 系统下使用 Podman 制作镜像为例进行说明。
为避免在真实集群中安装额外的软件，建议使用一台全新的集群外服务器制作容器镜像，该服务器应使用
与集群同样的硬件配置和操作系统（本文称为编译节点）。

### 2.4.1 制作编译容器和部署容器

```bash
# 步骤1 下载 openEuler-22.03-LTS-SP4 基础镜像（仅从认证渠道获取：官方 release 发布页/
# 官方镜像站 https 入口或内网可信 registry，禁用明文 HTTP 直链与来路不明镜像；https 校验
# 失败时先按 TLS 铁律装内部 CA）
wget https://repo.huaweicloud.com/openeuler/openEuler-22.03-LTS-SP4/docker_img/aarch64/openEuler-docker.aarch64.tar.xz
# 导入前核对可信摘要或签名（与官方发布页 checksum 逐字符对照，或用 skopeo/crane digest 对
# 官方源核对镜像 digest；核对通过才允许导入并启动，核对不过即停止并改从可信渠道重新获取）：
sha256sum openEuler-docker.aarch64.tar.xz   # 与官方发布页公布的 sha256 值逐字符比对
# 步骤2 导入下载的基础镜像（摘要核对通过后执行）
podman load -i openEuler-docker.aarch64.tar.xz
# 步骤3 根据镜像创建容器，启动前需要重置（unset 命令）物理机上的代理环境变量
unset http_proxy
unset https_proxy
podman run -dit --name openeuler2203sp4_base --hostname openeuler2203sp4_base -p 10000:22 --privileged --ipc=host docker.io/library/openeuler-22.03-lts-sp4:latest
# 【privileged 护栏】--privileged + --ipc=host/--net=host 使容器获得宿主机设备与内核全部能力
# （等价于接近 root 的宿主机权限面），仅限本节编译/部署容器与 §2.6.4 客户端容器这类部署容器
# 场景使用；用后必须 podman stop/rm 回收，不得长期留存，创建/回收动作均记入台账（容器名、
# 时间、用途）。
# 步骤4 进入容器
podman exec -it openeuler2203sp4_base /bin/bash
# 步骤5 在容器中配置代理等临时环境变量，不能配置到 bashrc 中
export TMOUT=0
export http_proxy=http://xxx  # 按需配置
export https_proxy=http://xxx  # 按需配置
# 步骤6 在容器中安装基础软件包
yum install openssh-server openssh-clients passwd vim perf sysstat dos2unix htop sshpass jq numactl hostname python3 python3-devel python3-pip tar createrepo ipmitool iproute git systemd psmisc udev wget rpmdevtools gtk-doc pam-devel xmlsec1-devel libtool libtool-ltdl-devel cmake gcc-c++ libstdc++-static java-1.8.0-openjdk java-1.8.0-openjdk-devel fio iputils make -y
# 步骤7 退出容器，在物理机上制作镜像
exit
podman commit openeuler2203sp4_base openeuler2203sp4_base:v2203sp4
# 步骤8 在物理机上制作编译容器
podman run --name openeuler2203sp4_build --hostname openeuler2203sp4_base -p 10001:22 --privileged --ipc=host -dti localhost/openeuler2203sp4_base:v2203sp4 /usr/sbin/init
# 步骤9 在物理机上制作部署容器
podman run --name openeuler2203sp4_release --hostname openeuler2203sp4_base -p 10003:22 --privileged --ipc=host -dti localhost/openeuler2203sp4_base:v2203sp4 /usr/sbin/init
# 步骤10 检查容器环境变量，确保环境变量中不能有代理配置；如果有，可以重置物理机上的代理环境
# 变量，然后从步骤 3 开始重新执行
podman inspect openeuler2203sp4_release | grep Env -A 10
```

说明：如果遇到证书异常错误无法安装基础软件包时，保持 TLS 证书校验开启（禁止为联网安装全局关闭
yum 的 sslverify 证书校验）：先把内部 CA 装入系统信任库（cp <internal-ca>.crt
/etc/pki/ca-trust/source/anchors/ && update-ca-trust extract，容器内执行时写入镜像层随镜像分发）
后重试；yum 配置文件 /etc/yum.conf 仅在文件末尾添加 `deltarpm=0`（离线受限内网场景，用后恢复）。

### 2.4.2 在编译容器环境中配置依赖

部分依赖内容仅在编译 Ceph 时需要，实际部署 Ceph 时不需要，此类依赖仅需在编译容器内配置。

```bash
# 步骤1 进入编译容器
podman exec -it openeuler2203sp4_build /bin/bash
# 步骤2 在容器中配置代理等环境变量，不建议配置到 bashrc 中
export TMOUT=0
export http_proxy=http://xxx  # 按需配置
export https_proxy=http://xxx  # 按需配置
# 步骤3 安装通用组件
yum install CUnit-devel boost-random checkpolicy cmake cryptsetup-devel expat-devel fmt-devel fuse-devel gperf java-devel junit keyutils-libs-devel libaio-devel libbabeltrace-devel libblkid-devel libcap-ng-devel libcurl-devel numactl-devel libicu-devel libnl3-devel liboath-devel librabbitmq-devel librdkafka-devel librdmacm-devel libtool libxml2-devel lttng-ust-devel lua-devel luarocks lz4-devel make nasm ncurses-devel ninja-build nss-devel openldap-devel openssl-devel libudev-devel python3-Cython python3-devel python3-prettytable python3-pyyaml python3-setuptools python3-sphinx re2-devel selinux-policy-devel sharutils snappy-devel sqlite-devel sudo thrift-devel valgrind-devel xfsprogs-devel xmlstarlet doxygen meson python3-pyelftools libatomic gperftools-devel -y
# 步骤4 定义 RPM 包编译路径
vi /root/.rpmmacros
# 将 %_topdir 路径设置为编译 RPM 包的路径（本例中以新建路径 /root/rpmbuild 为例），并将其他行
# 的内容全部注释掉；首次编辑时该文件不存在，直接新增如下内容即可
# %_topdir /root/rpmbuild
# 创建 rpmbuild 下的构建目录
rpmdev-setuptree
# 步骤5 编译 ceph-mgr-cephadm 依赖包
# 1. git 配置（代理按需临时配置，用后恢复；证书校验保持开启，见 SKILL.md TLS 铁律）
git config --global http.proxy http://*****  # 按需配置
git config --global https.proxy http://*****  # 按需配置
git config --global http.postBuffer 1048576000  # 将 Git 缓冲区大小增加到 repo 的最大单个文件大小：1G
# 说明：git clone 遇 SSL 证书失败（self signed certificate in certificate chain）时，优先把内部
# CA 装入系统信任库（cp <internal-ca>.crt /etc/pki/ca-trust/source/anchors/ &&
# update-ca-trust extract）后正常 clone；确需临时绕过时用命令级一次性参数
# git -c http.sslVerify=false clone <url>（仅对单条命令生效，禁止写全局/系统 gitconfig，
# 见 SKILL.md TLS 铁律），豁免操作记入台账。
# 2. 编译 python-asyncssh 依赖（openEuler 22.03 推荐使用 python-asyncssh 2.7）
git clone https://gitee.com/src-oepkgs/python-asyncssh.git
cp python-asyncssh/* /root/rpmbuild/SOURCES/
cp python-asyncssh/* /root/rpmbuild/SPECS/
rpmbuild -bb /root/rpmbuild/SPECS/python-asyncssh.spec
# 3. 编译 python-natsort 依赖
git clone https://gitee.com/src-openeuler/python-natsort.git
yum install python3-coverage python3-hypothesis python3-pytest python3-pytest-cov python3-pytest-mock -y
cp python-natsort/* /root/rpmbuild/SOURCES/
cp python-natsort/* /root/rpmbuild/SPECS/
rpmbuild -bb /root/rpmbuild/SPECS/python-natsort.spec
# 4. 安装 RPM 包
mkdir -p /home/local_rpm/
mv /root/rpmbuild/RPMS/noarch/* /home/local_rpm/
cd /home/local_rpm/
yum install python-asyncssh-help-2.7.0-2.noarch.rpm python3-asyncssh-2.7.0-2.noarch.rpm python3-natsort-8.4.0-3.noarch.rpm -y
# 5. 退出容器
exit
```

### 2.4.3 在编译容器中编译 Ceph

在配置好依赖的编译容器中编译 Ceph，并把编译的 RPM 包拷贝到部署容器中用于镜像制作。

```bash
# 步骤1 进入编译容器
podman exec -it openeuler2203sp4_build /bin/bash
# 步骤2 在 /home 目录下载并解压 ceph-17.2.8 源码（需确保环境联网；https 校验失败时先按
# TLS 铁律装内部 CA，装 CA 后正常 wget；download.ceph.com 为官方认证渠道）
cd /home
wget https://download.ceph.com/tarballs/ceph-17.2.8.tar.gz
tar zxvf ceph-17.2.8.tar.gz
# 步骤3 修改 ceph.spec 文件
cd ceph-17.2.8
sed -i 's/redhat-rpm-config/openEuler-rpm-config/g' ceph.spec
sed -i 's#%if 0%{?fedora} || 0%{?rhel}#%if 0%{?fedora} || 0%{?rhel} || 0%{?openEuler}#' ceph.spec
sed -i 's#%if 0%{?rhel} || 0%{?fedora}#%if 0%{?rhel} || 0%{?fedora} || 0%{?openEuler}#' ceph.spec
sed -i 's#%if 0%{?fedora} || 0%{?suse_version} > 1500 || 0%{?rhel} == 9 || 0%{?openEuler}#%if 0%{?fedora} || 0%{?suse_version} > 1500 || 0%{?rhel} == 9#' ceph.spec
sed -i '1a\%define _binaries_in_noarch_packages_terminate_build 0' ceph.spec
sed -i '2a\%define _unpackaged_files_terminate_build 0' ceph.spec
# 步骤4 编译 Ceph
cd ..
tar cf ceph-17.2.8.tar.bz2 ceph-17.2.8/
cp ceph-17.2.8/ceph.spec /root/rpmbuild/SPECS/
cp ceph-17.2.8.tar.bz2 /root/rpmbuild/SOURCES/
rpmbuild -bb /root/rpmbuild/SPECS/ceph.spec
# 步骤5 编译 selinux-policy 依赖（Ceph 17.2.8 版本需要编译 selinux-policy 依赖，Ceph 17.2.7
# 及以下版本可跳过此步骤）
git clone https://gitee.com/src-openeuler/selinux-policy.git
cd selinux-policy
git checkout -b 2203sp3 origin/openEuler-22.03-LTS-SP3
cd ..
cp selinux-policy/* /root/rpmbuild/SOURCES/
cp selinux-policy/selinux-policy.spec /root/rpmbuild/SPECS/
rpmbuild -bb /root/rpmbuild/SPECS/selinux-policy.spec
# 步骤6 将编译好的 RPM 包拷贝到 home 中
mv /root/rpmbuild/RPMS/aarch64 /home/local_rpm/
mv /root/rpmbuild/RPMS/noarch /home/local_rpm/
# 步骤7 在物理机上将编译好的 RPM 包导入到部署容器
podman cp openeuler2203sp4_build:/home/local_rpm openeuler2203sp4_release:/home/
```

说明：

- 编译 Ceph 的过程中，至少需要 100GB 的磁盘空间存放临时文件，请注意为 /root/rpmbuild 目录
  预留充足空间；编译过程需要确保环境联网。
- Ceph 17.2.8 版本会有 OSD 偶尔不稳定重启的异常，见 §2.9.2。
- 若 Ceph 源码 src/osd/SnapMapper.cc 与 fmt 包的版本不匹配时，会出现编译报错，需要将
  SnapMapper.cc 文件中 fmt::format 相关代码行注释后重新编译，共涉及 4 处修改：233 行-234 行、
  272 行-275 行、321 行-324 行、327 行-329 行，示例如下（改动处均已加注释）：

```cpp
    228 tl::expected<SnapMapper::object_snaps, Scrub::SnapMapReaderI::result_t>
    229 SnapMapper::get_snaps_common(const hobject_t &oid) const
    230 {
    231   ceph_assert(check(oid));
    232   set<string> keys{to_object_key(oid)};
    233 //  dout(20) << fmt::format("{}: key string: {} oid:{}", __func__, keys, oid)
    234 //         << dendl;
    235
    236   map<string, ceph::buffer::list> got;
    ...
    264 std::set<std::string> SnapMapper::to_raw_keys(
    265   const hobject_t &clone,
    266   const std::set<snapid_t> &snaps) const
    267 {
    268   std::set<std::string> keys;
    269   for (auto snap : snaps) {
    270     keys.insert(to_raw_key(snap, clone));
    271   }
    272 //  dout(20) << fmt::format(
    273 //              "{}: clone:{} snaps:{} -> keys: {}", __func__, clone, snaps,
    274 //              keys)
    275 //         << dendl;
    276   return keys;
    277 }
    ...
    305   std::set<snapid_t> snaps_from_mapping;
    306   for (auto &[k, v] : kvmap) {
    307     dout(20) << __func__ << " " << hoid << " " << k << dendl;
    308     // extract the object ID from the value fetched for an SNA mapping key
    309     auto [sn, obj] = SnapMapper::from_raw(v);
    310     if (obj != hoid) {
    311       dout(1) << fmt::format(
    312                    "{}: unexpected object ID {} for key{} (expected {})",
    313                    __func__, obj, k, hoid)
    314               << dendl;
    315       return tl::unexpected(result_t{code_t::inconsistent});
    316     }
    317     snaps_from_mapping.insert(sn);
    318   }
    319
    320   if (snaps_from_mapping != *obj_snaps) {
    321 //    dout(10) << fmt::format(
    322 //                "{}: hoid:{} -> mapper internal inconsistency ({} vs {})",
    323 //                __func__, hoid, *obj_snaps, snaps_from_mapping)
    324 //           << dendl;
    325     return tl::unexpected(result_t{code_t::inconsistent});
    326   }
    327  // dout(10) << fmt::format(
    328 //              "{}: snaps for {}: {}", __func__, hoid, snaps_from_mapping)
    329 //         << dendl;
    330   return obj_snaps;
    331 }
```

### 2.4.4 在部署容器中制作部署镜像

制作最小完整功能的部署镜像，并导出为文件，后续拷贝到真实的部署集群节点 ceph1~ceph3 和
client1 中使用。

```bash
# 步骤1 进入部署容器
podman exec -it openeuler2203sp4_release /bin/bash
# 步骤2 安装 Ceph 依赖包（RPM 包来源于 §2.4.2 步骤 5 编译产物）
yum install python-asyncssh-help-2.7.0-2.noarch.rpm python3-asyncssh-2.7.0-2.noarch.rpm python3-natsort-8.4.0-3.noarch.rpm -y
pip3 install kubernetes==18.20.0
# 说明：安装过程需要确保环境联网；为避免 Python 三方库 pycryptodome 版本太低导致部署失败，
# 建议升级如下依赖包；为避免部署 MGR 报错 No module named 'kubernetes.client.models.v1_event'，
# 需安装 kubernetes==18.20.0
pip install pycryptodome==3.19.1
# 步骤3 安装 Ceph（将 §2.4.3 步骤 7 拷贝的 RPM 包进行安装）
# Ceph 17.2.8 版本需要额外安装 selinux-policy 相关包
cd /home/local_rpm
yum install aarch64/ceph-17.2.8-0.aarch64.rpm aarch64/ceph-base-17.2.8-0.aarch64.rpm aarch64/ceph-common-17.2.8-0.aarch64.rpm aarch64/ceph-debugsource-17.2.8-0.aarch64.rpm aarch64/ceph-exporter-17.2.8-0.aarch64.rpm aarch64/ceph-fuse-17.2.8-0.aarch64.rpm aarch64/ceph-immutable-object-cache-17.2.8-0.aarch64.rpm aarch64/ceph-mds-17.2.8-0.aarch64.rpm aarch64/ceph-mgr-17.2.8-0.aarch64.rpm aarch64/ceph-mon-17.2.8-0.aarch64.rpm aarch64/ceph-osd-17.2.8-0.aarch64.rpm aarch64/ceph-radosgw-17.2.8-0.aarch64.rpm aarch64/ceph-selinux-17.2.8-0.aarch64.rpm aarch64/ceph-test-17.2.8-0.aarch64.rpm aarch64/cephfs-java-17.2.8-0.aarch64.rpm aarch64/cephfs-mirror-17.2.8-0.aarch64.rpm aarch64/libcephfs-devel-17.2.8-0.aarch64.rpm aarch64/libcephfs2-17.2.8-0.aarch64.rpm aarch64/libcephfs_jni-devel-17.2.8-0.aarch64.rpm aarch64/libcephfs_jni1-17.2.8-0.aarch64.rpm aarch64/libcephsqlite-17.2.8-0.aarch64.rpm aarch64/libcephsqlite-devel-17.2.8-0.aarch64.rpm aarch64/librados-devel-17.2.8-0.aarch64.rpm aarch64/librados2-17.2.8-0.aarch64.rpm aarch64/libradospp-devel-17.2.8-0.aarch64.rpm aarch64/libradosstriper-devel-17.2.8-0.aarch64.rpm aarch64/libradosstriper1-17.2.8-0.aarch64.rpm aarch64/librbd-devel-17.2.8-0.aarch64.rpm aarch64/librbd1-17.2.8-0.aarch64.rpm aarch64/librgw-devel-17.2.8-0.aarch64.rpm aarch64/librgw2-17.2.8-0.aarch64.rpm aarch64/python3-ceph-argparse-17.2.8-0.aarch64.rpm aarch64/python3-ceph-common-17.2.8-0.aarch64.rpm aarch64/python3-cephfs-17.2.8-0.aarch64.rpm aarch64/python3-rados-17.2.8-0.aarch64.rpm aarch64/python3-rbd-17.2.8-0.aarch64.rpm aarch64/python3-rgw-17.2.8-0.aarch64.rpm aarch64/rados-objclass-devel-17.2.8-0.aarch64.rpm aarch64/rbd-fuse-17.2.8-0.aarch64.rpm aarch64/rbd-mirror-17.2.8-0.aarch64.rpm aarch64/rbd-nbd-17.2.8-0.aarch64.rpm noarch/ceph-grafana-dashboards-17.2.8-0.noarch.rpm noarch/ceph-mgr-cephadm-17.2.8-0.noarch.rpm noarch/ceph-mgr-dashboard-17.2.8-0.noarch.rpm noarch/ceph-mgr-diskprediction-local-17.2.8-0.noarch.rpm noarch/ceph-mgr-k8sevents-17.2.8-0.noarch.rpm noarch/ceph-mgr-modules-core-17.2.8-0.noarch.rpm noarch/ceph-mgr-rook-17.2.8-0.noarch.rpm noarch/ceph-prometheus-alerts-17.2.8-0.noarch.rpm noarch/ceph-resource-agents-17.2.8-0.noarch.rpm noarch/ceph-volume-17.2.8-0.noarch.rpm noarch/cephadm-17.2.8-0.noarch.rpm noarch/cephfs-top-17.2.8-0.noarch.rpm noarch/selinux-policy-35.5-23.noarch.rpm noarch/selinux-policy-devel-35.5-23.noarch.rpm noarch/selinux-policy-help-35.5-23.noarch.rpm noarch/selinux-policy-minimum-35.5-23.noarch.rpm noarch/selinux-policy-mls-35.5-23.noarch.rpm noarch/selinux-policy-sandbox-35.5-23.noarch.rpm noarch/selinux-policy-targeted-35.5-23.noarch.rpm -y
yum install chrony haproxy keepalived -y
# Ceph 17.2.7 版本安装命令如下，其他版本调整 RPM 包的名字正确即可
yum install aarch64/ceph-17.2.7-0.aarch64.rpm aarch64/ceph-base-17.2.7-0.aarch64.rpm aarch64/ceph-common-17.2.7-0.aarch64.rpm aarch64/ceph-debugsource-17.2.7-0.aarch64.rpm aarch64/ceph-exporter-17.2.7-0.aarch64.rpm aarch64/ceph-fuse-17.2.7-0.aarch64.rpm aarch64/ceph-immutable-object-cache-17.2.7-0.aarch64.rpm aarch64/ceph-mds-17.2.7-0.aarch64.rpm aarch64/ceph-mgr-17.2.7-0.aarch64.rpm aarch64/ceph-mon-17.2.7-0.aarch64.rpm aarch64/ceph-osd-17.2.7-0.aarch64.rpm aarch64/ceph-radosgw-17.2.7-0.aarch64.rpm aarch64/ceph-selinux-17.2.7-0.aarch64.rpm aarch64/ceph-test-17.2.7-0.aarch64.rpm aarch64/cephfs-java-17.2.7-0.aarch64.rpm aarch64/cephfs-mirror-17.2.7-0.aarch64.rpm aarch64/libcephfs-devel-17.2.7-0.aarch64.rpm aarch64/libcephfs2-17.2.7-0.aarch64.rpm aarch64/libcephfs_jni-devel-17.2.7-0.aarch64.rpm aarch64/libcephfs_jni1-17.2.7-0.aarch64.rpm aarch64/libcephsqlite-17.2.7-0.aarch64.rpm aarch64/libcephsqlite-devel-17.2.7-0.aarch64.rpm aarch64/librados-devel-17.2.7-0.aarch64.rpm aarch64/librados2-17.2.7-0.aarch64.rpm aarch64/libradospp-devel-17.2.7-0.aarch64.rpm aarch64/libradosstriper-devel-17.2.7-0.aarch64.rpm aarch64/libradosstriper1-17.2.7-0.aarch64.rpm aarch64/librbd-devel-17.2.7-0.aarch64.rpm aarch64/librbd1-17.2.7-0.aarch64.rpm aarch64/librgw-devel-17.2.7-0.aarch64.rpm aarch64/librgw2-17.2.7-0.aarch64.rpm aarch64/python3-ceph-argparse-17.2.7-0.aarch64.rpm aarch64/python3-ceph-common-17.2.7-0.aarch64.rpm aarch64/python3-cephfs-17.2.7-0.aarch64.rpm aarch64/python3-rados-17.2.7-0.aarch64.rpm aarch64/python3-rbd-17.2.7-0.aarch64.rpm aarch64/python3-rgw-17.2.7-0.aarch64.rpm aarch64/rados-objclass-devel-17.2.7-0.aarch64.rpm aarch64/rbd-fuse-17.2.7-0.aarch64.rpm aarch64/rbd-mirror-17.2.7-0.aarch64.rpm aarch64/rbd-nbd-17.2.7-0.aarch64.rpm noarch/ceph-grafana-dashboards-17.2.7-0.noarch.rpm noarch/ceph-mgr-cephadm-17.2.7-0.noarch.rpm noarch/ceph-mgr-dashboard-17.2.7-0.noarch.rpm noarch/ceph-mgr-diskprediction-local-17.2.7-0.noarch.rpm noarch/ceph-mgr-k8sevents-17.2.7-0.noarch.rpm noarch/ceph-mgr-modules-core-17.2.7-0.noarch.rpm noarch/ceph-mgr-rook-17.2.7-0.noarch.rpm noarch/ceph-prometheus-alerts-17.2.7-0.noarch.rpm noarch/ceph-resource-agents-17.2.7-0.noarch.rpm noarch/ceph-volume-17.2.7-0.noarch.rpm noarch/cephadm-17.2.7-0.noarch.rpm noarch/cephfs-top-17.2.7-0.noarch.rpm -y
yum install chrony haproxy keepalived -y
# 步骤4 修改 Ceph 用户登录配置
vim /etc/passwd
# 将 Ceph 的登录 shell 修改为 "/bin/bash"
# 步骤5 在物理机上将容器提交成镜像（688247c8b260 为 openeuler2203sp4_release 容器对应的
# 容器 ID，通过 podman ps 查看；[IP] 需要替换为实际本地仓库 IP 地址；执行 commit 前可将
# /home 目录下已安装的 RPM 包删除以精简镜像大小）
podman commit 688247c8b260 [IP]:5000/ceph/ceph_release:v17.2.8
# 步骤6 将部署镜像导出（a6e8aff2def8 为步骤 5 提交的镜像对应的 ID，通过 podman images 查看）
podman save -o ceph_release.tar a6e8aff2def8
```

### 2.4.5 物理机中本地仓库搭建和镜像配置（在 ceph1~ceph3 和 client1 节点的物理机上执行）

```bash
# 步骤1 关闭 SELinux
setenforce 0
# 步骤2 拉取 registry:2 到本地仓（拉取后核对镜像 digest：skopeo inspect
# docker://docker.io/library/registry:2 | grep Digest，与 docker.io 官方发布页展示的 digest
# 逐字符核对一致后才可使用，核对不一致即停止并改从可信渠道重新获取，防 tag 漂移/镜像投毒）
podman pull docker.io/library/registry:2
# 说明：若不能访问外网，使用如下方法或手动下载包导入到节点（git 证书校验保持开启：优先把内部
# CA 装入系统信任库后正常 clone；确需临时绕过时用命令级一次性参数
# git -c http.sslVerify=false clone <url>，仅对单条命令生效，禁止写全局/系统 gitconfig，
# 见 SKILL.md TLS 铁律，豁免操作记入台账）：
# git clone https://github.com/NotGlop/docker-drag.git
# 启动本地仓：podman load -i registry_2.tar
# 如代理上网时不能正常拉取镜像，需检查代理设置方式（Podman 依赖环境变量 HTTP_PROXY 和
# HTTPS_PROXY 进行代理上网）
# 步骤3 修改容器配置文件 /etc/containers/registries.conf（[IP] 替换为实际仓库 IP 地址；
# 如果文件中已有配置项，只需要调整配置的值即可；本地仓按步骤 4 启用认证 + HTTPS，此处不配置
# insecure 明文通道，改为装 CA + 登录，见下方说明）
vi /etc/containers/registries.conf
```

```
unqualified-search-registries = ["[IP]", "quay.io"]
short-name-mode="enforcing"
[[registry]]
location = "[IP]:5000"
```

说明：本地仓库启用认证 + HTTPS 后无需 insecure 项——客户端须把仓库证书的 CA 装入系统信任库
（openEuler：cp <registry-ca>.crt /etc/pki/ca-trust/source/anchors/ && update-ca-trust extract；
Ubuntu 见 §2.2.2），并先 `podman login [IP]:5000 -u deploy`（口令交互输入）再拉取/推送；若现场
受控内网确需保留明文 HTTP 直连本地仓库（无认证无 TLS，不推荐），才额外配置 insecure 信任项并
在台账记录风险与替代方案。

```bash
# 步骤4 启动本地仓库（htpasswd 认证 + HTTPS + 绑定管理网段地址；podman run 命令中的
# "localhost/registry:2" 为步骤 2 中成功拉取的仓库名称和版本；口令不得写进文档正文/脚本/台账
# ——交互输入或从 vault 占位下发）
mkdir -p /home/registry-data /home/registry-auth /home/registry-certs
# 4.1 生成 htpasswd（deploy 为账户名；先装口令工具：openEuler yum install -y httpd-tools，
# Ubuntu apt install -y apache2-utils；口令交互输入或 vault 占位下发，禁止明文落盘落文档）
htpasswd -Bn deploy > /home/registry-auth/htpasswd   # 交互输入口令（推荐）
# 或 vault 下发口令时：htpasswd -Bbn deploy <vault:registry-deploy-口令> > /home/registry-auth/htpasswd
# 4.2 准备 TLS 证书（优先内部 CA 签发；<server.crt>/<server.key> 为证书路径占位，按现场替换，
# 证书 SAN 需覆盖仓库地址；CA 证书按 §2.2.1/§2.2.2 装入 ceph1~ceph3、client1 各节点系统信任库）
# cp <server.crt> /home/registry-certs/server.crt
# cp <server.key> /home/registry-certs/server.key
# 4.3 启动（认证 + HTTPS；绑定管理网段地址——示例为 ceph1 的 198.51.100.166，按仓库所在节点
# 实际管理 IP 替换，禁止 0.0.0.0 全网卡监听）
podman run -d -p 198.51.100.166:5000:5000 \
  -v /home/registry-data:/var/lib/registry \
  -v /home/registry-auth:/auth:ro \
  -e REGISTRY_AUTH=htpasswd \
  -e REGISTRY_AUTH_HTPASSWD_REALM="ceph-local-registry" \
  -e REGISTRY_AUTH_HTPASSWD_PATH=/auth/htpasswd \
  -v /home/registry-certs:/certs:ro \
  -e REGISTRY_HTTP_TLS_CERTIFICATE=/certs/server.crt \
  -e REGISTRY_HTTP_TLS_KEY=/certs/server.key \
  --restart always --name registry localhost/registry:2
# 网络策略：5000 端口仅对管理网段 ceph1~ceph3（及 client1 按需）放通，防火墙/交换机 ACL 按
# 最小可达范围配置，禁止对管理网段之外开放；各节点拉取/推送前先 podman login [IP]:5000
# -u deploy（口令交互输入）。内部 CA 流程暂不具备时，可先去掉 -v /certs 与 REGISTRY_HTTP_TLS_*
# 两行仅保留 htpasswd 认证（客户端须配置 insecure 明文信任项并记台账），并尽快补齐 TLS。
# 步骤5 导入 §2.4.4 中生成的 Ceph 部署镜像
podman load -i ceph_release.tar
# 步骤6 镜像重命名并上传到本地仓（8bbba7d5cc80 为 ceph_release.tar 对应的镜像 ID，通过
# podman images 命令查看；[IP] 为本地 IP 地址；v17.2.8 是 Ceph 版本，可根据实际部署的版本调整；
# 本地仓已启用认证，推送前先登录：podman login [IP]:5000 -u deploy，口令交互输入）
podman tag 8bbba7d5cc80 [IP]:5000/ceph/ceph_release:v17.2.8
podman push [IP]:5000/ceph/ceph_release:v17.2.8 [IP]:5000/ceph/ceph_release:v17.2.8
# push 成功后记录镜像 digest 并入台账（后续 DEFAULT_IMAGE 与跨节点拉取一律按 digest 固定引用，
# 防 tag 被覆盖重推导致版本漂移）：
# skopeo inspect docker://[IP]:5000/ceph/ceph_release:v17.2.8 | grep Digest
# （登录凭据复用 podman login 写入的认证文件；digest 形如 sha256:<64 位十六进制>）
podman rmi 8bbba7d5cc80
# 使用 Docker 技术时，push 操作的命令如下（先 docker login 198.51.100.10:5000 -u deploy，
# 口令交互输入）：
# docker push 198.51.100.10:5000/ceph/ceph_release:v17.2.8
# 若 push 镜像时存在 "Gateway Time-out" 的报错，可能是因为配置了代理，需要关闭代理配置：
# unset http_proxy
# unset https_proxy
# 步骤7 修改 cephadm 文件中的 DEFAULT_IMAGE（按步骤 6 台账记录的 digest 固定引用——
# tag 可被覆盖重推，digest 不可变；<digest> 替换为步骤 6 台账记录的 sha256 值）
vim /usr/sbin/cephadm
# DEFAULT_IMAGE = "[IP]:5000/ceph/ceph_release@sha256:<digest>"
# 步骤8 保持 cephadm 源码原样：不修改 run_cmd 函数中的 privileged 判断（原样为
# if self.privileged:，禁止改写成恒真条件让判断永远成立）。
# 【privileged 场景边界】本地镜像制作中需要 privileged 的仅限临时编译容器与 §2.6.4 客户端
# 部署容器（护栏与台账要求见 §2.4.1 与 SKILL.md 铁律 8：用后 podman stop/rm 回收、创建/回收
# 动作记入台账）；cephadm 编排的长期常驻守护容器（MON/MGR/OSD/RGW）不得 --privileged——
# 恒真改写会让这些守护容器全部以 privileged 运行，显著扩大宿主机攻击面。
```

## 2.5 部署前环境分析与配置（§5.2.3.3）

### 2.5.1 系统配置

**（可选）关闭 SMMU**：步骤同 §1.3.1（进入 BIOS → "Advanced>MISC Config" → "Support Smmu"
设置为 "Disabled"，F10 保存退出）。更多 BIOS 调优手段参见 Ceph 对象存储 调优指南中的
"BIOS 调优"章节。

**关闭防火墙**（所有服务端节点和客户端节点的物理机操作系统中执行）：

```bash
# Ubuntu 系统关闭防火墙
ufw disable
ufw status
# openEuler 系统关闭防火墙
systemctl disable firewalld
systemctl stop firewalld
systemctl status firewalld
```

**关闭 SELinux**：为避免 SELinux 的访问控制策略限制影响系统的正常运行，需要关闭 SELinux。
未关闭 SELinux 策略时，可能会出现部署节点时节点中的守护进程一直是 Created 状态、无法变成正常
的 Up 状态导致异常。注意重启可能导致该策略的变化，需要重新设置。

```bash
# openEuler 下关闭 SELinux
setenforce 0
sed -i 's/=permissive/=disabled/g' /etc/selinux/config
sed -i 's/=enforcing/=disabled/g' /etc/selinux/config
# Ubuntu 下关闭 SELinux
apt install selinux-utils
setenforce 0
```

**配置主机名**（服务端 ceph1~ceph3，客户端 client1）：

```bash
hostnamectl --static set-hostname ceph1
hostnamectl --static set-hostname client1
vi /etc/hosts
# 在集群内各服务端节点和客户端节点的 /etc/hosts 中添加：
# 198.51.100.166   ceph1
# 198.51.100.167   ceph2
# 198.51.100.168   ceph3
# 198.51.100.160   client1
```

网络连通验证方法同 §1.3.3（`ping ceph2` 正确响应即为修改域名解析文件成功）。

**配置集群主机间时间同步策略**：Ubuntu 下使用 NTP 服务，openEuler 下使用 Chrony 服务。

Ubuntu 下配置 NTP：

```bash
# 步骤1 安装 NTP 服务（所有服务端 ceph1~ceph3 和客户端 client1 节点）
apt install -y ntp ntpdate
cd /etc && mv ntp.conf ntp.conf.bak
# 以 ceph1 为 NTP 服务端节点，在 ceph1 新建 /etc/ntp.conf，新增如下内容作为 NTP 服务端
# restrict 127.0.0.1
# restrict ::1
# restrict 198.51.100.0 mask 255.255.255.0
# server 127.127.1.0
# fudge 127.127.1.0 stratum 8
# 在 ceph2、ceph3 及所有客户端节点新建 /etc/ntp.conf，新增：server 198.51.100.166
# 步骤2 在 ceph1 节点启动 NTP 服务，检查状态为 active（running）
systemctl stop ntp
systemctl start ntp
systemctl enable ntp
systemctl status ntp
# 在除 ceph1 的所有节点强制停止 NTP 服务，同步 server（ceph1）时间
service ntp stop
ntpdate ceph1
# 在除 ceph1 的所有节点写入硬件时钟，避免重启后失效
hwclock -w
# 在除 ceph1 的所有节点安装并启动 crontab 工具
apt install -y cron
systemctl start cron
systemctl enable cron
crontab -e
# 添加以下内容，每隔 10 分钟自动与 ceph1 同步时间（此 IP 为 ceph1 节点 Public Network 的 IP）
# */10 * * * * /usr/sbin/ntpdate 198.51.100.166
```

注意：ceph1 节点启动 NTP 进程 5 分钟后，再在其他节点进行时间同步，否则会报错
（no server suitable for synchronization found）。

openEuler 配置 Chrony：

```bash
# 步骤1 所有服务端 ceph1~ceph3 节点和客户端 client1 节点安装 Chrony 服务
dnf install -y chrony
# 步骤2 备份配置文件
mv /etc/chrony.conf /etc/chrony.conf.bak
# 步骤3 修改配置文件
cat > /etc/chrony.conf <<EOF
server xxx iburst # xxx为提供时钟服务的服务器在网络中的ip或域名
allow xxx/xx # xxx/xx 是本地网络的IP地址范围（如：192.0.2.0/24），表示只有这个子网的机器才能与服务器进行时钟同步。
local stratum 10
EOF
# 步骤4 重启 Chrony
systemctl restart chronyd
systemctl enable chronyd
# 步骤5 查看时间同步状态
chronyc -a makestep  # 强制同步系统时间
chronyc sourcestats   # 显示当前时间源的同步统计信息
chronyc sources -v    # 显示当前时间源的同步信息
```

**配置免密登录**：ceph1 节点对所有服务端/客户端节点的免密（包括 ceph1 本身），及 client1 节点
对所有服务端/客户端节点的免密（包括 client1 本身）：

```bash
# 步骤1 在 ceph1 节点生成公钥，并发放到各个服务端/客户端节点（按 Enter 使用默认配置）
ssh-keygen -t rsa
for i in {1..3}; do ssh-copy-id ceph$i; done
ssh-copy-id client1
# 步骤2 在 client1 节点生成公钥，并发放到各个服务端/客户端节点
ssh-keygen -t rsa
for i in {1..3}; do ssh-copy-id ceph$i; done
```

### 2.5.2 配置 ceph.conf（仅需在主节点 ceph1 上执行）

```bash
# 步骤1 新建 "/root/ceph-mycluster/" 目录
mkdir /root/ceph-mycluster && cd /root/ceph-mycluster
# 步骤2 配置 /root/ceph-mycluster/ceph.conf，添加如下内容
vi /root/ceph-mycluster/ceph.conf
```

```
[global]
mon_max_pg_per_osd = 3000
mon_max_pool_pg_num = 300000
ms_bind_before_connect = true
ms_dispatch_throttle_bytes = 2097152000
throttler_perf_counter = false
bluefs_buffered_io=false
bluestore_default_buffered_read = false
bluestore_default_buffered_write = false
osd_pool_default_pg_autoscale_mode = off
osd_pool_default_size = 2
osd_min_pg_log_entries = 10
osd_max_pg_log_entries = 10
bluestore_cache_size_ssd = 6G
osd_memory_target = 8G
mon_allow_pool_delete = true
rgw_frontends = beast port=10000
rgw_thread_pool_size = 1024                         # beast 前端不支持 num_threads 选项，RGW 线程池由 rgw_thread_pool_size 控制
rgw_override_bucket_index_max_shards = 8000          # = 预计桶内最大对象数/100000
rgw_dynamic_resharding = false
```

说明：配置节点命令以及使用 cephadm 操作 Ceph 集群时，建议在专门的集群目录（如
/root/ceph-mycluster/）下执行，以避免不必要的冲突。更多 Ceph 调优配置参见 Ceph 对象存储
调优指南中的 "Ceph 调优"章节。

### 2.5.3 分析 OSD 节点

```bash
# 在所有准备部署 OSD 的服务端节点确认可用磁盘
lsblk -T
```

回显中 "/dev/sdm" 为系统盘，可用的 HDD 磁盘有 "/dev/sda"、"/dev/sdb" 等，可用的 SSD 磁盘有
"/dev/nvme0n1" 等。

说明：有一些硬盘可能是以前 Ceph 集群里的数据盘或者曾经安装过操作系统，这些硬盘上可能存在未
清理的分区，需先按残留数据分区清理流程处理（见 §2.9.1）。cephadm 部署 OSD 节点会自动划分
DB 分区与 WAL 分区，无需手动分区；必须清理分区，否则 Ceph 无法识别硬盘。

## 2.6 安装 Ceph 与部署节点（§5.2.4）

### 2.6.1 部署 MON、MGR 节点（仅需在主节点 ceph1 执行，需先完成 §2.5.2）

前置告警处置：

- 若出现警告 "ERROR: Failed command: /usr/bin/docker pull quay.io/ceph/ceph:v17"，可能是由于
  没有配置 Docker 代理导致，参见 §2.2.2 完成 Docker 代理配置。
- 若出现警告 "x509: certificate signed by unknown authority"，可能是安全证书未配置，参见
  §2.2.2 配置安全证书。
- 若出现警告 Error: initializing source docker://xx.xx.xx.xx:5000/ceph/ceph_release@sha256:xxxx:
  pinging container registry xx.xx.xx.xx:5000: Get "https://xx.xx.xx.xx:5000/v2/": http: server
  gave HTTP response to HTTPS client，说明本地仓库尚未启用 TLS 而客户端按 HTTPS 访问：按
  §2.4.5 步骤 4 为仓库配置受信 CA 签发的证书，客户端装入 CA（§2.2.1/§2.2.2）并
  podman/docker login 后再拉取。
- 若拉取本地仓库镜像提示 401 unauthorized / authentication required，说明未登录或凭据失效：
  各拉取节点先 podman login [IP]:5000 -u deploy（口令交互输入）再重试（cephadm 拉取
  DEFAULT_IMAGE 前同样要求仓库所在节点已登录）。

```bash
# 步骤1 引导 Ceph 集群，启动部署 MON 和 MGR 节点
# 多节点 Ceph 集群启动
cd /root/ceph-mycluster && cephadm bootstrap --config ceph.conf --mon-ip 198.51.100.166 --cluster-network 203.0.113.0/24 --allow-fqdn-hostname --skip-monitoring-stack
# 说明：--mon-ip 对应前端网络的 IP 地址；--cluster-network 对应后端网络的 IP 地址；
# --config ceph.conf 可选，需要修改 Ceph 默认配置时使用
# 如果环境上同时安装了 Podman 和 Docker，cephadm 会默认优先使用 Podman；想通过 Docker 启动
# 容器可以使用 --docker 选项：
# cephadm --docker bootstrap --config ceph.conf --mon-ip 198.51.100.166 --cluster-network 203.0.113.0/24 --allow-fqdn-hostname --skip-monitoring-stack
# 请保持全局容器技术的一致性：建议 Ubuntu 系统使用 Docker 技术，openEuler 系统使用 Podman。
# 如需启动不同版本的 Ceph 集群，可使用 vi 命令修改 /usr/sbin/cephadm 脚本中 DEFAULT_IMAGE
# 变量的值。手动制作容器镜像进行多节点启动时，cephadm 会自动将相关镜像拉取到每个服务器节点；
# 使用本地镜像仓库时，需保证本地镜像仓库中的镜像可以被其他节点拉取（不同容器技术的信任配置见
# §2.2.1 / §2.2.2）；本地仓已启用认证与 TLS，所有拉取节点（ceph1~ceph3、client1）须先装入
# 仓库 CA 并 podman login / docker login [IP]:5000 -u deploy（口令交互输入），否则 cephadm
# bootstrap 拉取 DEFAULT_IMAGE 将报 401 认证失败
# 步骤2 拷贝公钥到 ceph2、ceph3、client1 节点
ssh-copy-id -f -i /etc/ceph/ceph.pub root@ceph2
ssh-copy-id -f -i /etc/ceph/ceph.pub root@ceph3
ssh-copy-id -f -i /etc/ceph/ceph.pub root@client1
# 步骤3 本地仓库配置同步到其他节点（Ubuntu 系统部署官方镜像时不需要执行此步骤）
scp /etc/containers/registries.conf ceph2:/etc/containers/
scp /etc/containers/registries.conf ceph3:/etc/containers/
scp /etc/containers/registries.conf client1:/etc/containers/
# 如果选择使用 Docker 技术且有自定义 Docker 配置，除本地仓库配置外还需同步 Docker 配置：
# scp /etc/docker/daemon.json ceph2:/etc/docker/
# scp /etc/docker/daemon.json ceph3:/etc/docker/
# scp /etc/docker/daemon.json client1:/etc/docker/
# 步骤4 启动一个 Ceph 容器
cephadm shell
# 步骤5 查看是否配置成功
ceph -s
# 步骤6 添加 ceph2、ceph3 节点（添加 host 节点耗时较长，需耐心等待一段时间后再检查；
# 此步骤不添加 client1 节点，后续在创建 RGW 节点时添加）
ceph orch host add ceph2 198.51.100.167 --labels _admin
ceph orch host add ceph3 198.51.100.168 --labels _admin
# 步骤7 查看 host 是否添加成功
ceph orch host ls
# 步骤8 查看是否配置成功，等待 3 个 MON 节点启动完成
ceph -s
```

### 2.6.2 部署 OSD 节点（仅需在主节点 ceph1 上执行，需在 cephadm shell 启动的容器内 bash 中执行）

添加为 OSD 的磁盘设备需满足以下所有条件：

- 设备必须没有分区。
- 设备不得具有任何 LVM 状态。
- 该设备不得包含文件系统。
- 该设备不得包含 Ceph BlueStore OSD。
- 设备必须大于 5GB。

```bash
# 步骤1 查看系统可用盘
ceph orch device ls --wide --refresh
# 步骤2 配置 osd_spec.yaml（支持混合存储场景，根据实际情况调整配置文件）
vi osd_spec.yaml
```

示例配置 1：指定具体磁盘部署

```yaml
service_type: osd
service_id: osd_spec_hdd
placement:
  hosts:
    - ceph1
    - ceph2
    - ceph3
spec:
  data_devices:
    paths:
      - /dev/sda
      - /dev/sdb
  db_devices:
    paths:
      - /dev/nvme0n1
      - /dev/nvme1n1
```

示例配置 2：仅指定磁盘类型和数量，自动选择磁盘部署

```yaml
service_type: osd
service_id: osd_spec_hdd
placement:
  hosts:
    - ceph1
    - ceph2
    - ceph3
spec:
  data_devices:
    rotational: 1 # 1 for HDD, 0 for SSD
  db_devices:
    rotational: 0
    limit: 2
```

配置说明：

- service_type：设置为 osd，表示这段配置用来部署 Ceph OSD 服务。
- placement - hosts：列出 OSD 将要部署的节点。
- spec - data_devices - paths：指明当前部署所需磁盘，与 rotational、limit 配置互斥；如不指定
  特定磁盘，Ceph 将自动选择符合 rotational、limit 配置的磁盘。
- spec - data_devices - rotational: 1：数据存储设备为机械硬盘（HDD）；rotational: 0 为 SSD。
- spec - db_devices - paths / rotational / limit：DB 存储设备配置；limit 指定用于数据库的
  SSD 设备数量限制，数据库设备通常用于存储 Ceph 的 WAL（Write-Ahead Logging）和其他元数据。
- 应用场景：混合存储场景（HDD 存数据 + SSD 加速 DB，限制每个 OSD 实例最多 2 个 SSD 作为
  数据库设备）；全闪存场景（数据和数据库设备都使用 SSD/NVMe，rotational 设为 0，用于高性能
  计算）；冷存储场景（数据设备 HDD rotational=1，数据库设备可用 SSD/NVMe）。

```bash
# 步骤3 应用配置（磁盘名仅为示例，请根据服务器实际磁盘盘符修改后再执行）
ceph orch apply -i osd_spec.yaml
# 步骤4 等待并查看是否配置成功完成，等待所有 OSD 节点状态都为 up
ceph -s
```

### 2.6.3 部署 RGW 节点

```bash
# 步骤1 添加 client1 host（业务客户端不携带 _admin 标签：_admin 会下发 ceph.client.admin
# keyring，client.admin 属管理侧身份；业务侧仅签发对象账户所需身份，见 §2.6.4 步骤 2 说明；
# ceph2/ceph3 为服务端管理节点，可保留 _admin）
ceph orch host add client1 198.51.100.160 --labels rgw
# 步骤2 在标记为 RGW 的主机上创建端口（下面以 8 个 RGW 节点为例，端口分别为 10000、10001...）
ceph orch apply rgw test_rgw --placement="label:rgw count-per-host:8" --port=10000
# 步骤3 查看集群信息
ceph -s
```

### 2.6.4 部署客户端

```bash
# 步骤1 在客户端节点上启动容器
# 说明：根据当前系统所使用的容器技术调整本节容器操作相关命令（本节示例以 Podman 为例，
# 使用 Docker 技术则将命令换为 Docker 即可）；[IMAGE_ID] 替换为实际 podman images 命令查看的
# 镜像 ID；本小节部署的客户端与 §2.6.3 中的 client1 是同一个节点
# 【privileged 护栏】--privileged --net=host --ipc=host 使容器获得接近宿主机 root 的能力面，
# 仅限部署容器场景使用；用后 podman stop/rm 回收并记台账（见 §2.4.1 护栏说明与 SKILL.md 铁律）
# 自制镜像时，需要手动将镜像拷贝到客户端节点
podman load -i ceph_release.tar
podman tag [IMAGE_ID] localhost/ceph_release:v17.2.8
podman run --name client1 --hostname client1 --privileged --net=host --ipc=host -dti localhost/ceph_release:v17.2.8 /usr/sbin/init
# 如果使用 Ceph 官方镜像，可直接拉取官网镜像进行部署（需联网；拉取前核对官方 digest：
# skopeo inspect docker://quay.io/ceph/ceph:v17 | grep Digest，与 quay.io 页面展示的 digest
# 逐字符核对一致后再按 digest 拉取——quay.io/ceph/ceph@sha256:<官方digest>，防 tag 漂移）
podman run --name client1 --hostname client1 --privileged --net=host --ipc=host -dti quay.io/ceph/ceph:v17 /usr/sbin/init
# 步骤2 从服务端同步配置文件至客户端（不复制 client.admin keyring：client.admin 属管理侧身份，
# 业务客户端容器不携带；业务侧仅签发对象账户所需身份——RGW S3 用户即 radosgw-admin user create
# 产物（§2.7.2），无需集群 CephX 身份；如需在业务侧做 RADOS 级访问或状态查看，在管理节点
# （cephadm shell / client.admin 持有者环境）签发限权 client.<app> 并仅下发限权 keyring）
sudo mkdir -p /etc/ceph
scp -r ceph1:/etc/ceph/ceph.conf /etc/ceph/
# （可选）业务侧限权身份：在管理节点执行（<rgw 池> 按实际池名替换，如 default.rgw.buckets.data）
# ceph auth get-or-create client.rgwapp mon 'allow r' osd 'allow rw pool=<rgw 池>' -o /etc/ceph/ceph.client.rgwapp.keyring
# scp /etc/ceph/ceph.client.rgwapp.keyring client1:/etc/ceph/
podman exec client1 mkdir -p /etc/ceph
podman cp /etc/ceph/ceph.conf client1:/etc/ceph/
# （可选）podman cp /etc/ceph/ceph.client.rgwapp.keyring client1:/etc/ceph/
# 步骤3 验证（管理动作只在管理节点/client.admin 持有者执行：ceph -s / ceph osd pool set 等
# 集群级命令在 ceph1 的 cephadm shell 中执行；业务客户端容器内仅用限权身份做只读检查）
podman exec -it client1 /bin/bash
# 业务容器内用限权身份做只读验证（可选）：
# ceph -s --name client.rgwapp -k /etc/ceph/ceph.client.rgwapp.keyring
# 说明：Ceph17 默认会根据 OSD 数量自动调整 PG 数量，需要关闭此配置（在管理节点执行）
ceph osd pool set ${pool_name} pg_autoscale_mode off
```

## 2.7 部署对象存储功能（§5.2.5）

### 2.7.1 创建存储池（仅需在主节点 ceph1 上的 cephadm shell 中执行）

```bash
# 步骤1 创建 Data Pool 和 Index Pool
ceph osd pool create default.rgw.buckets.index 1024 1024
ceph osd pool create default.rgw.buckets.data 4096 4096
ceph osd pool application enable default.rgw.buckets.data rgw
ceph osd pool application enable default.rgw.buckets.index rgw
# 步骤2 通过 ceph df 查看存储池是否创建完成
ceph df
```

### 2.7.2 创建 RGW 账户（仅需在主节点 ceph1 上执行）

```bash
# 步骤1 在 ceph1 上创建 RGW 账户
radosgw-admin user create --uid="admin" --display-name="admin user"
# 步骤2 完成创建账户后查询账户信息，用户可以通过 access_key 和 secret_key 访问 RGW 节点
# 读、写对象
radosgw-admin user info --uid=admin
```

## 2.8 卸载 Ceph（§5.2.6）

```bash
# 步骤1 在 ceph1 节点暂停后台进程（单命令形态：cephadm shell -- <命令> 在 shell 容器内执行后
# 自动返回宿主机，不进入交互式 shell）
cephadm shell -- ceph orch pause
# 步骤2 宿主机 shell 查询 Ceph 集群的 fsid 并记录为现场核准变量（变量必须在宿主机 shell 赋值，
# 不得在 cephadm shell 交互块内赋值——交互块内赋值的变量随容器退出即失效；禁止使用文档示例 fsid）
CLUSTER_FSID=$(cephadm shell -- ceph fsid | tr -d '\r')
echo "CLUSTER_FSID=${CLUSTER_FSID}"   # 与变更前台账记录的现场 fsid 逐字符核对
# （备选）宿主机已装 ceph-common 且 /etc/ceph/ceph.conf 可用时，也可在集群目录下直接采集：
# cd /root/ceph-mycluster && CLUSTER_FSID=$(ceph fsid)
# 步骤3 核对台账后在宿主机执行删除集群（破坏性操作，执行前需与用户二次确认目标集群与 fsid；
# 本地实例 fsid 与台账记录不一致时停止并报告，禁止继续删除）
cephadm rm-cluster --zap-osds --force --fsid ${CLUSTER_FSID}
# 步骤4 在所有其他节点执行步骤 1~3（各节点同样先现场读取 fsid 核对后再执行 rm-cluster）
```

## 2.9 常见问题（§5.2.7）

### 2.9.1 如何清理残留数据分区

**清理数据卷：**

```bash
# 步骤1 使用如下命令观察硬盘，识别残留数据卷
lsblk
# 步骤2 以 bcache0 设备为例，复制目标数据卷 lvm 编号，执行以下命令进行清理
dmsetup remove ceph--f559c3f7--d0e8--4cdc--b1d5--4c7208990746-osd--block--9d4a70f9--b75a--4dfb--8b11--121734750846
# 步骤3 再次执行命令，检测是否成功
lsblk
```

**清理文件类型：**

```bash
# 步骤1 使用如下命令观察硬盘，在 FSTYPE 列下发现残留文件系统
lsblk -f
# 步骤2 以清理 nvme1n1p2 分区为例，执行以下命令进行清理
wipefs -a /dev/nvme1n1p2
# 步骤3 再次执行命令，检测是否清理成功
lsblk -f
```

### 2.9.2 Ceph 17.2.8 跑长稳时报错

- 现象：Ceph 17.2.8 集群长时间持续运行时，可能出现 OSD 意外宕机又恢复的不稳定情况，并观察到
  类似如下报错（crash 记录可通过 `ceph crash info <crash_id>` 查看）：

```
"assert_condition": "log.t.seq == log.seq_live",
"assert_file": ".../ceph-17.2.8/src/os/bluestore/BlueFS.cc",
"assert_func": "uint64_t BlueFS::_log_advance_seq()",
"assert_line": 3029,
"assert_msg": ".../BlueFS.cc: 3029: FAILED ceph_assert(log.t.seq == log.seq_live)",
"assert_thread_name": "bstore_kv_sync",
"ceph_version": "17.2.8",
"crash_id": "2025-02-03T09:19:08.749233Z_9e2800fb-77f6-46cb-8087-203ea15a2039",
"entity_name": "osd.211"
```

- 原因：Ceph 17.2.8 已知问题（详见 Ceph 社区 PR）。
- 解决办法：

```bash
# 步骤1 手动修改源码 src/os/bluestore/BlueFS.cc 中的 3108-3118 行（BlueFS::_log_sync 尾段：
# 将 _log_advance_seq() 调用替换为增量序列推进，配合 _pad_bl/log.writer->append/
# ceph_assert(allocated_before_extension…) 与 "// before sync core we advance the seq" 片段
# 定位；优先按函数名 + 代码片段定位修改点，裸行号仅作辅助，行号以实际 17.2.8 源码为准）
# 步骤2 重新编译部署 Ceph
```

```cpp
  _pad_bl(bl, super.block_size);
  log.writer->append(bl);
  ceph_assert(allocated_before_extension >= log.writer->get_effective_write_pos());
  // before sync_core we advance the seq
  {
    std::unique_lock<ceph::mutex> l(dirty.lock);
    dirty.seq_live++;
    log.seq_live++;
    log.t.seq++;
  }
}
```

### 2.9.3 如何进行 Public 和 Cluster 网络配置增大网络带宽

**交换机配置 bond**（所有节点的组 bond 网口均需配置，以 ceph1 为例）：

```bash
# 步骤1 登录交换机，进入交互模式
system-view
# 步骤2 查看网口信息
display interface brief
# 步骤3 清除需要组 bond 口的网口配置
int 25GE1/0/23      # or interface 25GE1/0/23
clear configuration this
q
# 步骤4 交换机组 bond/trunk（trunkport 替换为对应的网口）
int eth-trunk ${id}   # or interface ${id}
mode lacp-static
trunkport 25GE1/0/23  # 替换为对应的网口
trunkport 25GE1/0/26  # 替换为对应的网口
commit
q # 退出
# 步骤5 查看 bond 信息
d in eth-trunk32    # or display interface ${id}
# 步骤6 重复步骤 1~5，配置所有节点需要组 bond 的网络端口
```

**Ubuntu 系统服务器配置 bond**（以 ceph1 节点和 Ubuntu 操作系统为例）：

```bash
# 步骤1 在各节点修改网络配置文件
vi /etc/netplan/50-cloud-init.yaml
```

```yaml
# This file is generated from information provided by the datasource.  Changes
# to it will not persist across an instance reboot.  To disable cloud-init's
# network configuration capabilities, write a file
# /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg with the following:
# network: {config: disabled}
network:
    ethernets:
        enp125s0f0:
            addresses:
            - 192.0.2.166/24    # 管理IP地址
            nameservers:
                addresses: []
                search: []
            routes:
            -   to: default
                via: 192.0.2.1  # 网关（同网段可用主机地址，示例值按现场实际网关替换）
#-----------以下为bond新增内容----------------
        enp3s0:
            dhcp4: false
        enp4s0:
            dhcp4: false
        enp5s0:
            dhcp4: false
        enp6s0:
            dhcp4: false
    bonds:
        bond0:      # bond设备名称
          addresses: [198.51.100.166/24]    # bond后的ip地址
          interfaces:
          - enp3s0     # 组bond的设备1，设备要与交换机组bond/trunk的端口匹配
          - enp4s0     # 组bond的设备2，设备要与交换机组bond/trunk的端口匹配
          parameters:
            mode: 802.3ad
        bond1:
          addresses: [203.0.113.166/24]
          interfaces:
          - enp5s0
          - enp6s0
          parameters:
            mode: 802.3ad
    version: 2
```

说明：将每个节点的 addresses 修改为实际使用的 IP 地址；网关（routes.via）、bond 地址、
interfaces 端口号均为占位示例，须按现场规划逐项替换；负载均衡需要手动配置分发策略
`echo layer3+4 > /sys/class/net/bond0/bonding/xmit_hash_policy`。

```bash
# 步骤2 应用配置（若 WARNING 提示没有 open vswitch，执行 apt install openvswitch-switch 即可）
netplan apply
# 步骤3 查看 bond 设备网络信息
apt install net-tools
ifconfig  # or ip a
# 步骤4 在所有节点重复步骤 1~3
```

**openEuler 系统服务器配置 bond**（推荐使用 nmcli 工具，以 ceph1 节点为例）：

```bash
# 步骤1 查看当前组网情况
nmcli connection show
# 步骤2 创建名称为 bond_01 的 bond
nmcli con add type bond ifname bond_01 mode 4
# 步骤3 配置 bond_01 的 IP 地址（需替换为实际需要配置的网络 IP 地址和子网掩码）
nmcli connection modify bond-bond_01 ipv4.addresses 203.0.113.166/24
nmcli connection modify bond-bond_01 ipv4.method manual
# 步骤4 增加 slave 网口（enp7s0/enp8s0 为网口的 NAME，需与交换机配置 bond/trunk 的端口匹配）
nmcli con add type bond-slave ifname enp7s0 master bond-bond_01
nmcli con add type bond-slave ifname enp8s0 master bond-bond_01
# 步骤5 查看最新网络信息
nmcli connection show
# 步骤6 应用配置，使 bond_01 生效
nmcli connection up bond-bond_01
# 说明：检查 bond 网络配置文件 /etc/sysconfig/network-scripts/ifcfg-bond-bond_01，如果配置中
# 包含 BONDING_OPTS=mode=802.3ad 可能会导致网络流量不均衡，需要调整为：
# BONDING_OPTS="mode=4 miimon=100 xmit_hash_policy=layer3+4"
# 随后重启 bond_01 和网络服务
ifdown  bond-bond_01
ifup bond-bond_01
systemctl restart NetworkManager
# 步骤7 检查 bond 生效情况
ip a
# 步骤8 在所有节点重复步骤 1~7
```

更多系统调优配置可参考：Ceph 对象存储 调优指南 > 系统调优。
