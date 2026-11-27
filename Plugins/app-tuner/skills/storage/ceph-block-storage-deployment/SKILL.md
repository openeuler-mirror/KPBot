---
name: ceph-block-storage-deployment
description: >
  鲲鹏平台 Ceph 块存储手工部署 SOP（CentOS 7.6 / openEuler 20.03&22.03，ceph-deploy 2.0.1/2.1.0，
  Ceph 14.2.8/16.2.7）：镜像源配置与 ceph-deploy 适配（liboath、Ceph 镜像源、源码安装）、
  系统配置七项（SMMU/防火墙/主机名/NTP/免密/SELINUX/umask）、部署前环境分析与配置、
  MON/MGR/OSD 部署、存储池/块设备/映射使能（rbd）、常见问题五则、故障排除三则。
  当需要 ceph-deploy 手工部署块存储集群并使能 RBD 时使用；ansible 编排路径 → 姊妹
  skill `ceph-ansible-deployment`（同批 PR #445，尚未合入；合入前该场景按通用流程处理
  并在产出中标注「未覆盖」），对象存储 RGW → 姊妹 skill `ceph-object-storage-deployment`
  （同批 PR #447，尚未合入；合入前同上），文件存储 MDS → 姊妹 skill
  `ceph-file-storage-deployment`（同批 PR #448，尚未合入；合入前同上）。
license: MulanPSL-2.0
compatibility: 鲲鹏 920 5220（aarch64）；OS 矩阵 CentOS 7.6.1810 / openEuler 20.03 LTS SP1 / openEuler 22.03 LTS SP4；Ceph 14.2.8 Nautilus / 16.2.7 Pacific；ceph-deploy 2.0.1/2.1.0（CentOS yum 安装、openEuler git clone 源码安装）；BlueStore（DB/WAL 分区）
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: distributed_storage
  version: "1.0.0"
---

# ceph-block-storage-deployment — ceph-deploy 块存储手工部署

## 简介

用 ceph-deploy 手工部署块存储集群并使能 RBD：镜像源配置与 ceph-deploy 适配 → 系统配置七项
逐节点执行 → 部署前环境分析与配置（MON/MGR/OSD 规划与分区）→ 主节点 /root/ceph-mycluster/
下依次部署 MON/MGR/OSD → 创建存储池（pg 数按公式取 2 的整数次幂）、使能 rbd 池类型、
创建块设备并映射到客户端。内容提炼自《鲲鹏 BoostKit 分布式存储使能套件 Ceph 编译&部署&调优&
测试指南》（文档版本 07，2025-06-30）第 4 章，命令与配置片段逐字保留。

## 边界表

| 场景 | 归属 |
|---|---|
| ceph-deploy 块存储手工部署与 RBD 使能 | **本 skill** |
| ceph-ansible 编排部署/自动部署 | 姊妹 skill `ceph-ansible-deployment`（同批 PR #445，尚未合入；合入前该场景按通用流程处理并在产出中标注「未覆盖」） |
| 对象存储 RGW（ceph-deploy / cephadm 双路径） | 姊妹 skill `ceph-object-storage-deployment`（同批 PR #447，尚未合入；合入前该场景按通用流程处理并在产出中标注「未覆盖」） |
| 文件存储 MDS（CephFS） | 姊妹 skill `ceph-file-storage-deployment`（同批 PR #448，尚未合入；合入前该场景按通用流程处理并在产出中标注「未覆盖」） |

## 铁律

1. **只读探测先行**：部署前先只读采集现状（`ceph -s`、`lsblk`、`ip add show`、
   `yum repolist`、`cat /etc/hosts`）保留 before 证据；现状不明不开跑。
2. **系统配置不放行**：SMMU（可选）、防火墙、主机名与 /etc/hosts、NTP（服务端启动后需等
   5 分钟再在其他节点同步，否则报 no server suitable for synchronization found）、免密登录、
   SELINUX permissive、umask 0022（仅 openEuler）七项未逐节点确认完成前，不得进入
   MON/MGR/OSD 部署。
