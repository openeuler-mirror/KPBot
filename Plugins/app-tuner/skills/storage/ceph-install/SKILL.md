---
name: ceph-install
description: >
  鲲鹏平台 Ceph 16.2.7 repo 安装 SOP：openEuler 22.03 上基于系统镜像源（已集成 Ceph 16.2.7
  Pacific）的本地源/在线源两种安装路径——备份 openEuler.repo、挂载 openEuler-22.03-LTS-SP4
  iso 配 local.repo 或配置指向 repo.openeuler.org 的七段在线源、刷新 Yum 缓存、yum 安装并以
  ceph -v 验证。当需要在 openEuler 22.03 直接安装 Ceph 16.2.7 时使用；不适用 14.2.8 源码
  编译（→ skill `ceph-build`）与集群部署（→ 姊妹 skill `ceph-ansible-deployment` /
  `ceph-block-storage-deployment`，同批姊妹 PR，同批交付）。
license: MulanPSL-2.0
compatibility: 鲲鹏 920 5220（aarch64）；OS 矩阵 openEuler 22.03 LTS SP4（Ceph 16.2.7 已集成系统镜像源，仅此矩阵）；yum、NTP；安装过程需接入互联网（在线源）
metadata:
  skill_type: tool_assisted
  category: install
  domain: distributed_storage
  version: "1.0.0"
---

# ceph-install — Ceph 16.2.7 安装（openEuler 22.03）

## 简介

在鲲鹏平台 openEuler 22.03 上安装 Ceph 16.2.7：系统镜像源已集成 Ceph 16.2.7 Pacific，使用
系统镜像源自带包即可，无需源码编译。按环境约束选择本地源（挂载 openEuler-22.03-LTS-SP4 iso）
或在线源（指向 repo.openeuler.org）两种方式之一配置安装环境，刷新 Yum 缓存后安装并以
`ceph -v` 验证。内容提炼自《鲲鹏 BoostKit 分布式存储使能套件 Ceph 编译&部署&调优&测试指南》
（文档版本 07，2025-06-30）第 2 章，原文命令与 repo 配置逐字保留；TLS/GPG 校验与回退安全加固
为本仓安全口径补充（见铁律与指南内「安全说明」）。

```
确认版本矩阵（16.2.7 仅 openEuler 22.03）→ 本地源 / 在线源二选一 → 刷新 Yum 缓存
→ yum 安装 Ceph → ceph -v 验证 16.2.7 → 后续部署走部署族 skill
```

## 边界表

| 场景 | 归属 |
|---|---|
| Ceph 16.2.7 安装（openEuler 22.03 本地源/在线源） | **本 skill** |
| Ceph 14.2.8 源码编译 | skill `ceph-build` |
| 集群部署（ceph-ansible 编排 / ceph-deploy 手工块存储） | 姊妹 skill `ceph-ansible-deployment` 或 `ceph-block-storage-deployment`（同批姊妹 PR，同批交付） |

## 铁律

1. **只读探测先行**：安装前先只读确认 OS 版本（`cat /etc/os-release`，须为 openEuler 22.03
   LTS）、`yum repolist` 现状并保留 before 证据；现状不明不开跑。
2. **版本与 OS 矩阵匹配**：16.2.7 安装矩阵 = openEuler 22.03（系统镜像源已集成），不得在
   CentOS/Ubuntu 套用本 SOP，也不得把 14.2.8 源码编译路径混入安装路径（编译 → skill
   `ceph-build`）。
3. **repo 先备份后修改，保留首次备份**：配置 openEuler.repo/local.repo 前先备份原文件
   （`cp -n` 不覆盖已有 .bak，已有备份时先 diff 核实当前配置与首次原件差异再决定继续），
   回退一律恢复首次原件并写入台账；在线源七段配置
   （OS/everything/EPOL/debuginfo/source/update/update-source）逐字保留。
4. **先刷缓存再安装，以 ceph -v 为判据**：安装前执行 `yum clean all && yum makecache`；
   安装完成后必须以 `ceph -v` 显示 16.2.7 版本号为成功判据，无版本回显不得标记完成。
5. **证据与台账**：每步记录命令与关键回显（repo 配置内容/缓存刷新/安装包清单/`ceph -v`）；
   安装失败保留 yum 报错原文再排查。
6. **TLS 校验保持开启**：不得为联网安装全局关闭证书校验（不写 yum `sslverify=false`、不写 git
   全局 `http.sslVerify false`、不用 `wget --no-check-certificate`）；内网自签名镜像源把内部 CA
   装入系统信任库（update-ca-trust）；确需临时绕过时用命令级 `git -c http.sslVerify=false`
   （仅单条命令生效），并在台账记录；RPM 签名公钥优先使用系统预装密钥并核对官方发布页指纹，
   不得经明文 HTTP 渠道导入（gpgkey 一律 HTTPS 或 file://）。

## 关键动作表

| 序 | 动作 | 参考 |
|---|---|---|
| ① | 版本与适用矩阵确认（16.2.7 仅 openEuler 22.03，其他版本可参考） | `references/installation-guide.md` §0 |
| ② | 基于本地源安装（备份 openEuler.repo 保留首次备份、ISO 官方 sha256 摘要核对、挂载 SP4 iso 到 /media、配置 gpgcheck=1 的 local.repo 并核对 GPG key 指纹） | `references/installation-guide.md` §1.1 |
| ③ | 基于在线源安装（GPG 公钥优先系统预装并核对指纹、配置指向 repo.openeuler.org 的七段 openEuler.repo，gpgkey 一律 HTTPS） | `references/installation-guide.md` §1.2 |
| ④ | Ceph 的安装（安装前 ceph 包快照与 yum 事务 id 记录、刷新 Yum 缓存、yum 安装、`ceph -v` 版本验证、回退仅撤销本次事务 `yum history undo`） | `references/installation-guide.md` §2 |
| ⑤ | 后续（集群部署路由到部署族 skill） | `references/installation-guide.md` §3 |

## 输出契约

每轮安装产出三件套，缺一即未完成：

1. **repo 配置证据**：所选路径（本地源/在线源）的 repo 文件内容回显与原 repo 备份路径。
2. **安装证据**：Yum 缓存刷新与 yum 安装回显、Ceph 软件包清单。
3. **版本验证**：`ceph -v` 显示 ceph version 16.2.7 的回显证据；回退路径：恢复 openEuler.repo
   首次备份原件、`yum history undo <id>` 仅撤销本次安装事务（按安装前 ceph 包快照恢复被升级包
   原版本、卸载仅本次新装包，不得无条件卸载全部 Ceph 软件包）、本地源路径清理（本次新建的
   local.repo 直接删除，原本已存在时恢复 cp -n 备份原件；umount /media，必要时 rmdir 挂载点），
   回退末尾 `yum clean all && yum makecache && yum repolist` 核对启用源清单与变更前一致。

## Dependencies

| 工具/资源 | 用途 | 缺失影响与降级 |
|---|---|---|
| openEuler-22.03-LTS-SP4 系统镜像 iso | 本地源安装路径 | 缺失时改走在线源路径（需接入互联网） |
| 互联网接入 | 在线源安装与依赖包下载 | 无外网且无 iso 时安装路径 blocked |
| yum | 缓存刷新与软件包安装 | 缺失时安装路径 blocked，不得用其他包管理器替代本 SOP |
| `references/installation-guide.md` | 全量命令与 repo 配置 SOP | 缺失时输出 blocked，不得凭记忆拼 repo 配置 |
