---
name: ceph-build
description: >
  鲲鹏平台 Ceph 14.2.8 源码编译 SOP：CentOS 7.6 / openEuler 20.03&22.03 三条编译路径的依赖
  环境、openEuler 22.03 路径的 isa-l 升级与 22.03_adaptation.patch 补丁、rpmdev-setuptree +
  rpmbuild -ba ceph.spec 编译、编译产物 local_ceph 本地源安装与 ceph -v 验证、故障排除三则。
  当需要从源码编译 Ceph 14.2.8 或为部署准备自编译 RPM 产物时使用；不适用 Ceph 16.2.7 repo
  安装与集群部署（当前仓库无对应 skill，规划中，按通用流程处理并标注未覆盖）
  与集群运行期调优（→ skill `ceph-parameter-tuning`）。
license: MulanPSL-2.0
compatibility: 鲲鹏 920 5220（aarch64）；OS 矩阵 CentOS 7.6.1810 / openEuler 20.03 LTS SP1 / openEuler 22.03 LTS SP2；Ceph 14.2.8 Nautilus；工具链 gcc-c++、cmake（CentOS 为 cmake3 + devtoolset-8-gcc-c++）、rpmbuild、rpmdevtools；安装依赖需接入互联网
metadata:
  skill_type: tool_assisted
  category: build
  domain: distributed_storage
  version: "1.0.0"
---

# ceph-build — Ceph 14.2.8 源码编译

## 简介

在鲲鹏平台从源码编译 Ceph 14.2.8：按起点 OS 选择 CentOS 7.6 / openEuler 20.03 / openEuler 22.03
三条编译路径之一，完成依赖环境配置、isa-l 升级与适配补丁、rpmbuild 打包编译，最后把编译产物
配置成本地源装回本机并用 `ceph -v` 验证。内容提炼自《鲲鹏 BoostKit 分布式存储使能套件 Ceph 编译&部署&调优&测试指南》（文档版本 07，2025-06-30）第 1 章，命令与配置片段逐字保留
（PDF 换行断词已复原为完整命令）。

```
确认版本矩阵 → 配置编译环境（按 OS 三选一）→ isa-l/补丁 → rpmbuild -ba ceph.spec
            → 编译产物 local_ceph 本地源安装 → ceph -v 验证 14.2.8 → 故障排除（如有）
```

## 边界表

| 场景 | 归属 |
|---|---|
| Ceph 14.2.8 源码编译（CentOS 7.6 / openEuler 20.03 / openEuler 22.03 三路径） | **本 skill** |
| Ceph repo 安装（16.2.7，openEuler 22.03 本地源/在线源） | 当前仓库无对应 skill（规划中）；按通用 yum 流程处理并在产出中标注「未覆盖」 |
| 集群部署（MON/MGR/OSD 初建，含 ceph-ansible 路径） | 当前仓库无对应 skill（规划中）；按通用部署流程处理并在产出中标注「未覆盖」 |
| 集群运行期参数调优（ceph.conf、PG、绑核、OS 层） | skill `ceph-parameter-tuning`（姊妹 PR #308，同批交付） |

## 铁律

1. **只读探测先行**：编译前先只读确认 OS 版本（`cat /etc/os-release`）、内存与磁盘资源、
   `yum repolist` 现状并保留 before 证据；现状不明不开跑，编译与部署分属两个 skill 不得混跑。
2. **版本与 OS 矩阵匹配**：14.2.8 编译矩阵 = CentOS 7.6.1810 / openEuler 20.03 LTS SP1 /
   openEuler 22.03 LTS SP2；不得跨矩阵套用命令（如把 CentOS 的 cmake3/devtoolset-8 步骤套到
   openEuler，或把 openEuler 的镜像源/依赖清单套到 CentOS）；isa-l 升级为三条路径共同步骤
   （铁律 3），不属跨矩阵禁例。
3. **isa-l 升级三路径必做 + 22.03 补丁**：三条编译路径均须升级 isa-l（mv isa-l isa-l.bak 后
   clone intel/isa-l，升级后 erasure_code/ 下有 aarch64 目录，并记录 isa-l commit 号）；
   openEuler 22.03 路径还必须下载并应用 22.03_adaptation.patch、重新打包 tar.bz2 后再 rpmbuild，
   否则编译失败；补丁只做平台适配，不得改动算法语义。
