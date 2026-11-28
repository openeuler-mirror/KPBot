---
name: ceph-file-storage-deployment
description: >
  鲲鹏平台 Ceph 文件存储手工部署 SOP（CentOS 7.6 / openEuler 20.03&22.03，ceph-deploy
  2.0.1/2.1.0，Ceph 14.2.8/16.2.7）：镜像源配置与 ceph-deploy 适配、系统配置七项、节点本地源
  同步与 MON/MGR/OSD/MDS 部署（ps -ef 过滤 ceph-mds 验证）、创建存储池和文件系统（ceph fs new，
  元数据池在前数据池在后，fs_data 分配更多 pg）、客户机挂载（mount -t ceph，MON 默认端口 6789，
  stat -f 验证 type=ceph）、常见问题与故障排除三则。当需要 ceph-deploy 手工部署文件存储集群
  （CephFS）时使用；对象存储 RGW → 姊妹 skill `ceph-object-storage-deployment`，块存储 →
  姊妹 skill `ceph-block-storage-deployment`（已合入，PR #446）。
license: MulanPSL-2.0
compatibility: 鲲鹏 920 5220（aarch64）；OS 矩阵 CentOS 7.6.1810 / openEuler 20.03 LTS SP1 / openEuler 22.03 LTS SP4；Ceph 14.2.8 Nautilus / 16.2.7 Pacific；ceph-deploy 2.0.1/2.1.0（CentOS yum 安装、openEuler 固定 v2.1.0 tag 源码克隆 + 隔离 venv 安装）；客户端 mount -t ceph（内核 ceph 客户端）
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: distributed_storage
  version: "1.0.0"
---

# ceph-file-storage-deployment — ceph-deploy 文件存储（MDS）部署

## 简介

用 ceph-deploy 手工部署文件存储集群（CephFS）：镜像源配置与 ceph-deploy 适配 → 系统配置
七项 → 节点本地源同步 → 主节点 /root/ceph-mycluster/ 下部署 MON/MGR/OSD/MDS → 创建
fs_data 与 fs_metadata 两个存储池（pg 数按官方单池基线公式 (OSD 数量×100)/副本数 取 2 的
整数次幂，fs_data 存数据量更大应分配更多 pg）、`ceph fs new` 创建文件系统（元数据池在前、
数据池在后）→ 客户机以限权身份 client.<cfs-app> 的 keyring 文件 `mount -t ceph` 挂载
（不在命令行出现密钥）并用 `stat -f` 验证文件系统类型为 ceph。内容提炼自《鲲鹏 BoostKit
分布式存储使能套件 Ceph 编译&部署&调优&测试指南》（文档版本 07，2025-06-30）第 6 章，
命令与配置片段逐字保留。

## 边界表

| 场景 | 归属 |
|---|---|
| 文件存储 MDS 部署（CephFS 存储池/文件系统/客户机挂载，ceph-deploy 路径） | **本 skill** |
| 对象存储 RGW（ceph-deploy / cephadm 双路径） | 姊妹 skill `ceph-object-storage-deployment`（已合入，PR #447） |
| 块存储 ceph-deploy 手工部署（RBD 使能） | 姊妹 skill `ceph-block-storage-deployment`（已合入，PR #446） |
| ceph-ansible 编排路径 | 姊妹 skill `ceph-ansible-deployment`（已合入，PR #445） |

## 铁律

1. **只读探测先行**：部署前先只读采集现状（`ceph -s`、`lsblk`、`ip add show`、
   `yum repolist`、`cat /etc/hosts`）保留 before 证据；现状不明不开跑。
2. **系统配置不放行**：SMMU（可选）、防火墙、主机名与 /etc/hosts、NTP（服务端启动后需等
   5 分钟再在其他节点同步）、免密登录、SELINUX permissive、umask 0022（仅 openEuler）七项
   未逐节点确认完成前，不得进入 MON/MGR/OSD/MDS 部署。
