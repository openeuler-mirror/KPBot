# Ceph 块存储部署指南（CentOS 7.6 & openEuler 20.03 & openEuler 22.03，ceph-deploy）

> 来源：《鲲鹏 BoostKit 分布式存储使能套件 Ceph 编译&部署&调优&测试指南》（文档版本 07，
> 2025-06-30）第 4 章「Ceph 块存储 部署指南（CentOS 7.6&openEuler 20.03&openEuler 22.03）」
> 全文蒸馏（§4.2 环境要求 / §4.3.1 镜像源配置与 ceph-deploy 适配 / §4.3.2 系统配置 /
> §4.3.3 部署前环境分析与配置 / §4.4 安装 Ceph 与部署节点 / §4.5 使能 Ceph 块存储 /
> §4.6 常见问题 / §4.7 故障排除）。本文件按 SOP 步骤组织，命令与配置片段逐字保留
> （PDF 换行断词已复原为完整命令）。TLS 校验、磁盘核准清单、限权身份与分阶段验证的
> 安全/正确性加固为本仓口径补充（见各节「修正说明」）。

## 0. 版本与适用矩阵

| 项 | 版本 |
|---|---|
| Ceph | 14.2.8 Nautilus / 16.2.7 Pacific（其他版本安装也可参考本文档） |
| ceph-deploy | 2.0.1 / 2.1.0 |
| OS | CentOS Linux release 7.6.1810；openEuler 20.03 LTS SP1（Kernel: 4.19.90-2012.4.0.0053.oe1）；openEuler 22.03 LTS SP4（Kernel: 5.10.0-216.0.0.115.oe2203sp4 / ...oe2203sp4.aarch64） |
| 硬件 | 鲲鹏服务器：鲲鹏920 5220 处理器，2*32核 2600MHz，内存 8*16GB 2933MHz，IN200 网卡 4*25GE；系统盘 RAID1（2*960GB SATA SSD）；数据盘 RAID 模式下使能 JBOD（12*4TB SATA HDD）；NVMe SSD 缓存盘 1*ES3000 V5 3.2TB；RAID 控制卡 Avago SAS 3508 |

集群规划：服务端 ceph1~ceph3（管理 192.0.2.166~168、Public 198.51.100.166~168、Cluster
203.0.113.166~168），客户端 client1~client3（管理 192.0.2.160~162、Public 198.51.100.160~162）。
示例网段采用 RFC 5737 文档保留地址（192.0.2.0/24 管理、198.51.100.0/24 Public、
203.0.113.0/24 Cluster），按现场规划替换；后文 hosts、NTP、ceph.conf 等命令中的示例 IP
同样为文档保留地址。
管理 IP 用于远程 SSH 管理；Cluster Network 用于集群内部节点间同步数据（任选一个 25GE 网口）；
Public Network 供其他节点访问（任选一个 25GE 网口）；客户端当压力机时业务口 IP 与集群
Public Network 同网段。Ceph 客户端和服务端都需安装 Ceph 软件。
全新安装操作系统建议最小化安装；在线安装软件包需接入互联网。

硬盘划分（BlueStore）：每个节点 12 块 4TB 数据盘（每块作为一个 OSD 数据分区）+ 1 块 3.2TB
NVMe 盘（作为 12 个 OSD 的 DB、WAL 分区）。WAL 分区大于 10GB 即够用；官方建议 DB 分区不小于
数据盘容量的 4%。本方案以 WAL 60GB、DB 180GB 为例：单个 OSD = 4TB 数据盘 + 180GB DB 分区 +
60GB WAL 分区。

## 1. 镜像源配置与 ceph-deploy 适配（§4.3.1）

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

openEuler 20.03 可参考 skill `ceph-build` 的 `build-guide.md` §2.2「编译 liboath-devel 软件包」手动编译。

### 1.2 配置 Ceph 镜像源（在集群内所有服务端和客户端节点配置）

在线源（Ceph 16.2.7，建议 openEuler 22.03 用户使用）：由于操作系统差异，Ceph 14.2.8 版本
不建议使用在线源进行安装，建议使用本地源编译后进行安装。

```bash
# 步骤1 备份原有 openEuler.repo 文件（cp -n 不覆盖已有文件：重复运行保留首次备份原件；
#       run_id 为时间戳，备份为 openEuler.repo.bak.<run_id>，回退时从该备份恢复首次原件
#       并写入台账；在线源与本地源为互斥安装方式但同目标文件，两种路径均按本步骤备份）
RUN_ID=$(date +%Y%m%d%H%M%S)
cp -n /etc/yum.repos.d/openEuler.repo{,.bak.$RUN_ID}
# 步骤2 编辑 /etc/yum.repos.d/openEuler.repo，添加 openEuler-22.03-LTS-SP4 的
#       [OS]/[everything]/[EPOL]/[debuginfo]/[source]/[update]/[update-source] 七个 repo 段
#       （完整内容见下方 ini 配置块，与姊妹 skill ceph-install 的 installation-guide.md
#         §1.2 同源）
```

七个 repo 段完整内容如下（出处为姊妹 skill `ceph-install` 同源内容）：

