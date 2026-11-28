# Ceph 16.2.7 安装指南（openEuler 22.03）

> 来源：《鲲鹏 BoostKit 分布式存储使能套件 Ceph 编译&部署&调优&测试指南》（文档版本 07，
> 2025-06-30）第 2 章「Ceph 16.2.7 安装指南（openEuler 22.03）」全文蒸馏
> （§2.2 环境要求 / §2.3 配置安装环境 / §2.4 Ceph 的安装）。
> 本文件按 SOP 步骤组织，原文命令与 repo 配置逐字保留；TLS/GPG 校验、ISO 摘要核对与回退
> 安全加固为本仓安全口径补充（见各处「安全说明」）。

## 0. 版本与适用矩阵

| 项 | 版本 |
|---|---|
| Ceph | 16.2.7 Pacific（其他版本安装也可参考本文档） |
| OS | openEuler 22.03 LTS SP4（Kernel: 5.10.0-216.0.0.115.oe2203sp4.aarch64） |
| 关键事实 | Ceph 16.2.7 已在 openEuler 22.03 版本中集成到系统镜像源中，使用系统镜像源自带包即可 |
| 硬件 | 鲲鹏服务器：鲲鹏920 5220 处理器，2*32核 2600MHz，内存 8*16GB 2933MHz，IN200 网卡 4*25GE；系统盘 RAID 1（2*960GB SATA SSD）；数据盘 RAID 模式下使能 JBOD（12*4TB SATA HDD）；NVMe SSD 缓存盘 1*ES3000 V5 3.2TB；RAID 控制卡 Avago SAS 3508 |

说明：安装过程中需要在线安装 Ceph 软件包及依赖包，请确保服务器可以接入互联网；
全新安装操作系统时，建议使用最小化安装。

## 1. 配置安装环境（§2.3）

基于本地源和在线源两种方式，使用 openEuler-22.03-LTS-SP4 操作系统配置 Ceph 16.2.7 的安装环境。
两种方式二选一。

### 1.1 基于本地源安装

步骤 1 备份原有 openEuler.repo 文件（保留首次备份，重复运行不覆盖）。

```bash
# cp -n：已有 openEuler.repo.bak 时不覆盖，保留首次备份原件
cp -n /etc/yum.repos.d/openEuler.repo /etc/yum.repos.d/openEuler.repo.bak
# 已有备份时先 diff 核实当前配置与首次原件的差异，再决定是否继续（差异写入台账）
diff -u /etc/yum.repos.d/openEuler.repo.bak /etc/yum.repos.d/openEuler.repo || true
RUN_ID=$(date +%Y%m%d%H%M%S)
mv /etc/yum.repos.d/openEuler.repo /etc/yum.repos.d/openEuler.repo.bak.$RUN_ID
```

安全说明：备份必须保留首次原件，回退时执行 `cp -p /etc/yum.repos.d/openEuler.repo.bak /etc/yum.repos.d/openEuler.repo` 恢复首次原件；不得让二次运行的备份覆盖首次备份。

步骤 2 下载 openEuler-22.03-LTS-SP4 系统镜像，上传 iso 到 `/root`，核对官方摘要后挂载本地镜像源。

```bash
# 先核对官方摘要再挂载：与官方发布页/镜像站公布的 sha256sum 逐字符对照，一致后写入台账
sha256sum /root/openEuler-22.03-LTS-SP4-everything-aarch64-dvd.iso
mount /root/openEuler-22.03-LTS-SP4-everything-aarch64-dvd.iso /media/
```

安全说明：ISO 未经官方摘要核对不得直接挂载安装；摘要值以官方发布页/镜像站公布为准写入台账，不得凭空填写。

步骤 3 配置 repo 源。若 `/etc/yum.repos.d/local.repo` 已存在，先 cp -n 备份原件（保留首次备份，
不得直接覆盖不备份），再打开 `/etc/yum.repos.d/local.repo`，添加以下内容：

```bash
# local.repo 原本已存在时先 cp -n 备份（已有 local.repo.bak 时不覆盖，保留首次备份原件）；
# 原本不存在则跳过备份，直接新建
[ -f /etc/yum.repos.d/local.repo ] && cp -n /etc/yum.repos.d/local.repo /etc/yum.repos.d/local.repo.bak
```

```ini
[local]
name=system local
baseurl=file:///media/
enabled=1
gpgcheck=1
gpgkey=file:///media/RPM-GPG-KEY-openEuler
```

安全说明：禁用 gpgcheck 不得用于掩盖本地包签名校验，本地源同样保持 gpgcheck=1 并引用镜像内
GPG key。`gpgkey=file:///media/...` 的具体路径以镜像内 RPM-GPG-KEY 文件实际路径为准（挂载后先
执行 `find /media -name 'RPM-GPG-KEY*'` 现场核对，不得凭空填写路径），并执行
`gpg --show-keys <keyfile>` 将密钥指纹与 openEuler 官方发布页公布值对照一致后方可使用；
若 ISO 未提供 RPM-GPG-KEY 文件，改用 §1.2 的系统预装密钥核对流程后引用系统密钥路径。

### 1.2 基于在线源安装

步骤 1 备份原有 openEuler.repo 文件（保留首次备份，重复运行不覆盖）。

```bash
# cp -n：已有 openEuler.repo.bak 时不覆盖，保留首次备份原件
cp -n /etc/yum.repos.d/openEuler.repo /etc/yum.repos.d/openEuler.repo.bak
# 已有备份时先 diff 核实当前配置与首次原件的差异，再决定是否继续（差异写入台账）
diff -u /etc/yum.repos.d/openEuler.repo.bak /etc/yum.repos.d/openEuler.repo || true
RUN_ID=$(date +%Y%m%d%H%M%S)
mv /etc/yum.repos.d/openEuler.repo /etc/yum.repos.d/openEuler.repo.bak.$RUN_ID
```

