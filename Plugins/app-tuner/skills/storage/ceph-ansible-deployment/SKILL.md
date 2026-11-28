---
name: ceph-ansible-deployment
description: >
  鲲鹏平台 Ceph ceph-ansible 部署全链路（CentOS 7.6 / openEuler 20.03，Ansible 2.9.0 +
  ceph-ansible stable-4.0）：部署环境配置、安装 ceph-ansible、块/文件/对象三场景 group_vars
  （all.yml ceph_conf_overrides、osds.yml 两种 OSD 定义）、部署集群（site.yml）、MON/OSD/MDS
  扩容、purge 删除集群、repo 源压缩包制作；附 ceph-ansible-autotool 自动部署套件（CentOS 7.6，
  Ceph 14.2.1：ceph_input.xlsx 安装参数填写、input_check.sh 校验、install_all.sh 一键安装）。
  当需要 ansible 编排部署/扩容/删除 Ceph 集群或自动部署时使用；ceph-deploy 手工路径 → 姊妹
  skill `ceph-block-storage-deployment`；非 ansible 的鲲鹏扩容按集群实际使用的存储接口路由 →
  姊妹 skill `ceph-block-expansion`（块存储）/`ceph-file-expansion`（文件存储）/
  `ceph-object-expansion`（对象存储），类型不确定时在三者中按实际接口选择（均已合入，PR #452/#453/#454）。
license: MulanPSL-2.0
compatibility: 鲲鹏 920 5220（aarch64）；OS 矩阵 CentOS 7.6.1810 / openEuler 20.03 SP1（自动部署套件仅 CentOS 7.6）；Ansible 2.9.0 + ceph-ansible stable-4.0（Ceph 14.2.22 Nautilus）/ 自动部署套件 Ceph 14.2.1（依赖 python 2.7.9+）
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: distributed_storage
  version: "1.0.0"
---

# ceph-ansible-deployment — ceph-ansible 编排部署与自动部署

## 简介

用 ceph-ansible 编排部署 Ceph 集群：配置部署环境（SMMU/主机名/免密/防火墙/SELINUX/repo 源）
→ 安装 Ansible 2.9.0 与 ceph-ansible stable-4.0 → 按场景配置块/文件/对象三场景 group_vars →
`ansible-playbook -i hosts site.yml` 部署 → `ceph health` 显示 HEALTH_OK → 按需扩容
（add-mon.yml/add-osd.yml/MDS 群组）、删除集群（purge-cluster.yml）、制作 repo 源压缩包。
另覆盖 ceph-ansible-autotool 自动部署套件：填写 ceph_input.xlsx 安装参数、input_check.sh
校验、install_all.sh 一键安装。内容提炼自《鲲鹏 BoostKit 分布式存储使能套件 Ceph 编译&部署&调优&测试指南》
（文档版本 07，2025-06-30）第 3/7 章，原文命令与 yml 片段逐字保留；TLS 校验、凭据下发与
YAML 结构的安全/正确性加固为本仓口径补充（见铁律 7 与指南内「安全说明」「修正说明」）。

## 边界表

| 场景 | 归属 |
|---|---|
| ceph-ansible 编排部署/扩容/删除集群/repo 源压缩包、ceph-ansible-autotool 自动部署 | **本 skill** |
| ceph-deploy 手工部署（块存储） | 姊妹 skill `ceph-block-storage-deployment`（已合入，PR #446） |
| 鲲鹏节点加入 x86 集群扩容（非 ansible 路径，块存储扩容） | 姊妹 skill `ceph-block-expansion`（已合入，PR #452） |
| 鲲鹏节点加入 x86 集群扩容（非 ansible 路径，文件存储扩容） | 姊妹 skill `ceph-file-expansion`（已合入，PR #453） |
| 鲲鹏节点加入 x86 集群扩容（非 ansible 路径，对象存储扩容） | 姊妹 skill `ceph-object-expansion`（已合入，PR #454）；扩容存储类型不确定时按集群实际使用的存储接口在三者中选择 |
| 集群运行期参数调优 | skill `ceph-parameter-tuning` |

## 铁律

1. **只读探测先行**：部署前先只读采集现状（`lsblk`、`ip add show`、`yum repolist`、
   `cat /etc/hosts`）保留 before 证据；现状不明不开跑。
2. **部署前环境检查不放行**：SMMU（BIOS 关闭）、防火墙、主机名与 /etc/hosts、NTP 时间同步、
   免密登录（ssh-copy-id 分发公钥）、SELINUX permissive、repo 源等未逐节点确认完成前，不得
   进入 group_vars 配置与 site.yml 部署。
3. **版本与 OS 矩阵匹配**：ansible 路径 = Ansible 2.9.0 + ceph-ansible stable-4.0（Ceph
   14.2.22）+ CentOS 7.6/openEuler 20.03；自动部署套件 = CentOS 7.6 + Ceph 14.2.1 +
   python 2.7.9+；不得跨矩阵套用（openEuler 专属步骤：python-enum34 编译、关闭 chrony）。
