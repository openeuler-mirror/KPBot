# Ceph 自动部署指南（CentOS 7.6，ceph-ansible-autotool 套件）

> 来源：《鲲鹏 BoostKit 分布式存储使能套件 Ceph 编译&部署&调优&测试指南》（文档版本 07，
> 2025-06-30）第 7 章「Ceph 自动部署指南（CentOS 7.6）」全文蒸馏
> （§7.2 配置部署环境 / §7.3 安装 Ceph）。本文件按 SOP 步骤组织，原文命令逐字保留；
> 客户端凭据下发方式为本仓安全口径修正（见 §4.3「安全说明」）。

## 0. 版本与适用矩阵

| 项 | 版本 |
|---|---|
| Ceph | 建议使用版本 14.2.1（套件默认安装 14.2.1，改版本见 §3.0） |
| OS | CentOS 7.6 |
| 依赖 | python 2.7.9 及以上版本（套件依赖） |
| 工具 | 自动化工具 ceph-ansible-autotool.zip（含 ceph_input.xlsx / input_check.sh / install_all.sh / scripts/conf/manual.cfg） |
| 组网 | 3 台客户端节点 + 3 台鲲鹏服务器作为存储节点；网络含前端网络 Public Network 与后端网络 Cluster Network |

## 1. 配置物理环境（§7.2.1）

准备好服务端和客户端，根据组网规划完成物理服务器的物理组网（典型组网：Ceph 集群由 Ceph 客户端
和 Ceph 服务端组成，前端 Public Network 与后端 Cluster Network 分离）。

## 2. 配置软件（§7.2.2）

### 2.1 OS 配置

安装对应网卡的网卡驱动，在所有服务端机器上分别配置网卡 IP 用于部署管理
（本文档所有涉及网卡部分均以 1822 网卡为例）。

```bash
# 步骤1（可选）确认网卡驱动正常安装
hinicadm info
# 步骤2 确认管理网络 IP 已经配置
ip add show
```

### 2.2 Ceph 安装配置信息获取

```bash
# 步骤1 服务端输入 lsblk 获取数据盘符名称和磁盘大小信息
lsblk
```

- 从输出中获取数据盘的盘符名称（如 sda、sdb、sdc、sdd、sde、sdf、sdg、sdh、sdi、sdk、sdl）
  以及磁盘大小（如 3.7TB）——在 §3 填写安装参数时，ceph_input 文件中 3.7TB 需要转换成 3700GB；
- 获取需要当成 DB/WAL 的 SSD 盘符信息（如 nvme0n1 和 nvme1n1）以及磁盘大小（如 2.9TB）——
  填写安装参数时 2.9TB 需要转换成 2900GB。

说明：如果不需要单独配置 DB/WAL，请跳过对应步骤。

```bash
# 步骤2 获取节点需要配置业务网络的网卡名称
ip add show
```

## 3. 填写安装参数与校验（§7.2.3 / §7.2.4）

### 3.1 填写安装参数

下载自动化工具 ceph-ansible-autotool.zip，打开自动化工具 storage/input 目录下的
ceph_input.xlsx 配置文件，填写 Ceph Configuration、Server Configuration、Client Configuration
三个 sheet 里的必选配置。

说明：具体参数参考工具内各字段说明；如果不需要安装客户端，清空 Client Configuration 工作簿即可。

### 3.2 校验安装参数

```bash
# 步骤1 下载整套安装套件，上传到需要安装 Ceph 的一台服务器上
# 步骤2 将文件解压到任意目录下
# 步骤3 将 §3.1 填写的配置信息覆盖解压目录下 input 文件夹里的 Excel
# 步骤4 确认参数是否填写正确
sh input_check.sh
```

## 4. 安装 Ceph（§7.3）

### 4.1 部署 Ceph

说明（执行前须知）：

- Ceph 自动化安装套件默认安装 Ceph 14.2.1 版本；如需修改版本号，请修改
  scripts/conf/manual.cfg 的 yum_exclude 字段；
- 如需配置代理，请修改 scripts/conf/manual.cfg 的 config_proxy、config_no_proxy 字段；
- 本套件依赖 python 2.7.9 及以上版本。

```bash
# 步骤1 进入安装脚本路径进行安装
sh install_all.sh
# 步骤2 通过日志最下方 ceph install 日志确认是否安装成功，显示 OK 即安装成功
```

### 4.2 确认 Ceph 部署结果

登录一台服务端服务器执行，确认安装组件同配置文件是否一致：

```bash
ceph -s
```

### 4.3 安装客户端（按需限权身份，不下发 client.admin）

安全说明：不得将 client.admin 全权限身份复制到业务客户端（admin 拥有全集群管理权限，下发到
业务机会扩大凭据暴露面）。在服务端服务器上按业务实际用途创建受限身份，只分发该身份的 keyring
（凭据不入库、不进 git、不经 stdout 明文输出、不落日志，见 SKILL.md 铁律 6）：

```bash
# 步骤1 按实际用途创建受限业务身份（<pool> 替换为业务实际读写的池名，权限按需收敛）
ceph auth get-or-create client.<app> mon 'allow r' osd 'allow rw pool=<pool>'
# 步骤2 导出该受限身份的完整 INI keyring 文件并收紧权限（ceph auth get -o 直写文件，凭据不经
#       stdout 明文输出；不得用 auth print 类重定向写法产出裸 key，也不得以 0644 等宽松权限留置）
ceph auth get client.<app> -o /etc/ceph/ceph.client.<app>.keyring
chmod 600 /etc/ceph/ceph.client.<app>.keyring
# 步骤3 分发到业务客户端，并在客户端收紧权限、核验属主为 root（分发不得落 0644）
scp /etc/ceph/ceph.client.<app>.keyring root@客户端主机名:/etc/ceph/
ssh root@客户端主机名 "chmod 600 /etc/ceph/ceph.client.<app>.keyring && ls -l /etc/ceph/ceph.client.<app>.keyring"
# 步骤4 业务客户端以该身份访问集群回读验证
ceph -s --name client.<app>
```