3. **SSH 信任域最小化**：免密登录仅限 ceph-deploy 管理链路最小范围（主节点 ceph1 对
   服务端 ceph1~ceph3，含自身），业务客户端（client1~client3）不对集群节点免密，仅以
   §4.2 步骤 4 签发的限权 keyring 访问 RBD；专用密钥/账号仅放行 ceph-deploy 所需操作，
   部署结束或节点退出时的撤销动作（清理 authorized_keys 对应公钥）记入台账。
4. **版本与 OS 矩阵匹配**：ceph-deploy 2.0.1/2.1.0（CentOS 用 yum 安装，openEuler 走
   git clone + setup.py install）；Ceph 14.2.8/16.2.7；不得跨矩阵套用命令。
5. **专用集群目录**：ceph-deploy 操作须在主节点的专用集群目录（`/root/ceph-mycluster/`）下
   执行，在 `/etc/ceph/` 下直接操作会产生冲突；验收口径分阶段执行——MON 部署后以
   `ceph mon stat`/`ceph quorum_status` 只确认 quorum，MGR 部署后以 `ceph mgr stat` 确认
   active，OSD 部署后以 `ceph osd stat` 确认 osd up/in，全部部署完成后再整体 `ceph -s`
   确认 HEALTH_OK；各阶段验证不过不进入下一步。
6. **磁盘操作二重核对**：parted/disk zap/wipefs 不可逆，执行前逐机核准稳定设备 ID 清单
   （`lsblk -o NAME,SIZE,TYPE,MOUNTPOINT` 与 `ls -l /dev/disk/by-id` 对照，写盘一律用
   by-id 路径），并排除根盘（`df -l /` 反查）、挂载盘（`findmnt`）与现役 OSD；禁止范围式
   （如 sda~sdl）直清，首次写盘前打印清单并与集群规划核对。数据盘/DB/WAL 规划（DB 分区
   ≥ 数据盘容量的 4%、WAL 分区 > 10GB）；曾属 Ceph 集群或装过系统的残盘先按残留分区清理
   流程处理，清理前与用户二次确认目标对象。OSD 布局二选一：普通创建与 DB/WAL 外置创建为
   互斥分支，不得先按普通分支建完 OSD 再对同一数据盘执行外置分支的 zap（会清掉刚建的 OSD）。
7. **凭据与证据台账**：keyring 文件（ceph.client.admin.keyring）不入库、不进 git、不落日志；
   业务客户端不得下发 client.admin 全权限身份，改用按实际用途限权的 client.<rbd-app> 身份
   并只分发该身份 keyring；每步记录节点、命令与关键回显（quorum/mgr active/osd up/in/
   HEALTH_OK/rbd0 出现），无回显证据不得标记完成。
8. **TLS 校验保持开启**：不得为联网安装全局关闭证书校验（不写 yum `sslverify=false`、不写 git
   全局 `http.sslVerify false`、不用 `wget --no-check-certificate`）；内网自签名镜像源把内部 CA
   装入系统信任库（update-ca-trust）；确需临时绕过时用命令级 `git -c http.sslVerify=false`
   （仅单条命令生效），并在台账记录。

## 关键动作表

