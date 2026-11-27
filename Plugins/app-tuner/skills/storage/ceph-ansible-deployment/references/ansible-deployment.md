# ceph-ansible 部署指南（CentOS 7.6 & openEuler 20.03）

> 来源：《鲲鹏 BoostKit 分布式存储使能套件 Ceph 编译&部署&调优&测试指南》（文档版本 07，
> 2025-06-30）第 3 章「Ceph-ansible 部署指南（CentOS 7.6&openEuler 20.03）」全文蒸馏
> （§3.2 环境要求 / §3.3 配置部署环境 / §3.4 安装 ceph-ansible / §3.5 配置块存储 /
> §3.6 配置文件存储 / §3.7 配置对象存储 / §3.8 部署集群 / §3.9 集群扩容 / §3.10 删除集群 /
> §3.11 repo 源压缩包制作）。本文件按 SOP 步骤组织，原文命令与 yml 片段逐字保留；TLS 校验、
> 供应链验签（https 下载、rpm 签名校验与本地 repo gpgcheck/repo_gpgcheck）、代理凭据、
> repo 目录统一与 YAML 结构修正为本仓口径补充（见各处「安全说明」「修正说明」）。
> 不同 OS 下操作若有不同会明确标注；未标注则在 CentOS 7.6 与 openEuler 20.03 下一致。

## 0. 版本与适用矩阵

ceph-ansible 是用于部署（deployment 编排）Ceph 分布式系统的 Ansible 脚本；Ansible 基于模块工作，
本身没有批量部署能力，真正具有批量部署能力的是其所运行的模块。

| 项 | 版本 |
|---|---|
| ceph-ansible | stable-4.0（建议版本） |
| Ansible | 2.9.0 |
| Ceph | 14.2.22 Nautilus |
| OS | CentOS Linux release 7.6.1810；openEuler 20.03 SP1 |
| 硬件 | 鲲鹏服务器：鲲鹏920 5220 处理器，2*32核 2600MHz，内存 12*16GB 2666MHz，IN200 网卡 4*25GE；系统盘 RAID1（2*960GB SATA SSD）；数据盘 RAID 模式下使能 JBOD（12*4TB SATA HDD）；NVMe SSD 1*ES3000 V5 3.2TB；RAID 卡 Avago SAS 3508 |

集群规划：服务端 ceph1~ceph3，客户端 client1~client3（管理 IP 192.0.2.x、Public Network
198.51.100.x、Cluster Network 203.0.113.x；示例网段采用 RFC 5737 文档保留地址，按现场规划
替换）。管理 IP 用于远程 SSH 管理；Cluster Network 用于
集群内部节点间同步数据；Public Network 供其他节点访问；客户端当压力机时业务口 IP 与集群
Public Network 同网段。Ceph 客户端和服务端都需安装 Ceph 软件（编译安装见 skill `ceph-build`）。

## 1. 配置部署环境（§3.3）

### 1.1 关闭 SMMU

部署 Ceph 前关闭 SMMU，以防其对 Ceph 性能造成影响：进入 BIOS（参见《TaiShan 服务器 BIOS 参数
参考（鲲鹏920处理器）》），进入 Advanced->MISC Config 配置页面，将 Support Smmu 设置为
Disabled，按 F10 保存退出。

### 1.2 配置主机名

步骤 1 配置永久静态主机名（服务端 ceph1~ceph3，客户机 client1~client3，其余节点以此类推）：

```bash
hostnamectl --static set-hostname ceph1
hostnamectl --static set-hostname client1
```

步骤 2 修改域名解析文件：在各个集群和客户端节点的 `/etc/hosts` 中添加如下内容：

```text
198.51.100.166   ceph1
198.51.100.167   ceph2
198.51.100.168   ceph3
198.51.100.160   client1
198.51.100.161   client2
198.51.100.162   client3
```

### 1.3 配置免密登录

需配置 ceph1 节点对所有主/客户机节点的免密（包括 ceph1 本身），以及 client1 节点对所有
主/客户机节点的免密（包括 client1 本身）。

步骤 1 在 ceph1 节点生成公钥，并发放到各个主机/客户机节点（`ssh-keygen -t rsa` 后按回车使用
默认配置）：

```bash
ssh-keygen -t rsa
for i in {1..3}; do ssh-copy-id ceph$i; done
for i in {1..3}; do ssh-copy-id client$i; done
```

步骤 2 在 client1 节点执行同样两步（`ssh-keygen -t rsa` + 两条 ssh-copy-id 循环）。

### 1.4 关闭防火墙