```ini
[OS]
name=OS
baseurl=http://repo.openeuler.org/openEuler-22.03-LTS-SP4/OS/$basearch/
metalink=https://mirrors.openeuler.org/metalink?repo=$releasever/OS&arch=$basearch
metadata_expire=1h
enabled=1
gpgcheck=1
gpgkey=https://repo.openeuler.org/openEuler-22.03-LTS-SP4/OS/$basearch/RPM-GPG-KEY-openEuler
priority=1
[everything]
name=everything
baseurl=http://repo.openeuler.org/openEuler-22.03-LTS-SP4/everything/$basearch/
metalink=https://mirrors.openeuler.org/metalink?repo=$releasever/everything&arch=$basearch
metadata_expire=1h
enabled=1
gpgcheck=1
gpgkey=https://repo.openeuler.org/openEuler-22.03-LTS-SP4/everything/$basearch/RPM-GPG-KEY-openEuler
priority=1
[EPOL]
name=EPOL
baseurl=http://repo.openeuler.org/openEuler-22.03-LTS-SP4/EPOL/main/$basearch/
metalink=https://mirrors.openeuler.org/metalink?repo=$releasever/EPOL/main&arch=$basearch
metadata_expire=1h
enabled=1
gpgcheck=1
gpgkey=https://repo.openeuler.org/openEuler-22.03-LTS-SP4/OS/$basearch/RPM-GPG-KEY-openEuler
[debuginfo]
name=debuginfo
baseurl=http://repo.openeuler.org/openEuler-22.03-LTS-SP4/debuginfo/$basearch/
metalink=https://mirrors.openeuler.org/metalink?repo=$releasever/debuginfo&arch=$basearch
metadata_expire=1h
enabled=1
gpgcheck=1
gpgkey=https://repo.openeuler.org/openEuler-22.03-LTS-SP4/debuginfo/$basearch/RPM-GPG-KEY-openEuler
[source]
name=source
baseurl=http://repo.openeuler.org/openEuler-22.03-LTS-SP4/source/
metalink=https://mirrors.openeuler.org/metalink?repo=$releasever&arch=source
metadata_expire=1h
enabled=1
gpgcheck=1
gpgkey=https://repo.openeuler.org/openEuler-22.03-LTS-SP4/source/RPM-GPG-KEY-openEuler
[update]
name=update
baseurl=http://repo.openeuler.org/openEuler-22.03-LTS-SP4/update/$basearch/
metalink=https://mirrors.openeuler.org/metalink?repo=$releasever/update&arch=$basearch
metadata_expire=1h
enabled=1
gpgcheck=1
gpgkey=https://repo.openeuler.org/openEuler-22.03-LTS-SP4/OS/$basearch/RPM-GPG-KEY-openEuler
[update-source]
name=update-source
baseurl=http://repo.openeuler.org/openEuler-22.03-LTS-SP4/update/source/
metalink=https://mirrors.openeuler.org/metalink?repo=$releasever/update&arch=source
metadata_expire=1h
enabled=1
gpgcheck=1
gpgkey=https://repo.openeuler.org/openEuler-22.03-LTS-SP4/source/RPM-GPG-KEY-openEuler
```

安全说明（TLS 校验保持开启）：各段 gpgkey 一律使用 HTTPS 官方渠道地址，不得为联网安装关闭
yum 证书校验；内网自签名镜像源场景把内部 CA 装入系统信任库（`cp <internal-ca>.crt
/etc/pki/ca-trust/source/anchors/ && update-ca-trust extract`）。回退时执行
`cp -p /etc/yum.repos.d/openEuler.repo.bak.<run_id> /etc/yum.repos.d/openEuler.repo`
恢复首次原件（run_id 取台账中记录的首次备份时间戳）。

本地源：

- 编译后安装（针对 Ceph 14.2.8 版本）：参考 skill `ceph-build` 的「配置编译环境」完成编译环境配置，
  再参考其「Ceph 的编译与部署」完成 Ceph 软件的本地安装。
- 挂载本地镜像源（针对 Ceph 16.2.7 版本，建议 openEuler 22.03 用户）：

```bash
# 步骤1 备份原有 openEuler.repo 文件（cp -n 不覆盖已有文件：重复运行保留首次备份原件；
#       run_id 为时间戳，备份为 openEuler.repo.bak.<run_id>；不 mv 原件，改写入新文件）
RUN_ID=$(date +%Y%m%d%H%M%S)
cp -n /etc/yum.repos.d/openEuler.repo{,.bak.$RUN_ID}
# 已有更早备份时先核对当前配置与首次原件差异，再决定是否继续（差异写入台账）
diff -u "$(ls -1t /etc/yum.repos.d/openEuler.repo.bak.* 2>/dev/null | head -n 1)" /etc/yum.repos.d/openEuler.repo || true
# 步骤2 下载 openEuler-22.03-LTS-SP4 系统镜像，上传 iso 到 /root，挂载本地镜像源
mount /root/openEuler-22.03-LTS-SP4-everything-aarch64-dvd.iso /media/
# 步骤3 配置 repo 源：新建 /etc/yum.repos.d/local.repo，添加以下内容（写入新文件，
#       不覆盖 openEuler.repo.bak.<run_id> 备份；如需停用原在线源，先核对备份完整
#       后将 openEuler.repo 各段 enabled 改为 0，操作写入台账）
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
# 步骤2 在 ceph1 获取 ceph-deploy 源码（公开 github.com 地址走系统信任库 CA 校验，
#       固定 v2.1.0 tag 浅克隆，不使用免校验 clone）
git clone --branch v2.1.0 --depth 1 https://github.com/ceph/ceph-deploy.git
# （内网自签名镜像源场景：先把内部 CA 装入系统信任库，保持 TLS 校验开启
#  cp <internal-ca>.crt /etc/pki/ca-trust/source/anchors/
#  update-ca-trust extract）
```

版本核对（安装前必做，独立于安装命令执行；结果记入台账）：