3. **版本与 OS 矩阵匹配**：ceph-deploy 2.0.1/2.1.0（CentOS 用 yum 安装，openEuler 走固定
   v2.1.0 tag 源码克隆 + 隔离 venv 安装）；Ceph 14.2.8/16.2.7；不得跨矩阵套用命令。
4. **专用集群目录**：ceph-deploy 操作须在主节点的专用集群目录（`/root/ceph-mycluster/`）下
   执行，在 `/etc/ceph/` 下直接操作会产生冲突；每阶段 `ceph -s` 确认 HEALTH_OK。
5. **ceph fs new 顺序与验证**：`ceph fs new cephfs fs_metadata fs_data` 元数据池在前、数据池
   在后，顺序不得颠倒；MDS 用 ceph-deploy mds create 创建，以 `ps -ef` 过滤 ceph-mds 进程
   验证启动成功；客户机挂载用 MON 节点 IP:6789 列表（MON 默认端口号 6789）并以 `stat -f`
   验证文件系统类型为 ceph。
6. **磁盘操作二重核对**：parted/disk zap/wipefs 不可逆，执行前逐机核准稳定设备 ID 清单
   （`lsblk -o NAME,SIZE,TYPE,MOUNTPOINT` 与 `ls -l /dev/disk/by-id` 对照，写盘一律用
   by-id 路径），并排除根盘（`df -l /` 反查）、挂载盘（`findmnt`）与现役 OSD；禁止范围式
   （如 sda~sdl）直清，首次写盘前打印清单并与集群规划核对。OSD 布局二选一：普通创建与
   DB/WAL 外置创建为互斥分支，不得先按普通分支建完 OSD 再对同一数据盘执行外置分支的 zap
   （会清掉刚建的 OSD）。
7. **凭据与证据台账**：keyring 密钥（ceph.client.admin.keyring）不入库、不进 git、不落日志、
   不在命令行打印；业务客户端不得下发 client.admin 全权限身份，改用按实际用途限权的
   client.<cfs-app> 身份并只分发该身份 keyring（keyring 文件落盘 600 权限，密钥串如确需使用
   从 keyring 文件取或用 read -s 读取，避免落入 shell history）；每步记录节点、命令与关键
   回显，无回显证据不得标记完成。
8. **TLS 校验保持开启与供应链加固**：不得为联网安装全局关闭证书校验（不写 yum
   `sslverify=false`、不写 git 全局 `http.sslVerify false`、不用 `wget --no-check-certificate`）；
   内网自签名镜像源把内部 CA 装入系统信任库（update-ca-trust）；确需临时绕过时用命令级
   `git -c http.sslVerify=false`（仅单条命令生效、仅限内网自签名地址），并在台账记录；
   第三方源码须固定版本并校验摘要（rev-parse HEAD 与台账比对，不一致中止）后方可安装，
   公开源不得关闭证书校验。
9. **危险开关用后即复位**：mon_allow_pool_delete 等危险开关不得在基础 ceph.conf 中预置常开，
   仅在对应破坏性操作（如 §6.3 删池）执行前 `ceph config set mon mon_allow_pool_delete true`
   临时开启，操作完成后立即复位为 false 并回显确认。

## 关键动作表

