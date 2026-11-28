# Ceph 14.2.8 编译指南（CentOS 7.6 & openEuler 20.03 & openEuler 22.03）

> 来源：《鲲鹏 BoostKit 分布式存储使能套件 Ceph 编译&部署&调优&测试指南》（文档版本 07）
> （2025-06-30）第 1 章「Ceph 14.2.8 编译指南（CentOS 7.6&openEuler 20.03&openEuler 22.03）」
> 全文蒸馏（§1.2 环境要求 / §1.3 配置编译环境 / §1.4.1 编译 Ceph / §1.4.2 部署集群 / §1.5 故障排除）。
> 本文件按 SOP 步骤组织，每步标注适用 OS/版本；命令与配置逐字保留（PDF 换行断词已复原为完整命令）。

## 0. 版本与适用矩阵

| 项 | 版本 |
|---|---|
| Ceph | 14.2.8 Nautilus（其他 Ceph 版本可参考本文档进行编译操作） |
| OS | CentOS Linux release 7.6.1810；openEuler 20.03 LTS SP1（Kernel: 4.19.90-2012.4.0.0053.oe1）；openEuler 22.03 -LTS-SP2（Kernel: 5.10.0-153.12.0.92.oe2203sp2） |
| 硬件 | 鲲鹏服务器：鲲鹏920 5220 处理器，2*32核 2600MHz，内存 8*16GB 2933MHz，IN200 网卡 4*25GE；系统盘 RAID 1（2*960GB SATA SSD）；数据盘 RAID 模式下使能 JBOD（12*4TB SATA HDD）；NVMe SSD 缓存盘 1*ES3000 V5 3.2TB；RAID 控制卡 Avago SAS 3508 |

说明：

- 安装过程中需要在线安装 Ceph 软件包及依赖包，请确保服务器可以接入互联网。
- 全新安装操作系统时，建议使用最小化安装。
- 集群由 Ceph 客户端（client1~client3）和 Ceph 服务端（ceph1~ceph3）组成；管理 IP 用于远程 SSH
  管理，Cluster Network 用于集群内部节点间数据同步，Public Network 供其他节点访问，均选取任意
  一个 25GE 网口配置；客户端当压力机时，其业务口 IP 需与集群 Public Network 同网段。
  Ceph 客户端和服务端都需安装 Ceph 软件。

硬盘划分（BlueStore）：Ceph 14.2.8 起后端存储引擎为 BlueStore，无 Jewel 版 Journal 盘分区，
改为 DB 分区（元数据）与 WAL 分区。每节点 12 块 4TB 数据盘（每块作为一个 OSD 的数据分区）+
1 块 3.2TB NVMe 盘（作为 12 个 OSD 的 DB、WAL 分区）。WAL 分区大于 10GB 即够用；官方建议每个
DB 分区不小于数据盘容量的 4%。本方案以 WAL 60GB、DB 180GB 为例：单个 OSD = 4TB 数据盘 +
180GB DB 分区 + 60GB WAL 分区。

## 1. 配置编译环境（§1.3）

### 1.1 CentOS 7.6

步骤 1 安装 SCL 软件集。

```bash
yum -y install centos-release-scl
```

步骤 2 安装依赖。

```bash
yum -y install epel-release
yum -y install python36-scipy.aarch64
```

步骤 3 调整 yum.conf（仅 deltarpm）。

1. 打开 `/etc/yum.conf`；
2. 在文件末尾添加 `deltarpm=0`；
3. 保存退出。

说明：保持 TLS 证书校验开启（禁止为联网安装全局关闭 yum 的 sslverify 证书校验）；内网自签名镜像源
把内部 CA 装入系统信任库：

```bash
cp <internal-ca>.crt /etc/pki/ca-trust/source/anchors/
update-ca-trust extract
```

步骤 4 安装依赖（一条完整命令，勿拆分）。