```bash
# 步骤3 核对源码版本：记录 HEAD commit 与 tag，与 SKILL.md 声明版本（2.0.1/2.1.0）
#       对应关系核对，不匹配即中止安装并排查
git -C /root/ceph-deploy rev-parse HEAD
git -C /root/ceph-deploy describe --tags
```

```bash
# 步骤4 安装 ceph-deploy（按 Python 版本选择命令）
cd /root/ceph-deploy
# openEuler 20.03 用户：
python setup.py install
# openEuler 22.03 用户：
python3 setup.py install
# 步骤5 查看 ceph-deploy 的版本
ceph-deploy --version
```

修正说明（TLS 与版本固定）：源文档建议通过修改 `~/.gitconfig` 的 `[http]` 段全局关闭 git
证书校验来绕过 clone 失败，该做法会全局关闭证书校验，扩大中间人攻击暴露面，本仓不采用。
公开 github.com 地址必须走系统信任库 CA 校验，代码块内不出现免校验 clone；仅内网自签名
镜像源在把内部 CA 装入系统信任库后仍确需临时绕过时，用命令级 `git -c http.sslVerify=false`
（仅单条命令一次性生效），执行后在台账记录。同时源文档 clone 未固定版本，本仓改为
`--branch v2.1.0 --depth 1` 固定 tag 克隆，安装前以 `git rev-parse HEAD` 核对 commit 并与
SKILL.md 声明版本（2.0.1/2.1.0）记入台账，不匹配即中止。

## 2. 系统配置（§4.3.2）

### 2.1 （可选）关闭 SMMU

部署 Ceph 前关闭 SMMU，以防其对 Ceph 性能造成影响：进入 BIOS（参见《TaiShan 服务器 BIOS 参数
参考（鲲鹏920处理器）》），进入 Advanced->MISC Config 页面，将 Support Smmu 设置为 Disabled，
按 F10 保存退出。

### 2.2 关闭防火墙

在所有服务端节点和客户端节点依次执行：

```bash
systemctl stop firewalld
systemctl disable firewalld
systemctl status firewalld
```

### 2.3 配置主机名

步骤 1 配置永久静态主机名（服务端 ceph1~ceph3，客户端 client1~client3，其余节点以此类推）：

```bash
hostnamectl --static set-hostname ceph1
hostnamectl --static set-hostname client1
```

步骤 2 修改域名解析文件：在集群内各个服务端节点和客户端节点的 `/etc/hosts` 中添加：

```text
198.51.100.166   ceph1
198.51.100.167   ceph2
198.51.100.168   ceph3
198.51.100.160   client1
198.51.100.161   client2
198.51.100.162   client3
```

说明：检查服务端节点和客户节点网络连接是否成功，如登录 ceph1 节点执行 `ping ceph2`，
返回 `64 bytes from ceph2 (198.51.100.167): icmp_seq=1 ...` 且 0% packet loss 即为成功。

### 2.4 配置 NTP

Ceph 会自动校验存储节点之间的时间，若不同节点之间时差较大会有告警，需配置 NTP 同步各节点时间。

```bash
# 步骤1 安装 NTP 服务（所有服务端节点和客户端节点）
yum -y install ntp ntpdate
# 备份旧配置（所有节点；cp -n 不覆盖已有文件：重复运行保留首次备份原件，不 mv 原件；
# run_id 为时间戳，备份为 ntp.conf.bak.<run_id>，回退时从该备份恢复原件并写入台账）
RUN_ID=$(date +%Y%m%d%H%M%S)
cp -n /etc/ntp.conf{,.bak.$RUN_ID}
```

以 ceph1 为 NTP 服务端节点：在 ceph1 新建 `/etc/ntp.conf`，新增如下内容（其中
`restrict 198.51.100.0 mask 255.255.255.0` 是 ceph1 的网段与掩码）：

```text
restrict 127.0.0.1
restrict ::1
restrict 198.51.100.0 mask 255.255.255.0
server 127.127.1.0
fudge 127.127.1.0 stratum 8
```

在 ceph2、ceph3 及所有客户端节点新建 `/etc/ntp.conf`，新增：`server 198.51.100.166`。

```bash
# 步骤2 启动 NTP 服务
# 在 ceph1 节点启动 NTP 服务，并检查服务状态为 Active
systemctl start ntpd
systemctl enable ntpd
systemctl status ntpd
```

说明：ceph1 节点启动 ntp 进程 5 分钟后，再在其他节点进行时间同步，否则会报错
（no server suitable for synchronization found）。

```bash
# 在除 ceph1 的所有节点强制同步 server（ceph1）时间
ntpdate ceph1
# 同步成功显示：adjust time server 198.51.100.166 offset -0.000034 sec
# 在除 ceph1 的所有节点写入硬件时钟，避免重启后失效
hwclock -w
# 在除 ceph1 的所有节点安装并启动 crontab 工具
yum install -y crontabs
chkconfig crond on
systemctl start crond
crontab -e
# 添加以下内容，每隔 10 分钟自动与 ceph1 同步时间（IP 为 ceph1 节点 Public Network 的 IP 地址）
*/10 * * * * /usr/sbin/ntpdate 198.51.100.166
```

### 2.5 配置免密登录

免密信任域最小化：仅需配置主节点 ceph1 对三个服务端节点 ceph1~ceph3（含 ceph1 自身）的
免密——ceph-deploy 部署只从主节点向服务端节点下发配置与密钥；client1~client3 不对集群
节点免密，业务客户端仅使用 §4.2 步骤 4 签发的限权 keyring（client.rbd-app）访问 RBD，
不参与集群管理操作。