| 序 | 动作 | 参考 |
|---|---|---|
| ① | 版本与适用矩阵、集群规划与硬盘划分 | `references/file-storage-deployment.md` §0 |
| ② | 镜像源配置与 ceph-deploy 适配（liboath、Ceph 镜像源、ceph-deploy 安装） | `references/file-storage-deployment.md` §1 |
| ③ | 系统配置七项（SMMU/防火墙/主机名/NTP/免密/SELINUX/umask，逐节点） | `references/file-storage-deployment.md` §2 |
| ④ | 部署前环境分析与配置（节点本地源同步、MON/MGR/OSD 配置与分区规划） | `references/file-storage-deployment.md` §3 |
| ⑤ | 安装 Ceph 与部署节点（MON/MGR/OSD 普通·DB/WAL 外置互斥分支/MDS，ps -ef 过滤 ceph-mds 验证） | `references/file-storage-deployment.md` §4 |
| ⑥ | 创建存储池和文件系统（pg 按官方单池基线公式规划、ceph fs new 元数据池在前、可选 zlib 压缩）与客户机挂载（限权身份 keyring 文件、mount -t ceph、stat -f 验证） | `references/file-storage-deployment.md` §5 |
| ⑦ | 回退与清理（客户端 umount 与 /etc/fstab 清理、ceph fs rm、ceph osd pool delete、ceph-deploy mon destroy，破坏性操作二次确认） | `references/file-storage-deployment.md` §6 |
| ⑧ | 常见问题（快速划分 WAL·DB 分区/快速部署 OSD） | `references/file-storage-deployment.md` §7 |
| ⑨ | 故障排除三则（no module named xxx / auth: unable to find a keyring——一次性只读身份 client.ro-status + 排障后回收，不对 admin keyring 授持久 ACL / conf_read_file） | `references/file-storage-deployment.md` §8 |

## 输出契约

- **部署清单**：节点角色表（MON/MGR/OSD/MDS/客户端）、三网 IP 规划、存储池 pg 规划
  （fs_data/fs_metadata）、ceph.conf 关键项，与实际部署回显一致。
- **健康验证结果**：每阶段 `ceph -s` 回显（HEALTH_OK、osd up/in）；MDS 进程回显
  （ps -ef 过滤 ceph-mds）；`ceph fs ls` 显示 cephfs；客户机 `stat -f /mnt/cephfs` 显示
  文件系统类型为 ceph 的回显。
- **回退步骤**：与 `references/file-storage-deployment.md` §6「回退与清理」一一对应——挂载类
  给出客户端 umount 与 /etc/fstab 清理（§6.1）；集群类按逆序给出 CephFS 删除（§6.2：先 umount
  全部客户端并 `ceph mds fail`/`ceph fs fail` 处理 MDS 后 `ceph fs rm`）、存储池删除（§6.3：
  `ceph osd pool delete`，删池前临时开启 mon_allow_pool_delete、删除后立即复位 false，
  基础 ceph.conf 不预置）、MON 销毁（§6.4：`ceph-deploy mon destroy`）；破坏性操作二次确认
  后执行。

## Dependencies

| 工具/资源 | 用途 | 缺失影响与降级 |
|---|---|---|
| ceph-deploy（2.0.1 / 2.1.0） | MON/MGR/OSD/MDS 部署 | 缺失时 CentOS 用 yum 安装，openEuler 走固定 v2.1.0 tag 源码克隆 + 隔离 venv 安装 |
| python3（含 venv 模块） | ceph-deploy 隔离 venv 安装与运行 | 缺失时先补齐 python3 解释器（CentOS 7.6：yum install python36；openEuler 20.03/22.03：dnf install python3）并确认 `python3 -m venv` 可用，补齐后 ceph-deploy 隔离 venv 路径恢复；直接转交 skill `ceph-ansible-deployment`（已合入，PR #445）仅当 OS 为其矩阵内（CentOS 7.6 / openEuler 20.03）且用户明确接受路径变更，否则 ceph-deploy 路径标 blocked，不得以转交替代补齐依赖 |
| 内核 ceph 客户端（mount -t ceph） | 客户机挂载 CephFS | 客户机内核不支持时挂载路径 blocked，先确认客户端内核版本 |
| 系统镜像源（everything iso/在线源） | liboath 与依赖安装 | 缺失时镜像源适配路径 blocked |
| `references/file-storage-deployment.md` | 全量命令 SOP | 缺失时输出 blocked，不得凭记忆拼命令 |