在所有 Ceph 节点和 Client 节点依次执行：

```bash
systemctl stop firewalld
systemctl disable firewalld
systemctl status firewalld
```

### 1.5 设置 permissive 模式

在所有主客户机节点执行：

```bash
# 临时修改，下次重启自动失效
setenforce permissive
```

永久设置：编辑 `/etc/selinux/config`，修改 `SELINUX=permissive`（下次重启自动生效）。

### 1.6 配置 repo 源

提供在线和离线两种方式，推荐使用在线方式。

方法一（在线）：步骤 1 在所有集群和客户端节点建立 `/etc/yum.repos.d/ceph.repo`，加入：

```ini
[Ceph]
name=Ceph packages for $basearch
baseurl=http://download.ceph.com/rpm-nautilus/el7/$basearch
enabled=1
gpgcheck=1
type=rpm-md
gpgkey=https://download.ceph.com/keys/release.asc
priority=1

[Ceph-noarch]
name=Ceph noarch packages
baseurl=http://download.ceph.com/rpm-nautilus/el7/noarch
enabled=1
gpgcheck=1
type=rpm-md
gpgkey=https://download.ceph.com/keys/release.asc
priority=1

[ceph-source]
name=Ceph source packages
baseurl=http://download.ceph.com/rpm-nautilus/el7/SRPMS
enabled=1
gpgcheck=1
type=rpm-md
gpgkey=https://download.ceph.com/keys/release.asc
priority=1
```

步骤 2 更新 yum 源：

```bash
yum clean all && yum makecache
```

步骤 3 安装 epel 源（openEuler 系统下无需操作此步骤，请跳过）：

```bash
yum -y install epel-release
```

步骤 4 配置临时代理（支持相关依赖包的安装）：代理凭据只保存在安装账号家目录下 600 权限的
临时 env 文件中，source 后使用，不得写入 /etc/environment（全局可读且持久化）：

```bash
cat > /root/.ceph_install_proxy.env <<'EOF'
export http_proxy=http://{Proxy-User-Name}:{Proxy-Password}@<Proxy-Server-IP-Address>:<Proxy-Port>
export https_proxy=http://{Proxy-User-Name}:{Proxy-Password}@<Proxy-Server-IP-Address>:<Proxy-Port>
export ftp_proxy=http://{Proxy-User-Name}:{Proxy-Password}@<Proxy-Server-IP-Address>:<Proxy-Port>
export no_proxy=127.0.0.1,localhost
EOF
chmod 600 /root/.ceph_install_proxy.env
source /root/.ceph_install_proxy.env
```

安全说明：安装结束后必须清理临时代理配置，删除临时文件并撤销当前会话的代理环境变量
（凭据不长期留存、不落全局配置文件）：

```bash
rm -f /root/.ceph_install_proxy.env
unset http_proxy https_proxy ftp_proxy
```

方法二（离线）：集群所有节点均需配置。local_source.zip 包需手动制作（见 §9 repo 源压缩包制作，
§9 已将 reposync/createrepo 产物归一到 /home/local_source 并打包）。

```bash
cd /home                      # 步骤1 将 local_source.zip 传到断网节点 /home 后进入该目录
unzip local_source.zip        # 步骤2 解压到与 §9 打包一致的同一目录 /home/local_source
ls /home/local_source         # 步骤2 回显核对解包目录确为 /home/local_source 且产物齐全（CentOS 为
                              #        EPEL/、CentOS-Base/、ceph/、gpgkey/；openEuler 为 ceph/、
                              #        gpgkey/；各目录下 repodata/ 已由 §9 生成，无需在断网节点
                              #        重装 createrepo 重跑 createrepo）
cp /home/local_source/gpgkey/* /etc/pki/rpm-gpg/   # 步骤3 安装随包分发的各源 GPG key（供下方
                                                   #        repo 文件 gpgkey 与 gpgcheck/repo_gpgcheck
                                                   #        验签使用，key 文件名与 §9 收集的一致）
mkdir -p /etc/yum.repos.d/bak          # 步骤4 备份移除系统自带 repo 文件（保留原件用于回退还原）
mv /etc/yum.repos.d/*.repo /etc/yum.repos.d/bak/
vi /etc/yum.repos.d/local.repo         # 步骤5 repo 文件目录为 /etc/yum.repos.d，不得写到 home 下
                                       #        的 yum.repo.d 目录
yum clean all && yum makecache         # 步骤6 刷新缓存，验证本地源可用
```

local.repo 内容（baseurl 指向 /home/local_source 下实际产物路径）。