```bash
# 步骤1 在 ceph1 节点生成公钥并发放到各服务端节点（ssh-keygen -t rsa 后按回车用默认配置；
#       建议使用专用密钥文件与专用账号，仅放行 ceph-deploy 所需操作，密钥与发放范围记入台账）
ssh-keygen -t rsa
for i in {1..3}; do ssh-copy-id ceph$i; done
```

说明：ceph-deploy 部署过程只需从 ceph1 SSH 到 ceph1~ceph3；上游文档将免密扩大到全部
服务端/客户端节点（含 client1 对所有节点免密），超出最小需要，本仓不采用。§5.2/§6.5 从
ceph1 远程执行客户端 rbd map 时使用常规 SSH 认证（按提示输入密码或经现场管理通道执行），
不依赖免密。撤销动作入台账：部署结束或节点退出集群时，逐节点清理 authorized_keys 中的
对应公钥并记录。

### 2.6 设置 SELINUX 模式

在所有节点执行：

```bash
# 步骤1 临时修改（下次重启自动失效）
setenforce permissive
# 步骤2 永久设置（下次重启自动生效）：编辑 /etc/selinux/config，修改成 SELINUX=permissive
```

### 2.7 修改 umask（仅 openEuler，CentOS 跳过）

openEuler 的安全性很高，需要修改 umask 为 0022 才可以正常安装 Ceph。

```bash
# 步骤1 在所有服务端节点打开 /etc/bashrc，将最后一行修改为 umask 0022
vi /etc/bashrc
# 步骤2 使配置更改生效
source /etc/bashrc
# 步骤3 确认配置是否生效
umask
```

## 3. 部署前环境分析与配置（§4.3.3）

### 3.1 配置 MON 节点（仅需在主节点 ceph1 执行）

步骤 1 新建 `/root/ceph-mycluster/` 集群目录，利用 ceph-deploy 部署节点配置文件：

```bash
mkdir /root/ceph-mycluster && cd /root/ceph-mycluster
ceph-deploy new ceph1 ceph2 ceph3
```

说明：使用 ceph-deploy 操作 ceph 集群时，建议在专门的集群目录下进行，而不是在 /etc/ceph/
目录下进行（如在 /root/ceph-mycluster/ 目录下执行），以避免不必要的冲突。

步骤 2 配置 MON 节点：在 `/root/ceph-mycluster/` 目录下自动生成的 ceph.conf 中配置
public network、cluster network 及 MON 节点设置（`vi /root/ceph-mycluster/ceph.conf`），
将 ceph.conf 中的内容修改为如下所示：

