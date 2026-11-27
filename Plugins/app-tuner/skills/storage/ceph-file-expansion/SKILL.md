---
name: ceph-file-expansion
description: >
  鲲鹏平台 Ceph 文件存储扩容 SOP：鲲鹏节点加入既有 x86 集群（扩容基线 Ceph 12.2.11 +
  ceph-deploy 2.0.1，CentOS 7.6/RHEL 7.5）。扩容机器环境配置与同版本 Ceph 安装（排除
  debuginfo 包）后执行文件存储扩容路径：修改 ceph.conf 的 mon_initial_members/mon_host 并
   推送、创建 Monitor 并以 ceph mon stat 验证、CephFS 客户端以受限身份 secretfile 重新
   挂载、MGR/MDS/OSD 部署（建 OSD 前逐机核对设备标识）、存储池 pgnum/pgpnum 按官方口径
   公式调整；以 ceph -s 恢复健康、ceph osd df 容量增加与 ceph fs status 收尾。当需要给
   现有 Ceph 集群扩容鲲鹏节点（文件存储路径）时使用；首次部署 → 姊妹 skill
   `ceph-file-storage-deployment`（同批 PR #446，尚未合入）；块存储扩容 →
   `ceph-block-expansion`（同批 PR #452，尚未合入）、对象存储扩容 →
   `ceph-object-expansion`（同批 PR #454，尚未合入）；运行期调优 → skill
   `ceph-parameter-tuning`（已合入）；编译 → skill `ceph-build`（已合入）。
license: MulanPSL-2.0
compatibility: 鲲鹏 920 5220（aarch64）扩容节点加入 x86 集群；OS 矩阵 CentOS 7.6.1810 / RHEL 7.5；Ceph 12.2.11（扩容基线，新节点须安装与原集群同版本）+ ceph-deploy 2.0.1；NTP/crontab；扩容含删除 Monitor 等破坏性操作
metadata:
  skill_type: tool_assisted
  category: expansion
  domain: distributed_storage
  version: "1.0.0"
---

# ceph-file-expansion — 鲲鹏节点加入 x86 集群文件存储扩容

## 简介

对既有 x86 Ceph 集群做鲲鹏节点文件存储扩容（示例：ceph1~ceph3 扩容加入 ceph4/ceph5）：先完成
扩容机器环境配置（防火墙收敛/主机名/hosts/NTP/免密/SELINUX/epel 源/Ceph 镜像源）并安装与原集群
**同版本**的 Ceph 软件，再执行文件存储扩容路径：添加 Monitor 并以 mon stat 验证 → CephFS
客户端重新挂载（以受限客户端身份与 secretfile 方式挂载至 /mnt/cephfs，不回显密钥）→ 部署
MGR/MDS/OSD（建 OSD 前逐机核对设备标识、写 by-id；MDS 扩 active 需调 max_mds）→ 按官方
口径 pg 公式调整存储池 pgnum/pgpnum（fs_metadata/fs_data）。以 `ceph -s` 数据迁移结束恢复
健康、`ceph osd df` 容量增加、`ceph fs status` MDS 扩容生效为扩容完成判据。内容提炼自
《鲲鹏 BoostKit 分布式存储使能套件 Ceph 编译&部署&调优&测试指南》（文档版本 07，
2025-06-30）第 12 章文件存储扩容路径，命令与配置片段以原文为基础，检视修复引入的安全增强
以标注说明。

## 边界表

| 场景 | 归属 |
|---|---|
| 文件存储扩容（鲲鹏节点加入 x86 集群：Monitor/MGR/MDS/OSD/CephFS 重新挂载/pgnum） | **本 skill** |
| 首次部署（文件存储 MDS/CephFS 集群） | 姊妹 skill `ceph-file-storage-deployment`（同批 PR #446，尚未合入；合入前该场景按通用流程处理并在产出中标注「未覆盖」） |
| 块存储扩容（RBD 路径） | 姊妹 skill `ceph-block-expansion`（同批 PR #452，尚未合入；合入前该场景按通用流程处理并在产出中标注「未覆盖」） |
| 对象存储扩容（RGW 路径） | 姊妹 skill `ceph-object-expansion`（同批 PR #454，尚未合入；合入前该场景按通用流程处理并在产出中标注「未覆盖」） |
| 集群运行期参数调优 | skill `ceph-parameter-tuning`（已合入） |
| Ceph 源码编译 | skill `ceph-build`（已合入） |

## 铁律

1. **只读探测先行**：扩容前记录现有集群版本（`ceph -v`）、拓扑（`ceph osd tree`）、容量
   （`ceph osd df`）与各 pool 的 pg_num/pgp_num 与 size 作为 before 证据；现状不明不开跑。
2. **版本与 OS 矩阵匹配**：扩容基线 = Ceph 12.2.11 + ceph-deploy 2.0.1 + CentOS 7.6/RHEL
   7.5；新节点必须安装与原集群同版本的 Ceph（排除 debuginfo 包），不得跨版本混部。
3. **先环境后扩容**：新节点防火墙/主机名/hosts/NTP/免密/SELINUX/epel 与 Ceph 源配置完成前，
   不得进入 Monitor/OSD 扩容步骤；停止 firewalld、SELinux permissive 属需用户授权的降防护
   动作，限受控内网与变更窗口，缺失授权时该步骤 blocked。
4. **mon 配置修改顺序**：先改 ceph.conf 的 mon_initial_members/mon_host 并推送至各节点，
   再 `ceph-deploy mon create` 并以 `ceph mon stat` 验证；删除 Monitor（mon destroy）属破坏性
   操作，执行前必须与用户二次确认目标节点。