```bash
yum install java-devel sharutils checkpolicy selinux-policy-devel gperf cryptsetup fuse-devel gperftools-devel libaio-devel libblkid-devel libcurl-devel libudev-devel libxml2-devel libuuid-devel ncurses-devel valgrind-devel xfsprogs-devel xmlstarlet yasm nss-devel libibverbs-devel openldap-devel CUnit-devel python36-Cython python36-setuptools python-prettytable lttng-ust-devel expat-devel junit boost-random keyutils-libs-devel openssl-devel libcap-ng-devel python3-sphinx leveldb leveldb-devel snappy snappy-devel lz4 lz4-devel liboath liboath-devel libbabeltrace-devel librabbitmq librabbitmq-devel librdkafka librdkafka-devel libnl3 libnl3-devel rdma-core-devel numactl numactl-devel numactl-libs createrepo openldap-devel rdma-core-devel lz4-devel expat-devel lttng-ust-devel libbabeltrace-devel gperftools-devel bc dnf-plugins-core librabbitmq-devel rpm-build java-1.8.0-openjdk-devel python36-devel rpmdevtools cmake3 gcc-c++ libtool leveldb-devel devtoolset-8-gcc-c++ liboath-devel python-devel xmlstarlet yasm CUnit-devel redhat-lsb-core python-sphinx lttng-ust-devel libffi-devel python36-pip -y
```

说明：CentOS 7.6.1810 的 base/updates 无 python3-* 命名（python3-* 自 7.7 起进入 base），本命令统一使用 EPEL 的 python36-* 实名（python36-setuptools/python36-devel/python36-pip，Cython 亦为 python36 前缀实名）；python36-pip 与 python36-setuptools 在 EPEL 最终归档中已退役，需 2019 时点 EPEL 快照源，或改用 SCL rh-python36 对应包。yum install 遇到无法解析的包名会整事务失败（Unable to find a match），不得凭经验改包名，以实际仓库检索为准。

### 1.2 openEuler 20.03

步骤 1 下载 openEuler-20.03-LTS-SP1 系统镜像，上传 iso 到 `/root`，挂载本地镜像源。

```bash
mount /root/openEuler-20.03-LTS-SP1-everything-aarch64-dvd.iso /media/
```

步骤 2 配置 repo 源。新建 `/etc/yum.repos.d/local.repo`，添加以下内容：（写入前备份：目标文件已存在时先 `cp -n /etc/yum.repos.d/local.repo{,.bak.<run_id>}` 记录原件，不存在则记录「新建」状态）

```ini
[local]
name=system local
baseurl=file:///media/
enabled=1
gpgcheck=0
```

步骤 3 生成本地 Yum 缓存。

```bash
yum clean all && yum makecache
```

步骤 4 安装依赖（一条完整命令，勿拆分）。

```bash
yum install java-devel sharutils checkpolicy selinux-policy-devel gperf cryptsetup fuse-devel gperftools-devel libaio-devel libblkid-devel libcurl-devel libudev-devel libxml2-devel libuuid-devel ncurses-devel valgrind-devel xfsprogs-devel xmlstarlet yasm nss-devel libibverbs-devel openldap-devel CUnit-devel python3-Cython python3-setuptools python-prettytable lttng-ust-devel expat-devel junit boost-random keyutils-libs-devel openssl-devel libcap-ng-devel python3-sphinx leveldb leveldb-devel snappy snappy-devel lz4 lz4-devel libbabeltrace-devel librabbitmq librabbitmq-devel librdkafka librdkafka-devel libnl3 libnl3-devel rdma-core-devel numactl numactl-devel numactl-libs createrepo openldap-devel rdma-core-devel lz4-devel expat-devel lttng-ust-devel libbabeltrace-devel python2-Cython gperftools-devel bc dnf-plugins-core librabbitmq-devel rpm-build java-1.8.0-openjdk-devel python3-devel openeuler-lsb rpmdevtools cmake gcc-c++ libtool -y
```

说明：oath 系列依赖（运行库与开发包）不在本步安装（基础源不含这两个包）；待 §2.2 的 oath-toolkit
本地源（local-oath）构建并启用后，再按「再编译 Ceph」步骤 4 安装，避免首次 yum 事务因缺包失败。