```ini
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

说明：修改 ceph.conf 内容是为了将内部集群间的网络与外部访问的网络隔离——网络 203.0.113.0 用于
内部存储集群之间的数据同步（仅在存储节点间使用），网络 198.51.100.0 用于存储节点与计算节点的
数据交互。

核对说明：`mon_initial_members` 与 `mon_host` 必须逐项与 §0 集群规划及 §2.3 的 /etc/hosts
表核对——三个 MON 节点为 ceph1~ceph3，对应 Public 地址依次为 198.51.100.166、198.51.100.167、
198.51.100.168，与 mon_initial_members 的 ceph1, ceph2, ceph3 一一对应；不得混入客户端地址
（client1 的 Public 地址为 198.51.100.160），也不得遗漏任一 MON 节点。填写后执行
`grep -c 198.51.100.166 /etc/hosts` 等逐项核对，确认 mon_host 三个 IP 与 hosts 中 ceph1~ceph3
的 Public 地址完全一致。

### 3.2 配置 MGR 节点

若非 CentOS 7.6 环境请跳过。基于 CentOS 7.6 环境时，在各服务端节点执行：

```bash
pip install pecan==1.4.2 parse==1.20.0
```

### 3.3 配置 OSD 节点

步骤 1 在所有准备部署 OSD 的服务端节点逐机核准待清盘设备清单（写盘前必做，核准结果供
§4.4 OSD 创建与 §6.3 快速部署脚本共用；禁止对盘符区间如 sda~sdl 做范围式直清）：

```bash
# 1a. 查看块设备拓扑（NAME/SIZE/TYPE/MOUNTPOINT），列出候选数据盘与 NVMe DB/WAL 盘
lsblk -o NAME,SIZE,TYPE,MOUNTPOINT
# 1b. 用稳定设备 ID 对照候选盘（sda/sdb 盘符重启后可能漂移，写盘一律使用 by-id 路径）
ls -l /dev/disk/by-id/
# 1c. 从候选中排除根盘：反查 / 所在设备，其所在物理盘整块排除（lsblk 反查该盘全部分区）
df -l /
# 1d. 排除挂载盘：当前所有挂载点中的设备整块排除
findmnt
# 1e. 排除现役 OSD：已建 OSD 的数据盘与 DB/WAL 分区整块排除
ceph-volume lvm list 2>/dev/null || ceph-disk list 2>/dev/null || lsblk
# （BlueStore OSD 数据分区特征：lsblk 中出现 ceph--<fsid>-osd--block--* 的 lvm 卷，
#   或分区标注 osd.wal/osd.db/OSD 结构特征；旧集群残盘按 §6.1 清理后再入清单）
# 1f. 汇总生成该节点的核准清单 approved_disks.txt（每行一个 /dev/disk/by-id/ 路径），
#     打印清单并与 §0 集群规划（每节点 12 块 4TB 数据盘 + 1 块 NVMe 盘）逐项核对
cat approved_disks.txt
```

修正说明（清盘核准）：源文档对三台节点以 for 循环直接 `disk zap /dev/sda~/dev/sdl`，盘符
直清不可逆且可能命中根盘/挂载盘/现役 OSD，本仓不采用。改为逐机核准稳定设备 ID 清单
（lsblk + /dev/disk/by-id 对照），排除根盘、挂载盘与现役 OSD 后按核准清单逐盘执行；
首次写盘前必须打印清单并与 §0 集群规划核对。

说明：部分硬盘可能是以前 Ceph 集群的数据盘或曾安装过操作系统，存在未清理的分区，
可按 §6.1 清理残留数据分区。

步骤 2 划分 OSD 分区（在部署 OSD 的所有节点均执行）：重置磁盘，并根据 NVMe 盘空间规划
WAL 分区和 DB 分区。本节示例仅在单块 NVMe 上演示 1 组 WAL/DB 分区的划分方法（与 §0
单 NVMe 规划一致）；多 OSD 场景必须改用 §6.2 的 partition.sh 快速划分（单盘 12 个 WAL
分区 + 12 个 DB 分区），不得照搬本示例的单分区演示。

```bash
parted /dev/nvme0n1 mklabel gpt
# /dev/nvme0n1 划分 1 个 60GB WAL 分区与 1 个 180GB DB 分区（单 NVMe，与 §0 规划一致）
parted /dev/nvme0n1 mkpart primary ext4 1 60G
parted /dev/nvme0n1 mkpart primary ext4 60G 240G
```

说明：parted 命令实时生效，分区命令完成后已将分区表写入硬盘；/etc/fstab 是系统自动挂载
分区的配置。生产环境建议将 DB 分区与 WAL 分区放置在同一块盘上以提高性能，此处仅作演示
指导。划分结果为 /dev/nvme0n1p1（60GB WAL）与 /dev/nvme0n1p2（180GB DB），与 §4.4 分支 B
及 §6.2/§6.3 的单 NVMe 布局一致。多 OSD 场景快速划分多个 WAL 和 DB 分区参见 §6.2。

## 4. 安装 Ceph 与部署节点（§4.4）

### 4.1 安装 Ceph 软件

- Ceph 14.2.8 版本：参见 skill `ceph-build` 的 `build-guide.md`（Ceph 14.2.8 编译指南，已合入）；
- Ceph 16.2.7 版本：参见姊妹 skill `ceph-install` 的 `installation-guide.md`（Ceph 16.2.7 安装指南，
  已合入，PR #444）；镜像源异常时的降级路径按本文件 §1.2 在线源/本地源配置安装
  Ceph 16.2.7，并在产出中标注「未覆盖」。

### 4.2 部署 MON 节点（仅需在主节点 ceph1 执行；需完成 §3.1；使用 ceph-deploy 时须在
/root/ceph-mycluster/ 目录下执行，否则会报错）

```bash
# 步骤1 初始化监视器并收集密钥
ceph-deploy mon create-initial
# 步骤2 将 ceph.client.admin.keyring 拷贝到各服务端节点（client1~client3 不下发 client.admin，
#       改由步骤4 签发限权身份）
ceph-deploy --overwrite-conf admin ceph1 ceph2 ceph3
# 步骤3 查看是否配置成功（MON 阶段验收：只确认 quorum，此时集群尚无 MGR 与 OSD）
ceph mon stat
# 或执行 ceph quorum_status，JSON 中 quorum_names 应为 ["ceph1","ceph2","ceph3"]
```

`ceph mon stat` 成功回显应包含 `3 mons at {...}` 且 leader/quorum 列表含 ceph1、ceph2、ceph3
三个 MON 节点（3 台 MON 全部在列即 quorum 达成）。分阶段验收口径：MON 部署后只查 quorum，
暂不要求 mgr active 与 osd up/in（此时集群还没有这两个组件），后续每阶段验收见 §4.3/§4.4，
全部部署完成后再统一确认整体 HEALTH_OK。

步骤 4 为业务客户端签发限权身份并只分发该身份的 keyring（不得将 client.admin 下发到业务
客户端——admin 拥有全集群管理权限，下发到业务机会扩大凭据暴露面）：

```bash
# 模板（<rbd-app> 为业务自定义用户名，<pool> 按后文 §5.0 创建的存储池名代入，权限按需收敛）
#   ceph auth get-or-create client.<rbd-app> mon 'allow r' osd 'allow rw pool=<pool>'
# 以本文环境的存储池 vdbench、业务用户名 rbd-app 为例，生成该身份的 keyring 文件：
ceph auth get-or-create client.rbd-app mon 'allow r' osd 'allow rw pool=vdbench' \
  -o /etc/ceph/ceph.client.rbd-app.keyring