4. **编译产物先装后验**：编译产物通过 local_ceph 本地源 yum 安装后，成功判据 = `ceph -v` 显示
   14.2.8 版本号 + `rpm -q ceph` 包 NEVRA 与本机 rpmbuild 产物一致且来自 local_ceph 源；无版本
   回显与包一致性证据的步骤不得标记完成。
5. **TLS 校验保持开启**：不得为联网安装全局关闭证书校验（不写 yum `sslverify=false`、不写 git
   全局 `http.sslVerify false`）；内网自签名镜像源把内部 CA 装入系统信任库（update-ca-trust）；
   确需临时绕过时用命令级 `git -c http.sslVerify=false`（仅单条命令生效），并在台账记录。
6. **证据与台账**：每步记录 OS、命令与关键回显（依赖版本/isa-l 版本/spec 编译结果/`ceph -v`）；
   编译失败时保留 rpmbuild 报错原文再排查，不得凭经验跳步重试。

## 关键动作表

| 序 | 动作 | 参考 |
|---|---|---|
| ① | 版本与适用矩阵、硬件与硬盘划分确认（BlueStore DB/WAL：DB 分区 ≥ 数据盘 4%、WAL > 10GB） | `references/build-guide.md` §0 |
| ② | 配置编译环境（CentOS 7.6 / openEuler 20.03 / openEuler 22.03 三路径依赖：gcc-c++、cmake 或 cmake3 + devtoolset-8、rpmdevtools 等） | `references/build-guide.md` §1 |
| ③ | 编译 Ceph（isa-l 升级、22.03_adaptation.patch 补丁、重新打包 tar.bz2、rpmdev-setuptree、`rpmbuild -ba ceph.spec`） | `references/build-guide.md` §2 |
| ④ | 编译产物本地源安装（RPMS 产物、local_ceph 源、yum 安装、`ceph -v` 验证） | `references/build-guide.md` §3 |
| ⑤ | 故障排除三则（no module named xxx / auth: unable to find a keyring / conf_read_file） | `references/build-guide.md` §4 |

## 输出契约

每轮编译产出三件套，缺一即未完成：

1. **编译环境清单**：OS 版本回显（`cat /etc/os-release`）、依赖包与版本（gcc/cmake/rpmbuild/
   isa-l），与实际执行路径（三选一）一致。
2. **编译产物证据**：rpmbuild 产物 RPM 清单（RPMS/aarch64 目录回显）、local_ceph 本地源配置
   内容、yum 安装回显。
3. **安装验证**：`ceph -v` 显示 ceph version 14.2.8 且 `rpm -q ceph` 包 NEVRA 与本机 rpmbuild
   产物一致的回显证据；回退路径：卸载编译产物（yum remove ceph-14.2.8）、删除本次新增的
   local_ceph.repo、还原 repo 写入前备份（<文件名>.bak.<run_id>，原件不存在的新建文件直接删除）、
   `yum clean all && yum makecache` 后 `yum repolist` 核对启用源清单与变更前一致。

## Dependencies

| 工具/资源 | 用途 | 缺失影响与降级 |
|---|---|---|
| gcc-c++ / cmake（CentOS 为 cmake3 + devtoolset-8-gcc-c++） | Ceph 14.2.8 源码编译 | 缺失时编译路径 blocked，降级为 Ceph 16.2.7 系统镜像源直接安装（当前仓库无对应安装 skill，规划中；标注未覆盖） |
| rpmbuild / rpmdevtools | spec 打包编译 | 缺失时编译路径 blocked，先补装工具链 |
| OS 系统镜像 iso（本地源）/ 互联网接入 | 依赖安装、下载 isa-l/补丁/源码包 | 无外网时用离线压缩包与预置补丁，缺补丁不得跳过直接编译 |
| `references/build-guide.md` | 全量命令 SOP（三路径 + 故障排除） | 缺失时输出 blocked，不得凭记忆拼命令 |