### 1.3 openEuler 22.03

步骤 1 下载 openEuler-22.03-LTS-SP2 系统镜像，上传 iso 到 `/root`，挂载本地镜像源。

```bash
mount /root/openEuler-22.03-LTS-SP2-everything-aarch64-dvd.iso /media/
```

步骤 2 配置 repo 源。新建 `/etc/yum.repos.d/local.repo`，内容与 openEuler 20.03 完全相同
（`[local]` / `baseurl=file:///media/` / `enabled=1` / `gpgcheck=0`）。写入前同样记录备份/新建状态（同 §1.2 步骤 2）。

步骤 3 生成本地 Yum 缓存。

```bash
yum clean all && yum makecache
```

步骤 4 安装依赖（与 openEuler 20.03 的差异：Cython 包为 `python3-Cython`，其余一致）。

```bash
yum install java-devel sharutils checkpolicy selinux-policy-devel gperf cryptsetup fuse-devel gperftools-devel libaio-devel libblkid-devel libcurl-devel libudev-devel libxml2-devel libuuid-devel ncurses-devel valgrind-devel xfsprogs-devel xmlstarlet yasm nss-devel libibverbs-devel openldap-devel CUnit-devel python3-Cython python3-setuptools python-prettytable lttng-ust-devel expat-devel junit boost-random keyutils-libs-devel openssl-devel libcap-ng-devel python3-sphinx leveldb leveldb-devel snappy snappy-devel lz4 lz4-devel liboath liboath-devel libbabeltrace-devel librabbitmq librabbitmq-devel librdkafka librdkafka-devel libnl3 libnl3-devel rdma-core-devel numactl numactl-devel numactl-libs createrepo openldap-devel rdma-core-devel lz4-devel expat-devel lttng-ust-devel libbabeltrace-devel python3-Cython gperftools-devel bc dnf-plugins-core librabbitmq-devel rpm-build java-1.8.0-openjdk-devel python3-devel openeuler-lsb rpmdevtools cmake gcc-c++ libtool -y
```

## 2. 编译 Ceph（§1.4.1）

### 2.1 CentOS 7.6

步骤 1 获取 Ceph 14.2.8 源码包并放入 `/home` 目录解压。

```bash
wget https://download.ceph.com/tarballs/ceph-14.2.8.tar.gz -O /home/ceph-14.2.8.tar.gz
sha256sum /home/ceph-14.2.8.tar.gz
```

说明：源码包须从 Ceph 官方发布渠道获取，`sha256sum` 结果与上游发布页 checksums 记录核对一致后
写入台账（与 ceph.spec Source0 同源）；不得使用来路不明的源码包。openEuler 20.03/22.03 路径的
源码包获取与校验同本步（三路径共用）。

```bash
cd /home && tar -zxvf ceph-14.2.8.tar.gz
```

步骤 2 修改 dashboard 的 requirements.txt：打开
`/home/ceph-14.2.8/src/pybind/mgr/dashboard/requirements.txt`，注释 `pyopenssl`，保存退出。

步骤 3 Ceph 14.2.8 源码自带的 isa-l 版本较低，升级 isa-l：

```bash
cd /home/ceph-14.2.8/src
mv isa-l isa-l.bak
git clone https://github.com/intel/isa-l.git
```

升级成功后，`/home/ceph-14.2.8/src/isa-l/erasure_code/` 目录下有 `aarch64` 目录。
版本核验：`git -C /home/ceph-14.2.8/src/isa-l log -1 --oneline` 记录 isa-l commit 号进台账。

步骤 4 安装补丁。

```bash
cd /home && wget https://github.com/ceph/ceph/commit/53040e4c0e9e86710b8800dbb7ea15b3fa196ebf.patch
cd /home/ceph-14.2.8
patch -p1 < ../53040e4c0e9e86710b8800dbb7ea15b3fa196ebf.patch
```

步骤 5 将修改后的 Ceph 源码重新打包成 tar.bz2 格式。