# 只分发该限权身份的 keyring 到三台客户端（keyring 不入库、不进 git、不落日志）
for i in {1..3}; do scp /etc/ceph/ceph.client.rbd-app.keyring root@client$i:/etc/ceph/; done
for i in {1..3}; do ssh client$i "chmod 600 /etc/ceph/ceph.client.rbd-app.keyring"; done
```

说明：若出现警告 mon is allowing insecure global_id reclaim（启用了允许使用不安全的全局 ID
回收配置，可能导致数据不一致），可执行以下命令关闭，消除该警告：

```bash
ceph config set mon auth_allow_insecure_global_id_reclaim false
```

### 4.3 部署 MGR 节点（仅需在主节点 ceph1 执行；CentOS 用户需先完成 §3.2；
须在 /root/ceph-mycluster/ 目录下执行）

```bash
# 步骤1 部署 MGR 节点
ceph-deploy mgr create ceph1 ceph2 ceph3
# 步骤2 查看 MGR 是否部署成功（MGR 阶段验收：mgr active）
ceph mgr stat
```

`ceph mgr stat` 成功回显应包含 `available: yes`、`active: ceph1`（active 指向任一 MON 节点
即验收通过）；也可用 `ceph -s` 查看 mgr 行示例：`mgr: ceph1(active, since 2d), standbys:
ceph2, ceph3`。

### 4.4 部署 OSD 节点（仅需在主节点 ceph1 执行；需完成 §3.3；须在
/root/ceph-mycluster/ 目录下执行）

步骤 1 部署 OSD 前先选定布局——分支 A（普通）与分支 B（外置 DB/WAL）互斥，只能选其一并贯穿
本次部署：不得先按分支 A 建完 OSD 再执行分支 B 的 zap（分支 B 首条 `disk zap` 会把分支 A
刚建好的 OSD 数据盘连同数据一并清掉）；两分支的目标盘一律取自 §3.3 步骤 1 逐机核准的
approved_disks.txt 设备清单。此处以 ceph1 清单中的 /dev/sdb（数据盘）、/dev/nvme0n1p1
（60GB WAL）与 /dev/nvme0n1p2（180GB DB，单 NVMe，与 §3.3 步骤 2 划分结果一致）为例：

**分支 A（普通 OSD：数据盘直建，不外置 DB/WAL）**

```bash
cd /root/ceph-mycluster/
ceph-deploy disk zap ceph1 /dev/sdb
ceph-deploy osd create ceph1 --data /dev/sdb
```

**分支 B（外置 DB/WAL：DB/WAL 分区已在 §3.3 步骤 2 划分，zap 与 osd create 同一事务连续执行）**

```bash
cd /root/ceph-mycluster/
ceph-deploy disk zap ceph1 /dev/sdb
ceph-deploy disk zap ceph1 /dev/nvme0n1p1
ceph-deploy disk zap ceph1 /dev/nvme0n1p2
ceph-deploy osd create ceph1 --data /dev/sdb --block-wal /dev/nvme0n1p1 --block-db /dev/nvme0n1p2
```

（可选）添加 DB 与 WAL 分区以提高写入性能：DB 和 WAL 通常部署在 NVMe SSD 上以提高写入性能；
如需配置，选择分支 B 并在添加数据盘 /dev/sdb 的基础上额外设置 --block-wal 与 --block-db 参数。
若没有 NVMe SSD 或直接用 NVMe SSD 作为数据盘，选择分支 A，不需要 --block-db 和 --block-wal，
只需 --data 指定数据盘。

步骤 2 创建成功后查看是否正常（OSD 阶段验收：osd up/in）：

```bash
ceph osd stat
```

成功回显示例（节选）：`1 osds: 1 up (since 2s), 1 in (since 2s)`（osd up/in 即验收通过；
整体 HEALTH_OK 在全部组件部署完成后于 §5.0 步骤 4 统一确认）。

命令说明：`ceph-deploy disk zap` 用来清除目标磁盘（${node} 是节点的 hostname）；
`ceph-deploy osd create` 用来生成 OSD 节点（--data 后是数据盘设备，--block-db 后是 DB 分区，
--block-wal 后是 WAL 分区）。快速部署多个 OSD 节点参见 §6.3。

## 5. 使能 Ceph 块存储（§4.5）

### 5.0 创建存储池（仅需在主节点 ceph1 执行）

Ceph 14.2.1 及以后的版本没有默认的存储池，需自行创建存储池，然后再在存储池中创建块设备。

```bash
# 步骤1 创建存储池（名字可自行命名，此处以 vdbench 为例）
ceph osd pool create vdbench 32 32
```

说明：命令最后的两个数字 32 32 分别代表存储池的 pg 和 pgp 数量。官方建议整个集群所有存储池的
pg 数量之和约为 (OSD 数量 × 100) / 数据冗余因数（副本模式为副本数，EC 模式为数据块+校验块之和，
如三副本为 3、EC4+2 为 6）；pg 数一般取 2 的整数次幂——例如 3 台服务器每台 12 个 OSD 共 36 个
OSD、三副本，按公式为 1200，可取 1024。

- 示例一：修改存储池副本数（以调整为 2 为例）：

```bash
ceph osd pool get vdbench size      # 获取副本数
ceph osd pool set vdbench size 2    # 修改副本数
ceph osd pool get vdbench size      # 再次查看，显示 size: 2
```

- 示例二：查看本地纠删配置：

```bash
ceph osd erasure-code-profile ls        # 查看配置文件列表（默认 default）
ceph osd erasure-code-profile get default   # 查看 default 配置（显示 k=2, m=2）
```

需要新建纠删配置并应用到存储池的用户，参见姊妹 skill `ceph-object-storage-deployment`
的创建 EC 存储池相关内容（同批 PR #447，已合入）。

```bash
# 步骤2 创建存储池后需指定池类型（cephFS、rbd、rgw），此处以块存储 rbd 为例
ceph osd pool application enable vdbench rbd
```

说明：命令后加上 `--yes-i-really-mean-it` 可对存储池类型进行修改。

（可选）步骤 3：存储池使能 zlib 压缩：

```bash
ceph osd pool set vdbench compression_algorithm zlib
ceph osd pool set vdbench compression_mode force
ceph osd pool set vdbench compression_required_ratio .99
```

步骤 4 整体健康验收（分阶段验收的最后一步：MON→MGR→OSD 各阶段通过后，在此统一确认整体
HEALTH_OK；`ceph -s` 回显中出现 `health: HEALTH_OK`、`pools: 1 pools, 32 pgs`、
`pgs: 32 active+clean` 即成功）。

### 5.1 创建块设备（仅需在 ceph1 执行）

```bash
# 步骤1 在 RBD 存储池中创建 1 个大小为 200GB 的块设备（--size 表示块设备大小）
rbd create --size 204800 vdbench/image1
# 步骤2 查看是否创建成功（--pool 指定存储池名称）
rbd ls --pool vdbench
```

快速创建多个块设备参见 §6.4。

### 5.2 映射块设备镜像（仅需在主节点 ceph1 执行）

```bash
# 步骤1 映射 RBD 存储池中 image1 到客户端 client1 上（以 §4.2 步骤4 签发的限权身份
# client.rbd-app 映射，不得以 client.admin 映射；模板为
# --name client.<rbd-app> --keyring /etc/ceph/ceph.client.<rbd-app>.keyring）
ssh client1 "rbd map vdbench/image1 --name client.rbd-app --keyring /etc/ceph/ceph.client.rbd-app.keyring"
# 步骤2 验证：分别登录 client1、client2、client3 检查
ls /dev |  grep rbd
```

输出结果中包含 rbd0 则说明创建成功。快速映射块设备参见 §6.5。

## 6. 常见问题（§4.6，共五则）

### 6.1 如何清理残留数据分区

清理数据卷：

```bash
# 步骤1 观察硬盘，识别残留数据卷
lsblk
# 步骤2 以 bcache0 设备为例，复制目标数据卷 lvm 编号执行清理
dmsetup remove ceph--f559c3f7--d0e8--4cdc--b1d5--4c7208990746-osd--block--9d4a70f9--b75a--4dfb--8b11--121734750846
# 步骤3 再次执行 lsblk 检测是否成功
```

清理文件类型：

```bash
# 步骤1 观察 FSTYPE 列
lsblk -f
# 步骤2 以清理残盘上的残留分区 sdd2 为例（设备名按现场残盘实际替换，清理前按铁律
#       与用户二次确认目标对象；残盘先经 §3.3 步骤 1 核准流程处理）
wipefs -a /dev/sdd2
# 步骤3 再次执行 lsblk -f 检测是否成功
```

### 6.2 如何快速划分多个 WAL 与 DB 分区

以 /dev/nvme0n1 为例（多块 NVMe SSD 或 SATA/SAS SSD 只需替换脚本中的盘符）。NVMe 盘划分为
12 个 60GB 分区、12 个 180GB 分区，分别对应 WAL 分区、DB 分区。

```bash
# 1. 创建 partition.sh 脚本
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