CentOS 7.6：

```ini
[local-epel]
name=local epel
baseurl=file:///home/local_source/EPEL
enabled=1
gpgcheck=1
repo_gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-EPEL-7
priority=1

[local-centos-base]
name=local centos base
baseurl=file:///home/local_source/CentOS-Base
enabled=1
gpgcheck=1
repo_gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-CentOS-7
priority=1

[local-ceph]
name=local ceph
baseurl=file:///home/local_source/ceph
enabled=1
gpgcheck=1
repo_gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-ceph
priority=1
```

openEuler 20.03：

```ini
[local-ceph]
name=local ceph
baseurl=file:///home/local_source/ceph
enabled=1
gpgcheck=1
repo_gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-ceph
```

说明：repo 文件一律写入 /etc/yum.repos.d/；本地 repo 一律 gpgcheck=1 + repo_gpgcheck=1（SKILL.md
铁律 7，禁止关闭 gpg 校验的本地 repo 直接安装），gpgkey 指向步骤 3 安装到 /etc/pki/rpm-gpg/ 的
随包分发 GPG key（key 文件名与 §9 步骤 4 收集的一致，示例名以现场实际 key 为准）；回退时从
/etc/yum.repos.d/bak/ 还原系统自带 repo 文件。`yum makecache` 报错时先核对 local_source.zip 解包
路径与 local.repo baseurl 是否一致；报 GPG 校验失败时核对 key 是否导入、repomd.xml.asc 签名是否
随包分发（§9 步骤 4）。

### 1.7 编译 python-enum34 软件包（仅 openEuler，CentOS 跳过）

以下操作需在所有节点进行。

步骤 1 下载源码及补丁：

```bash
git clone -b openEuler-20.03-LTS-SP1 https://gitee.com/src-openeuler/python-enum34.git
```

步骤 2 将源码包移动到 `/root/rpmbuild/SOURCES` 目录下：

```bash
mv python-enum34/enum34-1.1.6.tar.gz /root/rpmbuild/SOURCES
```

步骤 3 修改 spec 文件 `python-enum34/python-enum34.spec`：将第 42 行的
`%files -n python2-%{_name}` 改为 `%files -n python-%{_name}`。

步骤 4 编译软件包：

```bash
mkdir -p /root/rpmbuild/{SPECS,SOURCES}
mv python-enum34/python-enum34.spec /root/rpmbuild/SPECS/
rpmbuild -bb /root/rpmbuild/SPECS/python-enum34.spec
```

步骤 5 将编译好的 rpm 包移动到指定文件夹：

```bash
mkdir -p /home/rpm/local
cp -r /root/rpmbuild/RPMS/*  /home/rpm/local/
```

步骤 6 获取 liboath 软件包并制作 repo 源：

```bash
cd  /home/rpm/local
# 禁止明文 http:// 下载 rpm（SKILL.md 铁律 7）：改用 https 下载，下载后导入 GPG key 并做签名校验
wget https://mirrors.tools.huawei.com/openeuler/openEuler-21.03/everything/aarch64/Packages/liboath-2.6.5-2.oe1.aarch64.rpm
wget https://mirrors.tools.huawei.com/openeuler/openEuler-21.03/everything/aarch64/Packages/liboath-devel-2.6.5-2.oe1.aarch64.rpm
wget https://mirrors.tools.huawei.com/openeuler/openEuler-21.03/everything/aarch64/Packages/liboath-doc-2.6.5-2.oe1.noarch.rpm
# 导入 openEuler GPG key 到 /etc/pki/rpm-gpg/（key 路径以现场镜像源实际为准）后校验 rpm 签名
curl -o /etc/pki/rpm-gpg/RPM-GPG-KEY-openEuler-21.03 https://mirrors.tools.huawei.com/openeuler/openEuler-21.03/openEuler-21.03.gpg.key
rpm --import /etc/pki/rpm-gpg/RPM-GPG-KEY-openEuler-21.03
rpm -K liboath*.rpm   # 签名校验：回显含 digests signatures OK 方可继续，校验不过不得安装
yum install -y createrepo
cd  /home/rpm/local & createrepo .
gpg --detach-sign --yes repodata/repomd.xml   # 签名 repodata，供步骤7 repo_gpgcheck=1 验签（无内部签名 key 时按步骤7说明执行 sha256 最低要求）
```

步骤 7 配置 repo 文件：在 `/etc/yum.repos.d/local.repo` 加入：