```bash
cd /home && tar -jcvf ceph-14.2.8.tar.bz2 ceph-14.2.8
bzip2 -t ceph-14.2.8.tar.bz2
```

（`-j` 为 bzip2 格式，与 .tar.bz2 扩展名一致；`bzip2 -t` 核对源包完整性，避免 rpmbuild 解包失败。）

步骤 6 编译修改后的 Ceph。

```bash
rpmdev-setuptree
cp /home/ceph-14.2.8/ceph.spec /root/rpmbuild/SPECS/
cp /home/ceph-14.2.8.tar.bz2 /root/rpmbuild/SOURCES/
cd /root/rpmbuild/SPECS/
rpmbuild -ba ceph.spec
```

### 2.2 openEuler 20.03

先编译 liboath-devel 软件包：

步骤 1 下载源码及补丁。

```bash
cd /home
git clone https://gitee.com/src-openeuler/oath-toolkit.git
cd oath-toolkit
git checkout 545d464c
```

说明：此步可能报错 `SSL certificate problem: self signed certificate in certificate chain`
（SSL 认证失败）。优先将内部 CA 装入系统信任库（同 §1.1 步骤 3 说明）后重试；确需临时绕过时用
命令级一次性配置 `git -c http.sslVerify=false ...`（仅对单条命令生效，不写入全局配置，见 SKILL.md
铁律 5）。

步骤 2 就位补丁与源码包（两文件缺任一即停止，不得凭空构造后继续）。

```bash
cd /home/oath-toolkit
# 补丁：随 src-openeuler 打包仓库分发，确认 checkout 的 commit 树中存在
ls -l 0001-oath-toolkit-2.6.5-lockfile.patch
# 源码包：按 spec Source0（Savannah 官方发布区）获取并校验摘要后写入台账（该 tar.gz 通常已随 checkout 545d464c 树自带，树内已有时可跳过下载、直接校验）
wget https://download.savannah.gnu.org/releases/oath-toolkit/oath-toolkit-2.6.5.tar.gz -O /home/oath-toolkit/oath-toolkit-2.6.5.tar.gz
sha256sum /home/oath-toolkit/oath-toolkit-2.6.5.tar.gz
```

说明：补丁应随 src-openeuler 打包仓库（checkout 的 commit）自带，若该 commit 树中无此文件，从仓库
历史或上游 lockfile 修复提交重新导出同名补丁；源码包摘要与上游发布页核对一致后才执行下一步的
cp，两文件就位后 cp 目标才真实存在。

步骤 3 将 patch 文件和源码包移动到 `/root/rpmbuild/SOURCES` 目录下。

```bash
rpmdev-setuptree
cp /home/oath-toolkit/0001-oath-toolkit-2.6.5-lockfile.patch /home/oath-toolkit/oath-toolkit-2.6.5.tar.gz /root/rpmbuild/SOURCES
```

步骤 4 编译 RPM 包。

```bash
cd /home/oath-toolkit
rpmbuild -bb oath-toolkit.spec
```

步骤 5 将编译好的 RPM 包作为本地 Yum 源。

```bash
mkdir -p /home/rpm/oath
cp -r /root/rpmbuild/RPMS/*  /home/rpm/oath/
yum install -y createrepo
cd  /home/rpm/oath && createrepo .
```

步骤 6 配置 repo 文件：在 `/etc/yum.repos.d/local.repo` 末尾加入：

```ini
[local-oath]
name=local-oath
baseurl=file:///home/rpm/oath
enabled=1
gpgcheck=0
priority=1
```

步骤 7 刷新 Yum 缓存。

```bash
yum clean all && yum makecache
```

再编译 Ceph：

步骤 1 获取并校验 Ceph 源码包（同 §2.1 步骤 1：官方渠道下载 + sha256sum 核对），放入 `/home` 目录解压。

```bash
cd /home && tar -zxvf ceph-14.2.8.tar.gz
```

步骤 2 调整 `/etc/yum.conf`：末尾添加 `deltarpm=0`，保存退出（保持 TLS 校验开启，内网源装内部 CA，同 §1.1 步骤 3 说明；不全局关闭 sslverify 证书校验）。