### 6.3 如何快速部署多个 OSD 节点

使用脚本前需完成 §3.3 OSD 节点配置。假设 3 台服务器、每台 12 块硬盘、每个硬盘都要部署 OSD。

**禁止范围式直清**：不得对盘符区间（如 sda~sdl）做 for 循环直清——disk zap 不可逆，可能命中
根盘、挂载盘或现役 OSD。必须先按 §3.3 步骤 1 逐机核准稳定设备 ID 清单（排除根盘、挂载盘与
现役 OSD，写盘用 /dev/disk/by-id 路径），首次写盘前打印各节点清单并与 §0 集群规划核对通过，
再按核准清单逐盘执行。

```bash
# 1. 按 §3.3 步骤 1 逐机核准并生成每台节点的核准清单（approved_disks_<node>.txt，
#    每行一个 /dev/disk/by-id/ 路径），打印并与 §0 集群规划逐项核对：
cat approved_disks_ceph1.txt approved_disks_ceph2.txt approved_disks_ceph3.txt
# 2. 在 ceph1 上创建脚本 create_osd.sh，内容见下方脚本块（按核准清单逐盘执行；
#    本脚本为分支 B 外置 DB/WAL 布局，j/k 为 NVMe 上 WAL/DB 分区号；
#    采用分支 A 布局时去掉 --block-wal/--block-db 参数）
cd /root/ceph-mycluster/
vi /root/ceph-mycluster/create_osd.sh
# 3. 保存退出后在 ceph1 上运行
bash create_osd.sh
```

脚本内容（approved_disks_${node}.txt 为 §3.3 步骤 1 核准通过并打印核对的设备清单文件）：

```bash
#!/bin/bash
set -eu
for node in ceph1 ceph2 ceph3
do
    j=1
    k=2
    # 只消费核准清单，每行一个 /dev/disk/by-id/ 稳定设备路径；清单外一概不写盘
    while read -r disk
    do
        [ -n "$disk" ] || continue
        ceph-deploy disk zap ${node} "${disk}"
        ceph-deploy osd create ${node} --data "${disk}" --block-wal /dev/nvme0n1p${j} --block-db /dev/nvme0n1p${k}
        ((j=${j}+2))
        ((k=${k}+2))
        sleep 3
    done < approved_disks_${node}.txt
done
```

说明：此脚本只适用于当前硬件配置，其他硬件配置可参考修改（每台节点的核准清单须按 §3.3
步骤 1 独立核准；--data 为数据盘，--block-db 为 DB 分区，--block-wal 为 WAL 分区；
ceph-deploy/ceph-volume 支持 /dev/disk/by-id/ 路径，写盘走 by-id 可避免盘符漂移清错盘）。

