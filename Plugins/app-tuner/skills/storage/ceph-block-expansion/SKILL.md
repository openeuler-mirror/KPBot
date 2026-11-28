---
name: ceph-block-expansion
description: >
  鲲鹏平台 Ceph 块存储扩容 SOP：鲲鹏节点加入既有 x86 集群（扩容基线 Ceph 12.2.11 +
  ceph-deploy 2.0.1，CentOS 7.6/RHEL 7.5）。扩容机器环境配置与同版本 Ceph 安装（排除
  debuginfo 包）后执行块存储扩容路径：修改 ceph.conf 的 mon_initial_members/mon_host 并
  推送、创建 Monitor 并以 ceph mon stat 验证、MGR/OSD 部署、存储池 pgnum/pgpnum 公式调整、
  添加块设备；以 ceph -s 恢复健康与 ceph osd df 容量增加收尾。当需要给现有 Ceph 集群扩容
  鲲鹏节点（块存储路径）时使用；首次部署 → 姊妹 skill `ceph-block-storage-deployment`
  （同批 PR #446，已合入）；文件存储扩容 → `ceph-file-expansion`（同批 PR #453，
  已合入）、对象存储扩容 → `ceph-object-expansion`（同批 PR #454，已合入）；运行期调优 →
  skill `ceph-parameter-tuning`（已合入）；编译 → skill `ceph-build`（已合入）。
license: MulanPSL-2.0
compatibility: 鲲鹏 920 5220（aarch64）扩容节点加入 x86 集群；OS 矩阵 CentOS 7.6.1810 / RHEL 7.5；Ceph 12.2.11（扩容基线，新节点须安装与原集群同版本）+ ceph-deploy 2.0.1；NTP/crontab；扩容含删除 Monitor 等破坏性操作
metadata:
  skill_type: tool_assisted
  category: expansion
  domain: distributed_storage
  version: "1.0.0"
---

# ceph-block-expansion — 鲲鹏节点加入 x86 集群块存储扩容

## 简介

对既有 x86 Ceph 集群做鲲鹏节点块存储扩容（示例：ceph1~ceph3 扩容加入 ceph4/ceph5）：先完成
扩容机器环境配置（防火墙/主机名/hosts/NTP/免密/SELINUX/epel 源/Ceph 镜像源）并安装与原集群
**同版本**的 Ceph 软件，再执行块存储扩容路径：修改 ceph.conf 的 mon_initial_members/mon_host
并推送 → ceph-deploy mon create → ceph mon stat 验证 → MGR/OSD → 按 pg 公式调整存储池
pgnum/pgpnum → 创建块设备。以 `ceph -s` 数据迁移结束恢复健康、`ceph osd df` 容量增加为扩容
完成判据。内容提炼自《鲲鹏 BoostKit 分布式存储使能套件 Ceph 编译&部署&调优&测试指南》（文档版本 07，
2025-06-30）第 12 章块存储扩容路径，命令与配置片段保留自原文，检视修正项以文中标注为准。

## 边界表

| 场景 | 归属 |
|---|---|
| 块存储扩容（鲲鹏节点加入 x86 集群：Monitor/MGR/OSD/pgnum/块设备） | **本 skill** |
| 首次部署（ceph-deploy 手工部署块存储集群） | 姊妹 skill `ceph-block-storage-deployment`（同批 PR #446，已合入） |
| 文件存储扩容（MDS/CephFS 路径） | 姊妹 skill `ceph-file-expansion`（同批 PR #453，已合入） |
| 对象存储扩容（RGW 路径） | 姊妹 skill `ceph-object-expansion`（同批 PR #454，已合入） |
| 集群运行期参数调优 | skill `ceph-parameter-tuning`（已合入） |
| Ceph 源码编译 | skill `ceph-build`（已合入） |

## 铁律

1. **只读探测先行**：扩容前记录现有集群版本（`ceph -v`）、拓扑（`ceph osd tree`）与容量
   （`ceph osd df`）作为 before 证据；现状不明不开跑。
2. **版本与 OS 矩阵匹配**：扩容基线 = Ceph 12.2.11 + ceph-deploy 2.0.1 + CentOS 7.6/RHEL
   7.5；新节点必须安装与原集群同版本的 Ceph（排除 debuginfo 包），不得跨版本混部。
3. **先环境后扩容**：新节点防火墙/主机名/hosts/NTP/免密/SELINUX/epel 与 Ceph 源配置完成前，
   不得进入 Monitor/OSD 扩容步骤。
4. **mon 配置修改顺序**：先改 ceph.conf 的 mon_initial_members/mon_host 并推送至各节点，
   再 `ceph-deploy mon create` 并以 `ceph mon stat` 验证；删除 Monitor（mon destroy）属破坏性
   操作，执行前必须与用户二次确认目标节点。