```ini
[local-rpm]
name=local-rpm
baseurl=file:///home/rpm/local
enabled=1
gpgcheck=1
repo_gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-openEuler-21.03
priority=1
```

说明：本节 local-rpm 与 §1.6 各本地 repo 一律 gpgcheck=1 + repo_gpgcheck=1（SKILL.md 铁律 7，
禁止关闭 gpg 校验的本地 repo 直接安装），验签依赖已导入的 GPG key 与 repodata 签名（步骤 6 的
repomd.xml.asc）。无法用内部签名 key 生成 repomd.xml.asc 时的最低要求：记录 repodata/repomd.xml
的 sha256，与官方镜像对应 repomd.xml 校验和比对一致、结果记入台账后方可安装使用。

步骤 8 清理 yum 缓存：

```bash
yum clean all && yum makecache
```

## 2. 安装 ceph-ansible（§3.4）

说明：本文档中 ceph-ansible 的安装路径在 /home 下。安装 ceph-ansible 及其依赖均仅需在
ceph1 上操作；均支持在线/离线两种方式。

### 2.1 安装 ceph-ansible

在线方式：

```bash
# 步骤1 安装 Ansible
# CentOS 7.6
yum -y install ansible
# openEuler 20.03
pip3 install ansible==2.9
# 步骤2 安装 ceph-ansible
yum -y install git                        # 确保环境上安装了 git
# 保持 TLS 校验开启：内网自签名镜像源把内部 CA 装入系统信任库（不得全局关闭证书校验）
cp <internal-ca>.crt /etc/pki/ca-trust/source/anchors/
update-ca-trust extract
# 确需临时绕过时仅用命令级 -c 参数（仅单条命令生效，不写入全局配置）
git -c http.sslVerify=false clone -b stable-4.0 https://github.com/ceph/ceph-ansible.git --recursive
# 步骤3 克隆后核对版本并写入台账（commit/tag 记入台账；需固定版本时 checkout 指定 tag/commit 后再次记录）
cd /home/ceph-ansible
git log -1 --oneline
```

离线方式：

```bash
# CentOS 7.6
yum -y install ansible
# openEuler 20.03
pip3 install ansible
unzip /home/ceph-ansible-stable-4.0.zip   # 步骤2 解压 ceph-ansible
```

### 2.2 安装 ceph-ansible 依赖

在线方式：

```bash
yum install -y python3-pip      # 步骤1 安装 python3-pip
mkdir /home/ceph-ansible/       # 步骤2 创建并进入 ceph-ansible 目录
cd /home/ceph-ansible/
pip3 install -r requirements.txt   # 步骤3 检查并安装需要的软件版本
```

离线方式：

```bash
yum install -y python3-pip
mkdir /home/ceph-ansible/
cd /home/ceph-ansible/
yum install -y python-netaddr   # 步骤3 安装需要的依赖包
pip3 install -r requirements.txt
```

### 2.3 解决环境依赖问题

步骤 1 解决 rpm_key 依赖问题：进入
`/home/ceph-ansible/roles/ceph-common/tasks/installs/redhat_community_repository.yml`，
注释其中 rpm_key 相关代码。

步骤 2 解决 grafana 依赖问题：进入
`/home/ceph-ansible/roles/ceph-grafana/tasks/configure_grafana.yml`，注释其中相关代码。

### 2.4 创建服务节点配置列表

在 ceph-ansible 目录内新建 hosts 文件（`vi /home/ceph-ansible/hosts`），用于定义集群中的主机
及每个主机在 Ceph 集群中扮演的角色。参数列表：

| 参数 | 说明 |
|---|---|
| [mons] | 作为 Monitor 节点 |
| [mgrs] | 作为 MGR 节点 |
| [osds] | 作为 OSD 节点，具体 OSD 配置在 osds.yml 文件中配置 |
| [mdss] | 作为 MDS 节点，文件存储中需要指定 |
| [rgws] | 作为 RGW 节点，对象存储中需要指定 |
| [clients] | 作为客户端节点 |
| [grafana-server] | 一般需要保留该参数，不然会报缺少的错误 |

### 2.5 修改 ceph-ansible 配置文件

部署时必须传递相应 playbook 给 ansible-playbook 命令。所有选项及默认配置放在 group_vars
目录下，每种 Ceph 进程对应相关配置文件：

```bash
cd /home/ceph-ansible/group_vars/
cp mons.yml.sample mons.yml
cp mgrs.yml.sample mgrs.yml
cp mdss.yml.sample mdss.yml
cp rgws.yml.sample rgws.yml
cp osds.yml.sample osds.yml
cp clients.yml.sample clients.yml
cp all.yml.sample all.yml
```