### 6.4 如何快速创建多个块设备

假设 RBD 存储池 vdbench 中需创建 30 个块设备，每个 200GB：

```bash
# 1. 创建脚本
vi create_image.sh
# 2. 添加以下内容
#!/bin/bash
pool="vdbench"
size="204800"
createimages()
{
for image in {1..30}
do
rbd create image${image} --size ${size} --pool ${pool} --image-format 2 --image-feature layering
sleep 1
done
}
createimages
# 3. 执行脚本
bash create_image.sh
```

rbd 命令说明：--pool 指定存储池；--image-format 2 支持克隆（format 1 已弃用）；
--image-feature 指定 format 2 的特性启用/禁用。常用命令：

```bash
# 列出名字为 vdbench 的存储池中的镜像
rbd ls --pool vdbench
# 删除名字为 vdbench 的存储池中的名字为 image2 的镜像
rbd rm vdbench/image2
```

### 6.5 如何快速映射块设备

先按 §6.4 创建 30 个 image（每个 200GB）。在 ceph1 上创建脚本，将创建的 30 个 image 映射到
3 台客户端，每台 10 个 RBD：

```bash
# 1. 创建脚本
vi map_image.sh
# 2. 添加以下内容（rbd map 统一使用 §4.2 步骤4 签发的限权身份 client.rbd-app，
#    不得以 client.admin 映射）
#!/bin/bash
pool="vdbench"
mapimages()
{
for i in {1..10}
do
ssh client1 "rbd map ${pool}/image${i} --name client.rbd-app --keyring /etc/ceph/ceph.client.rbd-app.keyring"
done
for i in {11..20}
do
ssh client2 "rbd map ${pool}/image${i} --name client.rbd-app --keyring /etc/ceph/ceph.client.rbd-app.keyring"
done
for i in {21..30}
do
ssh client3 "rbd map ${pool}/image${i} --name client.rbd-app --keyring /etc/ceph/ceph.client.rbd-app.keyring"
done
}
mapimages
# 3. 执行脚本
bash map_image.sh
```

常用命令：

```bash
# 在客户端中，取消映射 vdbench 的存储池的 image 镜像
rbd unmap vdbench/image1
```

## 7. 故障排除（§4.7，共三则）

### 7.1 执行 ceph -s 提示 no module named xxx

- 现象：执行 `ceph -s` 提示 no module named xxx 错误。
- 解决：根据报错安装对应的依赖包，按 OS 矩阵选择 pip/pip3（CentOS 7.6 用 pip、
  openEuler 22.03 用 pip3）：
  `pip install xxx`（CentOS 7.6）/ `pip3 install xxx`（openEuler 22.03）。
  偏离标注：上游文档该节为 Ubuntu 示例（Python2 时代安装命令写法），本仓 OS 矩阵
  （CentOS 7.6 / openEuler 20.03&22.03）不含 Ubuntu，统一按矩阵使用 pip/pip3，
  不沿用上游的 Python2 专用安装命令。

### 7.2 执行 ceph -s 提示 auth: unable to find a keyring

- 现象：提示 Auth: unable to find a keyring。
- 根因：以非 root 用户运行 ceph 集群时，当前用户读权限不够导致 ceph 读取文件报错。
- 解决（以 ceph-adm 用户为例）：安装 acl 软件包，并以管理员上下文为预期用户签发只读
  CephX 身份 client.ro-status（mon allow r），setfacl 只授予该只读 keyring 读权限；
  不得对 client.admin keyring 授 ACL（client.admin 具备集群管理能力，仅状态查询不应持有，
  也不应扩大其文件读权限范围）——口径与已合入姊妹 skill `ceph-build` 的 build-guide.md
  §4.2 一致：

```bash
# 安装acl软件包（OS 矩阵为 CentOS 7.6/openEuler，用 yum；上游为 Ubuntu 示例写作 apt，此处按 OS 矩阵给出）
ceph-adm@mon:~/cluster$ sudo yum install acl -y
# 签发只读身份（以管理员身份执行：client.admin keyring 仅管理员持有，预期用户不接触）
ceph-adm@mon:~/cluster$ sudo ceph auth get-or-create client.ro-status mon 'allow r' --name client.admin --keyring /etc/ceph/ceph.client.admin.keyring -o /etc/ceph/ceph.client.ro-status.keyring
# 设置ceph-adm给只读身份keyring读权限
ceph-adm@ceph1:~/cluster$ sudo setfacl -m u:ceph-adm:r /etc/ceph/ceph.client.ro-status.keyring
```

状态查询使用该身份：`ceph -s --name client.ro-status --keyring /etc/ceph/ceph.client.ro-status.keyring`；
仅预期用户可读该 keyring 文件。

权限回收（排障结束后执行，避免长期悬挂的额外 CephX 身份）：

```bash
ceph-adm@ceph1:~/cluster$ sudo setfacl -x u:ceph-adm /etc/ceph/ceph.client.ro-status.keyring
ceph-adm@mon:~/cluster$ sudo ceph auth rm client.ro-status
```

### 7.3 提示 Error initializing cluster client: InvalidArgumentError('RADOS invalid argument (error calling conf_read_file)')

- 现象：执行 `ceph -s` 提示 Error initializing cluster client: InvalidArgumentError('RADOS
  invalid argument (error calling conf_read_file)')。
- 解决：确认在正确的用户和正确的 ceph 集群目录下运行 `ceph -s`（如 root 用户 + 集群目录
  /root/ceph-mycluster/），其他 ceph 集群相关操作命令同理。