步骤 3 升级 isa-l（与 CentOS 7.6 相同）：

```bash
cd /home/ceph-14.2.8/src
mv isa-l isa-l.bak
git clone https://github.com/intel/isa-l.git
ll /home/ceph-14.2.8/src/isa-l/erasure_code/
```

升级成功后，`/home/ceph-14.2.8/src/isa-l/erasure_code/` 目录下有 `aarch64` 目录。
版本核验：`git -C /home/ceph-14.2.8/src/isa-l log -1 --oneline` 记录 isa-l commit 号进台账。

步骤 4 安装 liboath 相关依赖。

```bash
yum install liboath liboath-devel -y
```

步骤 5 将修改后的 Ceph 源码重新打包成 tar.bz2 格式。

```bash
cd /home && tar -jcvf ceph-14.2.8.tar.bz2 ceph-14.2.8
bzip2 -t ceph-14.2.8.tar.bz2
```

（`-j` 为 bzip2 格式，与 .tar.bz2 扩展名一致；`bzip2 -t` 核对源包完整性，避免 rpmbuild 解包失败。）

步骤 6 编译修改后的 Ceph。

```bash
rpmdev-setuptree
cp ceph-14.2.8/ceph.spec /root/rpmbuild/SPECS/
cp ceph-14.2.8.tar.bz2 /root/rpmbuild/SOURCES/
cd /root/rpmbuild/SPECS/
rpmbuild -ba ceph.spec
```

### 2.3 openEuler 22.03

步骤 1 进入 `/home` 目录，获取并校验 Ceph 源码包（同 §2.1 步骤 1）后解压。

```bash
cd /home && tar -zxvf ceph-14.2.8.tar.gz
```

步骤 2 升级 isa-l（与 CentOS 7.6 / openEuler 20.03 相同）：

```bash
cd /home/ceph-14.2.8/src
mv isa-l isa-l.bak
git clone https://github.com/intel/isa-l.git
```

升级成功后，`/home/ceph-14.2.8/src/isa-l/erasure_code/` 目录下有 `aarch64` 目录；`git -C /home/ceph-14.2.8/src/isa-l log -1 --oneline` 记录 isa-l commit 号进台账。

步骤 3 在 Ceph 源码根目录，下载并应用 22.03_adaptation.patch 补丁适配 openEuler 22.03。

```bash
cd /home
wget https://gitee.com/kunpengcompute/ceph/raw/adpat-openeuler-22.03/patch/22.03_adaptation.patch -O /home/22.03_adaptation.patch
cd /home/ceph-14.2.8
patch -p1 --dry-run < ../22.03_adaptation.patch
patch -p1 < ../22.03_adaptation.patch
```

说明：应用前用 `patch -p1 --dry-run` 预检，应用后用 `git -C /home/ceph-14.2.8 status --short` 回显改动清单写入台账（路径与 §2.1 步骤 4 同款绝对路径写法，避免工作目录错位）。

步骤 4 将修改后的 Ceph 源码重新打包成 tar.bz2 格式。

```bash
cd /home && tar -jcvf ceph-14.2.8.tar.bz2 ceph-14.2.8
bzip2 -t ceph-14.2.8.tar.bz2
```

（`-j` 为 bzip2 格式，与 .tar.bz2 扩展名一致；`bzip2 -t` 核对源包完整性，避免 rpmbuild 解包失败。）

步骤 5 编译修改后的 Ceph。

```bash
rpmdev-setuptree
cp ceph-14.2.8/ceph.spec /root/rpmbuild/SPECS/
cp ceph-14.2.8.tar.bz2 /root/rpmbuild/SOURCES/
cd /root/rpmbuild/SPECS/
rpmbuild -ba ceph.spec
```

## 3. 编译产物安装与集群部署指引（§1.4.2）

前提条件：

- Python 版本：所有操作系统必须含有 Python3.x。
- 基础环境（镜像源、免密、NTP 服务等系统配置）属部署域职责，不在本 skill 范围；本 skill 前提仅要求 OS 矩阵匹配（§0）与依赖安装完成。
- 已安装依赖 pyOpenSSL、werkzeug 和 prettytable；未安装时：