说明：all.yml.sample 是应用于集群所有主机的特殊配置文件。all.yml 中 `ceph_conf_overrides`
变量可用于覆盖 ceph.conf 已配置的选项或增加新选项（`vim all.yml` 添加）：

```yaml
ceph_conf_overrides:
  global:
    osd_pool_default_pg_num: 64
    osd_pool_default_pgp_num: 64
    osd_pool_default_size: 2
    mon_max_pg_per_osd: 3000
  mon:
    mon_allow_pool_create: true
```

修正说明：`mon_max_pg_per_osd` 属 global 段选项，必须作为 `ceph_conf_overrides.global` 的子键；
`mon_allow_pool_create` 属 mon 段选项，必须作为 `ceph_conf_overrides.mon` 的子键。写在
`ceph_conf_overrides` 之外的顶层键会被 ansible 当作普通变量丢弃，不会写入 ceph.conf。
部署后核对生成 ceph.conf 的方法：登录 mon 节点执行 `cat /etc/ceph/ceph.conf`，核对 [global]
段含 `mon_max_pg_per_osd`、[mon] 段含 `mon_allow_pool_create`；需查看运行期生效值时执行
`ceph config show`。

### 2.6 定义 OSD

osds.yml 支持两种方式设置 OSD 盘、WAL 盘和 DB 盘。

方法一：ceph-volume lvm batch 方式。修改 group_vars 下 osds.yml，按需指定 devices、
dedicated_devices 和 bluestore_wal_devices 中的设备：

- devices 表示系统中的数据盘，可与 osds_per_device 选项结合，设置每个磁盘上创建的 OSD 数量；
- dedicated_devices 即 block.db；bluestore_wal_devices 即 block.wal；
- dedicated_devices 和 bluestore_wal_devices 的磁盘不能为相同磁盘；
- 二者可以包含一个或多个磁盘；系统会根据 devices 磁盘数量与 osds_per_device，
  将 dedicated_devices 和 bluestore_wal_devices 下的磁盘进行均分。

须知：单个 OSD 的 block.db 大于等于 50GB，否则可能因 block.db 过小而安装报错。

方法二：ceph-volume 从逻辑卷中创建 OSD。修改 osds.yml 的 lvm_volumes 属性，支持
只指定数据盘 / 数据盘+WAL / 数据盘+WAL+DB / 数据盘+DB 四种组合。需事先在所有集群节点创建
对应逻辑卷：

```bash
# a. 创建对应的 vg（-s 指定创建逻辑卷的基本单位）
/usr/sbin/vgcreate --yes -s 1G ceph-data  /dev/sdd
# b. 基于 vg 创建 lv（在 ceph-data 上创建名称为 osd-data2、大小为 300G 的 lv）
/usr/sbin/lvcreate -l 300 -n osd-data2 ceph-data
# c. 确认 lv 创建情况
lvs
```

### 2.7 关闭 chrony（仅 openEuler，CentOS 跳过）

进入 `vim /home/ceph-ansible/roles/ceph-infra/tasks/setup_ntp.yml`，注释 chrony 相关内容。

## 3. 配置块存储（§3.5，仅需在主节点 ceph1 执行）

步骤 1 定义 Ceph 服务需求：块存储场景必须的服务需求包括 mons 和 osds。

```bash
cp /home/ceph-ansible/site.yml.sample /home/ceph-ansible/site.yml
vim /home/ceph-ansible/site.yml
```

步骤 2 定义集群主机服务：进入 `/home/ceph-ansible/hosts`，按需修改各节点拥有的应用。

步骤 3 定义 Ceph 集群配置：修改 `group_vars/all.yml`（配置 Ceph 下载方式与版本、基本网络信息、
OSD 类型）。

Ceph 在线下载方式：

```yaml
ceph_origin: repository
ceph_repository: community
ceph_mirror: http://download.ceph.com
ceph_stable_release: nautilus
ceph_stable_repo: "{{ ceph_mirror }}/rpm-{{ ceph_stable_release }}"
ceph_stable_redhat_distro: el7
monitor_interface: enp133s0
journal_size: 5120
public_network: 198.51.100.0/24
cluster_network: 198.51.100.0/24
osd_objectstore: bluestore
```

参数填写说明：monitor_interface 为网口设备号；public_network 为 Public Network 的 IP 地址和
掩码；cluster_network 为 Cluster Network 的 IP 地址和掩码。

Ceph 离线下载方式：