安全说明：同 §1.1 步骤 1——回退时执行 `cp -p /etc/yum.repos.d/openEuler.repo.bak /etc/yum.repos.d/openEuler.repo` 恢复首次原件，首次备份不得被二次运行覆盖。

步骤 2 核对 RPM 签名公钥（优先系统预装，不走明文 HTTP 导入）。

```bash
# 优先使用系统预装密钥：列出已导入的 GPG 公钥，确认 openEuler 官方密钥已在其中
rpm -q gpg-pubkey --qf '%{NAME}-%{VERSION}-%{RELEASE} %{SUMMARY}\n'
```

若系统已预装 openEuler 官方公钥，直接进入步骤 3。若必须导入：

```bash
# 从 HTTPS 官方渠道下载 key（不得使用明文 http:// 地址导入）
curl -fsSL https://repo.openeuler.org/openEuler-22.03-LTS-SP4/OS/aarch64/RPM-GPG-KEY-openEuler -o /root/RPM-GPG-KEY-openEuler
# 核对指纹：将显示的指纹与 openEuler 官方发布页公布值逐字符对照
gpg --show-keys /root/RPM-GPG-KEY-openEuler
# 指纹一致后方可导入；不一致或无法核对时不得导入
rpm --import /root/RPM-GPG-KEY-openEuler
```

安全说明：密钥指纹值以 openEuler 官方发布页公布为准，本指南不代填指纹值；导入前必须完成
`gpg --show-keys` 指纹核对并写入台账，禁止跳过核对直接导入。

步骤 3 打开 `/etc/yum.repos.d/openEuler.repo`，添加以下完整内容后保存退出：

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

安全说明（TLS 校验保持开启）：保持 TLS 证书校验开启（禁止为联网安装全局关闭 yum 的 sslverify
证书校验，禁止把 git 的 sslVerify 全局置 false，禁止给 wget 追加跳过证书校验的参数）；各段
gpgkey 一律使用 HTTPS 官方渠道地址；若使用内网自签名镜像源，把内部 CA 装入系统信任库：

```bash
cp <internal-ca>.crt /etc/pki/ca-trust/source/anchors/
update-ca-trust extract
```

## 2. Ceph 的安装（§2.4）

步骤 1 刷新缓存，生成 Yum 本地缓存。

```bash
yum clean all && yum makecache
```

步骤 2 安装前快照（记录已装 Ceph 相关包与当前 yum 事务 id，写入台账）。

```bash
# 记录安装前已存在的 ceph 相关包清单（作为回退基线快照）
rpm -qa | grep -E '^ceph' | tee /root/ceph-rpm-snapshot-before.txt
# 记录安装前最后一个 yum 事务 id：本次 yum install 产生的事务 id 在此基础上递增，安装后核对并写入台账
yum history | head -n 5
```

步骤 3 安装 Ceph，并记录本次安装事务 id。

```bash
yum install ceph -y
# 本次 yum install ceph 生成的事务 id：yum history 第一行所示，写入台账并核对事务内容确为本次安装
yum history | head -n 5
yum history info <id>
```

验证：执行 `ceph -v`，能正确显示版本号则说明安装成功。

```bash
ceph -v
```

回退（只撤销本次安装事务，不得无条件卸载全部 Ceph 软件包）：

```bash
# 首选：撤销本次安装事务，<id> 为步骤 3 记录的本次 yum install ceph 事务 id
yum history undo <id>
```

若当前 yum 版本不支持 history undo，按步骤 2 快照恢复：对快照中已存在的被升级包，装回快照记录
的原版本（`yum install <包名>-<原版本完整NEVRA>`）；对仅本次新装的包，按确切包名逐一卸载
（`yum remove <仅本次新装的确切包名>`）。快照中不存在的包、非本次事务引入的包不得卸载；
禁止不带快照依据整批通配卸载 Ceph 软件包。

本地源路径（§1.1）还需清理本次新增的 repo 环境变更：本次新建的 local.repo 直接删除；
local.repo 原本已存在时恢复 cp -n 备份的原件（不得误删或改写他人原有配置）；随后卸载本地镜像，
挂载点目录为本次新建时再 `rmdir <挂载点>` 清理（必要时），系统原有目录不得删除：

```bash
if [ -f /etc/yum.repos.d/local.repo.bak ]; then
    # local.repo 原本已存在：恢复 cp -n 备份的原件
    cp -p /etc/yum.repos.d/local.repo.bak /etc/yum.repos.d/local.repo
else
    # local.repo 为本次新建：直接删除
    rm -f /etc/yum.repos.d/local.repo
fi
# 卸载本地镜像
umount /media
```

回退末尾刷新缓存并核对启用源清单与变更前一致（与安装前只读探测留档对照，差异写入台账）：

```bash
yum clean all && yum makecache && yum repolist
```

## 3. 后续

安装完成后，MON/MGR/OSD 部署与块存储使能参见姊妹 skill `ceph-block-storage-deployment`
（已合入，PR #446）；集群扩容按存储类型路由到对应扩容姊妹 skill：块存储扩容参见
`ceph-block-expansion`（已合入，PR #452）、文件存储扩容参见 `ceph-file-expansion`（已合入，PR #453）、
对象存储扩容参见 `ceph-object-expansion`（已合入，PR #454）；不确定存储类型时列出上述三个扩容
skill，按集群实际使用的存储接口（块/文件/对象）选择对应 skill。