```bash
pip3 install --upgrade pip
pip3 install  setuptools_rust
pip3 install pyOpenSSL werkzeug prettytable
```

- 已创建 Ceph 用户；未创建时优先确认 ceph RPM 安装脚本是否自动创建；确需手动创建，先核对名称与
  编号未被占用（`getent group ceph`、`getent passwd ceph`、`getent group 167`、`getent passwd 167`
  均无输出），再用系统账号管理命令创建（不追加固定编号，由系统自选 UID/GID 并回读记录进台账）：

```bash
groupadd --system ceph
useradd --system -g ceph -d /var/lib/ceph -s /sbin/nologin ceph
getent passwd ceph
```

部署 Ceph 集群：

步骤 1 拷贝编译完成的 RPM 包至 `/home/local_ceph` 目录。

```bash
mkdir /home/local_ceph
cp -r /root/rpmbuild/RPMS/*  /home/local_ceph/
```

步骤 2 创建本地源。

```bash
cd /home/local_ceph
createrepo .
```

新建 `/etc/yum.repos.d/local_ceph.repo`，添加如下内容：（本次新增文件，无需备份原件；回退时直接删除）

```ini
[local_ceph]
name=local_ceph
baseurl=file:///home/local_ceph
enabled=1
gpgcheck=0
```

步骤 3 生成 Yum 本地缓存。

```bash
yum clean all && yum makecache
```

步骤 4 安装相关依赖。

```bash
# openEuler 20.03 / 22.03：
yum install -y python-sphinx rdma-core-devel systemd-devel openldap-devel leveldb-devel snappy-devel lz4-devel curl-devel expat-devel liboath-devel lttng-ust-devel libbabeltrace-devel python3-Cython fuse-devel libnl3-devel libaio-devel gperf librabbitmq-devel gperftools libcap-ng-devel

# CentOS 7.6（python3-Cython → EPEL 的 python36-Cython，其余一致，见 §1.1 说明）：
yum install -y python-sphinx rdma-core-devel systemd-devel openldap-devel leveldb-devel snappy-devel lz4-devel curl-devel expat-devel liboath-devel lttng-ust-devel libbabeltrace-devel python36-Cython fuse-devel libnl3-devel libaio-devel gperf librabbitmq-devel gperftools libcap-ng-devel
```

说明：两套命令仅 Cython 包名不同（openEuler 20.03/22.03 用 python3-Cython，CentOS 7.6 用 EPEL 命名 python36-Cython，7.6.1810 base 无 python3-* 栈，同 §1.1 说明），其余包名完全一致。

步骤 5 查看是否安装成功（逐包核对安装结果）。

```bash
# openEuler 20.03 / 22.03：
rpm -q python-sphinx rdma-core-devel systemd-devel openldap-devel leveldb-devel snappy-devel lz4-devel curl-devel expat-devel liboath-devel lttng-ust-devel libbabeltrace-devel python3-Cython fuse-devel libnl3-devel libaio-devel gperf librabbitmq-devel gperftools libcap-ng-devel

# CentOS 7.6：
rpm -q python-sphinx rdma-core-devel systemd-devel openldap-devel leveldb-devel snappy-devel lz4-devel curl-devel expat-devel liboath-devel lttng-ust-devel libbabeltrace-devel python36-Cython fuse-devel libnl3-devel libaio-devel gperf librabbitmq-devel gperftools libcap-ng-devel
```

逐包回显「包名-版本」为已安装；回显「未安装」即缺项，先回到步骤 4 补装后再继续步骤 6
（`yum` 的 `repolist` 子命令只列软件源，不校验安装结果，不用于本步）。核对时用与安装路径对应的包名清单。

步骤 6 安装 Ceph 并核对包一致性与版本。

```bash
yum install -y ceph-14.2.8
rpm -q ceph --qf "%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH} %{VENDOR}\n"
ceph -v
```