```yaml
ceph_origin: distro
ceph_repository: local
ceph_stable_release: nautilus
```

步骤 4 定义 OSD：参照 §2.6 定义所需 OSD 配置。至此块存储部署环境准备完成。

## 4. 配置文件存储（§3.6，仅需在主节点 ceph1 执行）

步骤 1 定义 Ceph 服务需求：

```bash
cp /home/ceph-ansible/site.yml.sample /home/ceph-ansible/site.yml
vim /home/ceph-ansible/site.yml
```

在 site.yml 中添加 mdss 选项。

步骤 2 定义集群主机服务：修改 hosts，为节点添加 [mdss] 角色。

步骤 3 定义 Ceph 集群配置：修改 `group_vars/all.yml`，查找 CephFS 关键字，修改配置如下。

Ceph 在线下载方式（共用段与 §3 相同：ceph_origin/ceph_repository/ceph_mirror/
ceph_stable_release/ceph_stable_repo/ceph_stable_redhat_distro/monitor_interface/
journal_size/public_network/cluster_network/osd_objectstore，此处仅列 CephFS 增量段）：

```yaml
# CEPHFS #
##########
cephfs: cephfs # name of the ceph filesystem
cephfs_data_pool:
    name: "{{ cephfs_data if cephfs_data is defined else 'cephfs_data' }}"
    pg_num: "{{ osd_pool_default_pg_num }}"
    pgp_num: "{{ osd_pool_default_pg_num }}"
    rule_name: "replicated_rule"
    type: 1
    #  erasure_profile: ""
    #  expected_num_objects: ""
    application: "cephfs"
    size: "{{ osd_pool_default_size }}"
    min_size: "{{ osd_pool_default_min_size }}"
cephfs_metadata_pool:
    name: "{{ cephfs_metadata if cephfs_metadata is defined else 'cephfs_metadata' }}"
    pg_num: "{{ osd_pool_default_pg_num }}"
    pgp_num: "{{ osd_pool_default_pg_num }}"
    rule_name: "replicated_rule"
    type: 1
    #  erasure_profile: ""
    #  expected_num_objects: ""
    application: "cephfs"
    size: "{{ osd_pool_default_size }}"
    min_size: "{{ osd_pool_default_min_size }}"
cephfs_pools:
    - "{{ cephfs_data_pool }}"
    - "{{ cephfs_metadata_pool }}"
```

说明：参数 monitor_interface 为 Public Network 的网口设备 ID。

Ceph 离线下载方式（同 §3 离线段）：

```yaml
ceph_origin: distro
ceph_repository: local
ceph_stable_release: nautilus
```

步骤 4 定义 OSD：参照 §2.6 定义相应 OSD。

## 5. 配置对象存储（§3.7，仅需在主节点 ceph1 执行）

步骤 1 定义 Ceph 服务需求：

```bash
cp /home/ceph-ansible/site.yml.sample /home/ceph-ansible/site.yml
vim /home/ceph-ansible/site.yml
```

在 hosts 选项下增加 rgws 的定义（组名必须缩进到 `- hosts:` 之下，为列表子项）：

```yaml
- hosts:
  - mons
  - osds
  #  - mdss
  - rgws
  #  - nfss
  #  - rbdmirrors
  - clients
  - mgrs
  #  - iscsigws
  #  - iscsi-gws # for backward compatibility only!
  #  - grafana-server
  #  - rgwloadbalancers
```

修正说明：`mons`/`osds`/`rgws` 等组名若与 `- hosts:` 顶格平级，YAML 会把它们解析成与 hosts
平级的顶层列表项，playbook 将无法按组下发任务，必须缩进两格作为 hosts 的列表子项（上例）。
修改 site.yml 后、正式部署前，先用文中 inventory（/home/ceph-ansible/hosts）做语法检查：

```bash
ansible-playbook -i /home/ceph-ansible/hosts /home/ceph-ansible/site.yml --syntax-check
```

步骤 2 定义集群主机服务（`vim /home/ceph-ansible/hosts`）：定义 RGW 服务所在节点
（本例为 ceph1、ceph2）：

```text
[mons]
ceph1
[mgrs]
ceph1
[osds]
ceph1
ceph2
#[mdss]
#ceph1
#ceph2
[rgws]
ceph1
ceph2
[clients]
client3
[grafana-server]
ceph1
```

步骤 3 定义 Ceph 集群配置（`vim /home/ceph-ansible/group_vars/all.yml`）：设置前端网络类型、
端口、每个 RGW 节点的 RGW 实例数。