5. **pg 公式调整（官方口径）**：扩容后按 Total PGs = (OSD 总数 × 100) / 最大副本数（EC 为
   K+M），向上取整到最近的 2 的整数次幂，多池按各池数据占比分摊，重算并调整存储池
   pgnum/pgpnum（源指南原文在公式中额外按 pool 数均分作除法，与官方口径不一致，以官方口径
   为准）；Ceph 12.2.11 拒绝调低 pg_num，调整前核准目标 PG 数并入变更窗口台账，原值仅记录
   防呆、不承诺还原；不调会导致数据分布不均。
6. **扩容验证与台账**：`ceph -s` 确认数据迁移结束集群恢复健康、`ceph osd df` 确认容量增加；
   扩容前后 IP 规划与容量对比入台账。
7. **OSD 按核对后的数据盘创建**：禁止按盘符循环盲建 OSD；逐机用 lsblk 与 /dev/disk/by-id
   对照（df -l / 排除根盘、findmnt 排除已挂载盘），仅对确认的数据盘执行，写盘用 by-id 路径；
   单盘失败即停止并报告，不得带错继续。
8. **环境与安全变更最小化、授权可恢复**：防火墙调整收敛到扩容所需（新增节点 ceph4/ceph5
   按需停用或放行 6789/3300/6800-7568，现有节点 ceph1~ceph3 与 client1 只加规则不停用防火墙）；
   SELinux permissive 仅限新增节点；均须执行前获用户授权确认并记录变更窗口；免密私钥设
   口令并托管 agent，authorized_keys 加 from= 管理网段来源限定；收尾按回退说明恢复。

## 关键动作表

| 序 | 动作 | 参考 |
|---|---|---|
| ① | 版本与适用矩阵、扩容前后 IP 规划（ceph4/ceph5 三网 IP） | `references/block-expansion.md` §0 |
| ② | 扩容机器配置与安装（防火墙/主机名/hosts/NTP/免密/SELINUX/epel 源/Ceph 源/同版本安装） | `references/block-expansion.md` §1 |
| ③ | 块存储扩容（Monitor 添加与 mon stat 验证、MGR/OSD、存储池 pgnum/pgpnum、添加块设备、验证） | `references/block-expansion.md` §2 |

## 输出契约

- **扩容清单**：扩容前后节点与 IP 规划对比表（原 ceph1~ceph3 + 新增 ceph4/ceph5 三网 IP）、
  新节点软件版本（与原集群同版本回显）、所选扩容路径（块存储）。
- **健康验证结果**：`ceph mon stat` 出现扩容机器、`ceph -s` 数据迁移结束恢复 HEALTH_OK、
  `ceph osd df` 容量增加对比；块存储输出 rbd 块设备创建与 `rbd ls` 列举回显。
- **回退说明**：本 skill 只覆盖扩容加入路径，OSD/RGW 等拆除、下线动作不在本 skill 范围；如需
  收缩集群参见官方 shrink/下线文档与姊妹 deployment skill `ceph-block-storage-deployment`
  （同批 PR #446，已合入）的回退小节。参考内可选的删除 Monitor 步骤属破坏性操作（二次
  确认后执行，见铁律 4）。pgnum/pgpnum 调整前原值入变更窗口台账并核准目标 PG 数：Ceph
  12.2.11 拒绝调低 pg_num，原值仅作防呆记录，不承诺用同款命令还原（见铁律 5）。环境收尾
  恢复项：SELinux 恢复 `setenforce 1` 并将 /etc/selinux/config 恢复 SELINUX=enforcing（变更
  前 cp -n 备份原值）；曾停用的 firewalld 执行 `systemctl enable --now firewalld` 并放行 Ceph
  端口 6789/3300/6800-7568；免密撤销：从 ceph4/ceph5 的 authorized_keys 移除本次下发条目
  （含 from= 来源限定条目）。

## Dependencies

| 工具/资源 | 用途 | 缺失影响与降级 |
|---|---|---|
| ceph-deploy（2.0.1） | Monitor/MGR/OSD 扩容操作 | 缺失时扩容路径 blocked，先补装同版本工具链 |
| 原集群同版本 Ceph 包源（epel 源 + rpm-luminous ceph.repo） | 新节点安装同版本软件 | 源缺失时新节点安装 blocked，不得跨版本安装 |
| NTP/crontab | 新节点与集群时间同步 | 时间不同步会导致 Monitor 加入失败，先补齐同步 |
| `ceph osd df` / `ceph osd tree` | 扩容前后容量与拓扑对比 | 不可用时扩容验证降级为 ceph -s 健康检查并标注证据缺失 |
| `references/block-expansion.md` | 块存储路径全量命令 SOP | 缺失时输出 blocked，不得凭记忆拼命令 |