核对：`rpm -q ceph` 显示的包 NEVRA 须与步骤 1 拷入 `/home/local_ceph` 的 ceph-14.2.8-* 产物一致
（RELEASE/VENDOR 与本机 rpmbuild 产物相同），并用 `yum list ceph` 核对包来自 local_ceph 源而非
其他启用源或预装残留；`ceph -v` 正确显示 ceph version 14.2.8 才算安装成功。

编译产物安装验证完成即达本 skill 边界。MON/MGR/OSD 部署由姊妹 skill 承接——
`ceph-ansible-deployment`（其矩阵为 Ceph 14.2.22、CentOS 7.6/openEuler 20.03，不覆盖本
skill 的 14.2.8 产物与 22.03 路径）或 `ceph-block-storage-deployment`（Ceph 14.2.8/16.2.7、
OS 含 openEuler 22.03；本 skill 14.2.8 自编译产物——尤其 22.03 编译路径——的部署承接
以该 skill 为主）（同批 PR #445/#446，同批交付）；不得在本 skill 流程内混跑部署动作。

须知（交付给后续部署环节的交接信息）：部署指南中配置的 Ceph 镜像源为 Ceph 官方镜像（不包含本次
编译新特性的 RPM 包），后续部署时需采用本地源方式配置本次编译的 RPM 包（即本节 local_ceph 源）。

## 4. 故障排除（§1.5，共三则）

### 4.1 执行 ceph -s 提示 no module named xxx

- 现象：执行 `ceph -s` 提示 no module named xxx 错误。
- 解决：根据报错安装对应的依赖包。

```bash
pip3 install xxx
```

说明：本 skill OS 矩阵均要求 Python3.x（§3 前提条件），使用 pip3，不使用 pip2。

### 4.2 执行 ceph -s 提示 auth: unable to find a keyring

- 现象：提示 Auth: unable to find a keyring。
- 根因：以非 root 用户运行 ceph 集群时，当前用户读权限不够导致 ceph 读取文件报错。
- 解决：安装 acl 软件包，并给预期用户配置只读 CephX 身份（不使用 client.admin keyring——其具备
  集群管理能力，仅执行状态查询不应持有）：

```bash
# 安装acl软件包
ceph-adm@mon:~/cluster$ sudo yum install acl -y
# 创建只读身份（以管理员身份在 mon 节点执行：client.admin keyring 仅管理员持有，预期用户不接触）
ceph-adm@mon:~/cluster$ sudo ceph auth get-or-create client.ro-status mon 'allow r' --name client.admin --keyring /etc/ceph/ceph.client.admin.keyring -o /etc/ceph/ceph.client.ro-status.keyring
# 设置ceph-adm给只读身份keyring读权限
ceph-adm@ceph1:~/cluster$ sudo setfacl -m u:ceph-adm:r /etc/ceph/ceph.client.ro-status.keyring
```

状态查询使用该身份：`ceph -s --name client.ro-status --keyring /etc/ceph/ceph.client.ro-status.keyring`；
仅预期用户可读该 keyring 文件。

权限回收（排查完成后）：

```bash
ceph-adm@ceph1:~/cluster$ sudo setfacl -x u:ceph-adm /etc/ceph/ceph.client.ro-status.keyring
ceph-adm@mon:~/cluster$ sudo ceph auth rm client.ro-status
```

只读身份仅授予 mon 读权限，排查结束应回收，避免长期悬挂的额外 CephX 身份。

### 4.3 提示 Error initializing cluster client: InvalidArgumentError('RADOS invalid argument (error calling conf_read_file)')

- 现象：执行 `ceph -s` 提示 Error initializing cluster client: InvalidArgumentError('RADOS
  invalid argument (error calling conf_read_file)')。
- 解决：确认在正确的用户和正确的 ceph 集群目录下运行 `ceph -s`。例如以 root 用户、集群目录为
  `/root/ceph-mycluster/`，则应以 root 用户进入 `/root/ceph-mycluster/` 目录再执行 `ceph -s`，
  其他 ceph 集群相关操作命令同理。