5. **pg 公式调整（官方口径）**：扩容后按 Total PGs = (OSD 总数 × 100) / 最大副本数 计算，
   向上取整到最近的 2 的整数次幂，多池按各池数据量占比分摊，重算并调整存储池 pgnum/pgpnum，
   不调会导致数据分布不均；Ceph 12.2.11 不支持调低 pg_num，目标 PG 数须在变更前核准，
   原值采集仅作防呆对比、不构成回退承诺。
6. **扩容验证与台账**：`ceph -s` 确认数据迁移结束集群恢复健康、`ceph osd df` 确认容量增加、
   `ceph fs status`（或 `ceph mds stat`）确认 MDS 扩容生效；文件存储扩容后 CephFS 客户端必须
   重新挂载；扩容前后 IP 规划与容量对比入台账。
7. **建 OSD 前逐机核对设备标识**：`lsblk` + `/dev/disk/by-id` 逐机确认，`df -l /` 排根盘、
   `findmnt` 排挂载盘、现役 OSD（`ceph osd tree`）排占用；仅对确认的数据盘执行建盘、遇错
   即停，命令写 by-id 稳定路径，不得按盘符盲建。
8. **密钥最小化与不落明文**：CephFS 客户端挂载优先使用受限客户端身份（client.fs-expansion）
   与 secretfile 方式；keyring（权限 600）留在管理主机 ceph1 供 `ceph -s --name` 受限身份
   验证，挂载 secretfile 指向从 keyring 提取的 0600 纯 key 文件（12.2.11 mount.ceph 只取
   文件首行纯 base64 key，keyring 首行是段头，整文件作 secretfile 会报 secret is not valid
   base64）；禁止回显/打印任何 keyring 与 key 内容，密钥不得进入输出、台账与 argv，只记录
   已使用某身份与文件路径（600）；管理密钥（client.admin）只保留在管理主机，不随配置分发
   到各节点。免密登录私钥设口令并托管 agent，authorized_keys 加 from= 管理网段来源限定，
   主机指纹经 ssh-keyscan 预填后以 StrictHostKeyChecking=yes 连接，收尾从扩容节点
   authorized_keys 撤销本次下发条目。

## 关键动作表

| 序 | 动作 | 参考 |
|---|---|---|
| ① | 版本与适用矩阵、扩容前后 IP 规划（ceph4/ceph5 三网 IP） | `references/file-expansion.md` §0 |
| ② | 扩容机器配置与安装（防火墙/主机名/hosts/NTP/免密/SELINUX/epel 源/Ceph 源/同版本安装） | `references/file-expansion.md` §1 |
| ③ | 文件存储扩容（Monitor 添加与 mon stat 验证、CephFS 客户端重新挂载（受限身份 secretfile）、MGR/MDS/OSD（建盘前设备核对、max_mds）、存储池 pgnum/pgpnum（官方口径公式）、验证） | `references/file-expansion.md` §3 |

## 输出契约

- **扩容清单**：扩容前后节点与 IP 规划对比表（原 ceph1~ceph3 + 新增 ceph4/ceph5 三网 IP）、
  新节点软件版本（与原集群同版本回显）、所选扩容路径（文件存储）。
- **健康验证结果**：`ceph mon stat` 出现扩容机器、`ceph -s` 数据迁移结束恢复 HEALTH_OK、
  `ceph osd df` 容量增加对比、`ceph fs status`（或 `ceph mds stat`）确认 MDS 扩容生效；
  文件存储输出客户端重新挂载与 `stat -f` 验证。
- **回退步骤**：删除 Monitor 的清理路径（破坏性操作二次确认后执行）；环境类还原——
  `systemctl enable --now firewalld` 并恢复原规则、`setenforce enforcing` 且 /etc/selinux/config
  还原原值、/etc/hosts 与主机名还原、/etc/ntp.conf 用 .bak 还原、移除新增 ceph.repo/epel 源
  并按需卸载新增 Ceph 包；免密撤销——从 ceph4/ceph5 的 authorized_keys 移除本次下发条目
  （含 from= 来源限定条目）；pg_num/pgp_num 不承诺还原原值（Ceph 12.2.11 不支持调低 pg_num，
  目标 PG 数须变更前核准）；客户端挂载还原；全部动作绑定变更窗口记录。

## Dependencies

| 工具/资源 | 用途 | 缺失影响与降级 |
|---|---|---|
| ceph-deploy（2.0.1） | Monitor/MGR/MDS/OSD 扩容操作 | 缺失时扩容路径 blocked，先补装同版本工具链 |
| 原集群同版本 Ceph 包源（epel 源 + rpm-luminous ceph.repo） | 新节点安装同版本软件 | 源缺失时新节点安装 blocked，不得跨版本安装 |
| NTP/crontab | 新节点与集群时间同步 | 时间不同步会导致 Monitor 加入失败，先补齐同步 |
| firewalld/SELinux（停止/permissive） | 扩容窗口内网络与安全放行 | 需用户授权的降防护动作，缺失授权时该步骤 blocked，不得擅自关闭 |
| CephFS 客户端身份（client.fs-expansion）keyring（600，验证用）与纯 key 文件（600，secretfile 用）与内核 ceph 挂载 | CephFS 客户端重新挂载验证（secretfile 指向纯 key 文件） | 身份/密钥不可用时客户端验证降级为集群侧 ceph -s 检查并标注证据缺失 |
| `ceph osd df` / `ceph osd tree` | 扩容前后容量与拓扑对比 | 不可用时扩容验证降级为 ceph -s 健康检查并标注证据缺失 |
| `references/file-expansion.md` | 文件存储路径全量命令 SOP | 缺失时输出 blocked，不得凭记忆拼命令 |