| 序 | 动作 | 参考 |
|---|---|---|
| ① | 版本与适用矩阵、集群规划与硬盘划分（BlueStore DB/WAL） | `references/block-storage-deployment.md` §0 |
| ② | 镜像源配置与 ceph-deploy 适配（liboath、Ceph 镜像源、ceph-deploy 安装） | `references/block-storage-deployment.md` §1 |
| ③ | 系统配置七项（SMMU/防火墙/主机名/NTP/免密/SELINUX/umask，逐节点） | `references/block-storage-deployment.md` §2 |
| ④ | 部署前环境分析与配置（MON/MGR/OSD 配置与分区规划） | `references/block-storage-deployment.md` §3 |
| ⑤ | 安装 Ceph 与部署节点（mon create-initial/admin 仅服务端/mgr create/osd create 普通·DB/WAL 外置互斥分支） | `references/block-storage-deployment.md` §4 |
| ⑥ | 使能 Ceph 块存储（存储池创建、rbd 池类型、rbd create、rbd map 以限权身份映射，客户端出现 rbd0） | `references/block-storage-deployment.md` §5 |
| ⑦ | 常见问题五则（残留分区清理/快速划分 WAL·DB/快速部署 OSD/快速创建块设备/快速映射） | `references/block-storage-deployment.md` §6 |
| ⑧ | 故障排除三则（no module named xxx / auth: unable to find a keyring / conf_read_file） | `references/block-storage-deployment.md` §7 |

## 输出契约

按部署产出三件套，缺一即未完成：

- **部署清单**：节点角色表（MON/MGR/OSD/客户端）、三网 IP 规划、磁盘划分（数据盘/DB/WAL）、
  ceph.conf 关键项（mon_host、public_network/cluster_network）、SSH 免密发放范围收敛记录
  （仅 ceph1 → 服务端 ceph1~ceph3 及其撤销台账），与实际部署回显一致。
- **健康验证结果**：分阶段回显（MON 后 `ceph mon stat` quorum、MGR 后 `ceph mgr stat`
  active、OSD 后 `ceph osd stat` up/in、最后整体 `ceph -s` HEALTH_OK）；
  块存储使能输出 `rbd ls` 与 `rbd map` 证据（`ls /dev | grep rbd` 出现 rbd0）。
- **回退步骤**：系统配置类给出还原动作（防火墙恢复、SELINUX 原值、repo 与 ntp.conf 从
  `.bak.<run_id>` 首次备份原件还原）；集群类给出清理路径（disk zap/mon destroy 属破坏性
  操作，二次确认后执行）；免密收敛类给出 authorized_keys 对应公钥清理动作。

## Dependencies

| 工具/资源 | 用途 | 缺失影响与降级 |
|---|---|---|
| ceph-deploy（2.0.1 / 2.1.0） | MON/MGR/OSD 部署与块设备使能 | 缺失时 CentOS 用 yum 安装，openEuler 走 git clone + setup.py install |
| python3 / python2（按 OS） | ceph-deploy 源码安装与运行 | 缺失时 ceph-deploy 路径不可用，改走姊妹 skill `ceph-ansible-deployment`（同批 PR #445，尚未合入；合入前该场景按通用流程处理并在产出中标注「未覆盖」） |
| skill `ceph-build`（已合入） | Ceph 14.2.8 源码编译安装与 liboath 手动编译（本文件 §1.1、§4.1 引用） | 缺失时 14.2.8 编译路径 blocked，改走 Ceph 16.2.7 镜像源安装路径并在产出中标注「未覆盖」 |
| skill `ceph-install`（同批姊妹 PR，尚未合入） | Ceph 16.2.7 镜像源安装参考（本文件 §4.1 引用；其在线源七段 repo 配置已内联至本文件 §1.2） | 缺失时按本文件 §1.2 内联配置与 §1.1 本地源路径安装并在产出中标注「未覆盖」 |
| 磁盘拓扑（`lsblk -o NAME,SIZE,TYPE,MOUNTPOINT` 与 `/dev/disk/by-id` 对照，确认数据盘与 NVMe DB/WAL 盘） | OSD 划分与部署 | 缺失时不得执行 disk zap/parted/osd create，先补齐逐机核准清单 |
| 系统镜像源（everything iso/在线源） | liboath 与依赖安装 | 缺失时镜像源适配路径 blocked |
| `references/block-storage-deployment.md` | 全量命令 SOP | 缺失时输出 blocked，不得凭记忆拼命令 |