Ceph 在线下载方式（共用段同 §3）+ RGW 选项：

```yaml
## Rados Gateway options
radosgw_frontend_type: beast
radosgw_frontend_port: 12345
radosgw_interface: "{{monitor_interface}}"
radosgw_num_instances: 3
```

参数填写说明：monitor_interface 为 Public Network 的网口设备 ID；radosgw_frontend_type 为
RGW 前端类型（按需选填）；radosgw_frontend_port 为 RGW 前端端口（按需选填）；
radosgw_num_instances 为每个节点上 RGW 的实例个数，此时 RGW 的访问端口基于
radosgw_frontend_port 指定的端口号递增。

Ceph 离线下载方式（同 §3 离线段）。

步骤 4 设置 RGW 服务的权限：在 rgws.yml 中设置 RGW 默认数据池和索引池的 pg num 和 size，
以及 RGW 服务能否访问私有设备的权限。

CentOS 7.6：

```yaml
rgw_create_pools:
  defaults.rgw.buckets.data:
    pg_num: 8
    size: ""
  defaults.rgw.buckets.index:
    pg_num: 8
    size: ""
###########
# SYSTEMD #
###########
# ceph_rgw_systemd_overrides will override the systemd settings
# for the ceph-rgw services.
# For example,to set "PrivateDevices=false" you can specify:
ceph_rgw_systemd_overrides:
  Service:
    PrivateDevices: False
```

openEuler 20.03：

```yaml
rgw_create_pools:
  defaults.rgw.buckets.data:
      pg_num: 64
      size: 3
  defaults.rgw.buckets.index:
      pg_num: 64
      size: 3
###########
# SYSTEMD #
###########
# ceph_rgw_systemd_overrides will override the systemd settings
# for the ceph-rgw services.
# For example,to set "PrivateDevices=false" you can specify:
ceph_rgw_systemd_overrides:
  Service:
     PrivateDevices: False
```

核对：修改后逐项核对角色实际读取的两个池与 systemd 参数——`rgw_create_pools` 下
`defaults.rgw.buckets.data` 与 `defaults.rgw.buckets.index` 为两个池键，`pg_num`/`size` 是各自
池的子键（不得重复出现在顶层）；`ceph_rgw_systemd_overrides` 下为 `Service` 子键。可用 Python
对修改后的 all.yml 做解析校验（解析报错即缩进有误）：

```bash
python -c "import yaml,sys;yaml.safe_load(open('all.yml'))"
```

步骤 5 定义 OSD：参照 §2.6 定义相应 OSD。

## 6. 部署 Ceph 集群（§3.8）

```bash
# 步骤1 安装 prettytable
pip install prettytable
# 步骤2 部署集群（执行结束所有节点显示 failed=0 则处于部署过程中/部署完成）
ansible-playbook -i hosts site.yml
# 步骤3 查看集群健康状态，显示 HEALTH_OK 则集群状态健康
ceph health
```

步骤 4 ceph-ansible 部署 Ceph 集群后检查 ceph.conf 配置是否成功：配置皆写入 ceph.conf 则
部署成功。

## 7. 集群扩容（§3.9）

### 7.1 添加 Ceph Monitor

前提条件：通过 Ansible 部署的正常运行的 Ceph 集群；在新节点上具有 root 权限。

```bash
# 步骤1 在集群的 hosts 文件的 [mons] 群组下添加新的 Ceph Monitor 节点
# 步骤2 确认 Ansible 可以连接到节点
ansible all -i hosts -m ping
# 步骤3 运行脚本
# 方法一：切换到 Ansible 主目录运行 site.yml
ansible-playbook -i hosts site.yml
# 方法二：拷贝 infrastructure-playbooks/add-mon.yml 到主目录下运行
cp ./infrastructure-playbooks/add-mon.yml ./add-mon.yml
ansible-playbook -i hosts add-mon.yml
```

### 7.2 添加 OSD 节点

具有相同的磁盘拓扑（前提：正常运行的集群、新节点 root 权限、新节点数据盘数量与其他 OSD 节点
相同）：

```bash
# 步骤1 复制 add-osd.yml 到主目录
cp ./infrastructure-playbooks/add-osd.yml ./add-osd.yml
# 步骤2 在集群的 hosts 文件的 [osds] 群组下增加新的 OSD 节点名
# 步骤3 确认 Ansible 可以连接到节点
ansible all -i hosts -m ping
# 步骤4 运行脚本（inventory 与前一步核对过的清单一致，用 hosts）
ansible-playbook -i hosts add-osd.yml
```