4. **集群健康验证每阶段必过**：site.yml 执行结束所有节点 failed=0 后执行 `ceph health`
   确认 HEALTH_OK，并核对配置写入 ceph.conf；验证不过不进入扩容/交付，回显证据入台账。
5. **破坏性操作二次确认**：删除集群（`ansible-playbook -i hosts infrastructure-playbooks/
   purge-cluster.yml`，输入 yes）与删除 Monitor 属破坏性操作，执行前必须与用户二次确认目标
   集群并确认变更窗口。
6. **凭据与证据台账**：keyring/密钥文件不入库、不进 git、不落日志，免密统一 ssh-copy-id；
   业务客户端不得下发 client.admin 全权限身份，改用按实际用途限权的 client.<app> 身份并只分发
   该身份 keyring；每步记录节点、命令与关键回显（failed=0/HEALTH_OK/部署结果 OK），无回显证据
   不得标记完成。
7. **TLS 校验保持开启**：不得为联网安装全局关闭证书校验（不写 yum `sslverify=false`、不写 git
   全局 `http.sslVerify false`、不用 `wget --no-check-certificate`）；禁止明文 http:// 下载
   rpm/源码包（一律 https 下载，rpm 包下载后做签名校验再安装）；禁止 gpgcheck=0 的本地 repo
   直接安装（本地 repo 一律 gpgcheck=1/repo_gpgcheck=1 并导入对应 GPG key，无法签名时最低要求：
   记录 sha256 并与官方 repomd 校验和比对一致、结果记入台账后方可分发）；内网自签名镜像源把内部 CA
   装入系统信任库（update-ca-trust）；确需临时绕过时用命令级 `git -c http.sslVerify=false`
   （仅单条命令生效），并在台账记录；ceph-ansible 克隆后以 `git log -1 --oneline` 核对版本并
   将 commit/tag 写入台账。

## 关键动作表

| 序 | 动作 | 参考 |
|---|---|---|
| ① | 版本与适用矩阵、集群规划（三网 IP） | `references/ansible-deployment.md` §0 |
| ② | 配置部署环境（SMMU/主机名/免密/防火墙/SELINUX/repo 源/python-enum34/chrony） | `references/ansible-deployment.md` §1 |
| ③ | 安装 ceph-ansible 与依赖、hosts 角色与 group_vars（all.yml ceph_conf_overrides、osds.yml 两种 OSD 定义） | `references/ansible-deployment.md` §2 |
| ④ | 块/文件/对象三场景配置（group_vars/osds.yml、mdss.yml、rgws.yml） | `references/ansible-deployment.md` §3/§4/§5 |
| ⑤ | 部署集群（`ansible-playbook -i hosts site.yml`，ceph health 显示 HEALTH_OK） | `references/ansible-deployment.md` §6 |
| ⑥ | 集群扩容（add-mon.yml/add-osd.yml/MDS 群组） | `references/ansible-deployment.md` §7 |
| ⑦ | 删除集群（purge-cluster.yml，二次确认）与 repo 源压缩包制作（reposync + createrepo） | `references/ansible-deployment.md` §8/§9 |
| ⑧ | 自动部署（ceph-ansible-autotool：物理/软件环境、ceph_input.xlsx 安装参数与 input_check.sh 校验、install_all.sh 安装、客户端按需限权身份安装——不下发 client.admin） | `references/auto-deployment.md` §1–§4 |

## 输出契约

- **部署清单**：节点角色表（mons/osds/mdss/rgws/clients/grafana-server）、三网 IP 规划、
  group_vars 关键配置（ceph_conf_overrides、OSD 定义方式），与 hosts 实际配置一致。
- **健康验证结果**：site.yml 执行回显（所有节点 failed=0）、`ceph health` 显示 HEALTH_OK、
  配置写入 ceph.conf 核对；自动部署输出日志 ceph install 显示 OK 与部署结果确认回显。
- **回退步骤**：系统配置类给出还原动作（防火墙/SELINUX/repo 备份还原）；集群类给出删除路径
  （purge-cluster.yml，二次确认后执行）；扩容类给出 hosts 与 playbook 的对应回退说明。

## Dependencies

| 工具/资源 | 用途 | 缺失影响与降级 |
|---|---|---|
| ansible（2.9.0）+ ceph-ansible（stable-4.0） | 集群自动化部署、扩容、删除集群 | 缺失时改走 ceph-deploy 手工路径（姊妹 skill `ceph-block-storage-deployment`，已合入，PR #446） |
| python（自动部署套件需 2.7.9+） | ceph-ansible 依赖、自动部署套件运行 | 缺失时自动部署路径不可用，仅 ansible 手工编排可走 |
| ceph-ansible-autotool.zip 套件 | 自动部署（ceph_input.xlsx/input_check.sh/install_all.sh） | 缺失时降级为 ansible 手工编排路径 |
| 互联网接入（在线安装软件包与依赖） | Ansible/ceph-ansible 安装、reposync | 无外网时降级为离线 repo 源压缩包路径（§9 已给出制作方法） |
| `references/ansible-deployment.md`、`references/auto-deployment.md` | 全量命令与 yml 片段 SOP | 缺失时输出 blocked，不得凭记忆拼 playbook |