具有不同的磁盘拓扑（前提：正常运行的集群、新节点 root 权限）：在 hosts 的 [osds] 群组下增加
新 OSD 节点名，并在新增 OSD 节点名后写入需要的 devices 信息；`ansible all -i hosts -m ping`
确认连通；`cp ./infrastructure-playbooks/add-osd.yml ./add-osd.yml`；运行
`ansible-playbook -i hosts add-osd.yml`（inventory 统一用 hosts，与新增节点时的清单一致）。

### 7.3 添加 MDS 群组成员

前提条件：通过 Ansible 部署的正常工作的 Ceph 集群；有安装了 Ansible 的管理节点。

```bash
# 步骤1 在集群 hosts 文件的 [mdss] 群组中添加新的 MDS 节点名
# 步骤2 修改主目录下 site.yml 脚本里的 mdss 配置，将注释符删除
# 步骤3 在主目录下运行脚本
ansible-playbook -i hosts site.yml
```

## 8. 删除集群（§3.10，仅在 ceph1 执行）

破坏性操作：执行前与用户二次确认（SKILL.md 铁律 5）。

```bash
# 步骤1 执行删除命令
ansible-playbook -i hosts infrastructure-playbooks/purge-cluster.yml
# 步骤2 输入 yes 确认删除
Are you sure you want to purge the cluster? [no]:yes
```

## 9. repo 源压缩包制作（§3.11.1）

CentOS 7.6：

```bash
# 步骤1 通过 reposync 批量下载镜像源文件到主机（不指定路径则保存在当前目录）
reposync -r epel -p /opt/EPEL
reposync -r CentOS-Base -p /opt/CentOS-Base
reposync -r ceph -p /opt/ceph
# 步骤2 分别在各目录下生成 repodata 文件
yum install createrepo
cd /opt/EPEL && createrepo .
cd /opt/CentOS-Base && createrepo .
cd /opt/ceph && createrepo .
# 步骤3 将 reposync/createrepo 产物归一到 /home/local_source（与断网节点解包目录一致）
mkdir -p /home/local_source
cp -a /opt/EPEL /opt/CentOS-Base /opt/ceph /home/local_source/
# 步骤4 收集各源 GPG key 并签名 repodata（随包分发，供断网节点 gpgcheck=1/repo_gpgcheck=1 验签，
#       key 文件名与 §1.6 local.repo 的 gpgkey 一致）
mkdir -p /home/local_source/gpgkey
cp /etc/pki/rpm-gpg/RPM-GPG-KEY-* /home/local_source/gpgkey/
gpg --detach-sign --yes /home/local_source/EPEL/repodata/repomd.xml
gpg --detach-sign --yes /home/local_source/CentOS-Base/repodata/repomd.xml
gpg --detach-sign --yes /home/local_source/ceph/repodata/repomd.xml
# 步骤5 打包（产物统一为 local_source.zip，解包后目录结构与本地一致）
cd /home
zip -r local_source.zip local_source
# 步骤6 通过刻盘或移动存储介质将 local_source.zip 转存到本地，搭建本地镜像库（见 §1.6 方法二）
```

openEuler 20.03：

```bash
# 步骤1 批量下载 ceph.repo 镜像源中的所有文件
reposync -r ceph -p /opt/ceph
# 步骤2 生成 repodata 文件
yum  install -y createrepo
cd /opt/ceph
createrepo  .
# 步骤3 将 reposync/createrepo 产物归一到 /home/local_source（与断网节点解包目录一致）
mkdir -p /home/local_source
cp -a /opt/ceph /home/local_source/
# 步骤4 收集 GPG key 并签名 repodata（随包分发，供断网节点 gpgcheck=1/repo_gpgcheck=1 验签）
mkdir -p /home/local_source/gpgkey
cp /etc/pki/rpm-gpg/RPM-GPG-KEY-* /home/local_source/gpgkey/
gpg --detach-sign --yes /home/local_source/ceph/repodata/repomd.xml
# 步骤5 打包（产物统一为 local_source.zip）
cd /home
zip -r local_source.zip local_source
# 步骤6 通过刻盘或移动存储介质将 local_source.zip 转存至本地、搭建本地镜像库（见 §1.6 方法二）
```

说明：无法用内部签名 key 生成 repomd.xml.asc 时的最低要求：记录 /home/local_source 下各
repodata/repomd.xml 的 sha256，与官方镜像对应 repomd.xml 校验和比对一致、结果记入台账后方可
分发安装（SKILL.md 铁律 7）。
