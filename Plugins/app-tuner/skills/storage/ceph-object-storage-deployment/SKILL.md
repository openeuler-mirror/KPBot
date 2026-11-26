---
name: ceph-object-storage-deployment
description: >
  鲲鹏平台 Ceph 对象存储 RGW 部署双路径：路径一 ceph-deploy（CentOS 7.6 / openEuler
  20.03&22.03，Ceph 14.2.8/16.2.7，civetweb 多实例：存储池 crush rule/EC、RGW 实例、
  radosgw-admin 账户、压缩放置策略、常见问题与故障排除）；路径二 cephadm 容器化（Ubuntu
  22.04 用 Docker≥24 / openEuler 22.03 用 Podman≥2.0，Ceph 17.2.8 beast：容器环境安装、
  cephadm 安装、自制容器镜像与本地仓库、bootstrap 部署 MON/MGR/OSD/RGW/客户端、卸载 Ceph）。
  当需要部署对象存储 RGW 时使用；块存储 → 姊妹 skill
  `ceph-block-storage-deployment`，文件存储 MDS → 姊妹 skill
  `ceph-file-storage-deployment`（同批姊妹 PR，同批交付）。
license: MulanPSL-2.0
compatibility: 鲲鹏 920 5220（aarch64）；路径一 OS 矩阵 CentOS 7.6.1810 / openEuler 20.03 LTS SP1 / openEuler 22.03 LTS SP4 + Ceph 14.2.8/16.2.7 + ceph-deploy 2.0.1/2.1.0；路径二 OS 矩阵 Ubuntu 22.04（Docker≥24）/ openEuler 22.03 LTS SP4（Podman≥2.0，以 3.4.4 为例）+ Ceph 17.2.8 Quincy + cephadm（Ubuntu apt 安装 / openEuler quincy 脚本）
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: distributed_storage
  version: "1.0.0"
---

# ceph-object-storage-deployment — 对象存储 RGW 双路径部署

## 简介

对象存储 RGW 部署按起点选择两条路径之一：路径一 ceph-deploy（镜像源与 ceph-deploy 适配、
系统配置 → MON/MGR/OSD/RGW 部署（ceph.conf 配置 RGW 实例端口）→ 存储池（crush rule/EC）
与 RGW 账户 → 可选使能 RGW 压缩（compress-placement 放置策略 + zlib/snappy/LZ4））；路径二
cephadm 容器化（Ubuntu 用 Docker≥24 / openEuler 用 Podman≥2.0 就绪容器环境 → 安装 cephadm →
openEuler 推荐自制 Ceph 17.2.8 容器镜像并推本地 registry:2 仓库 → `cephadm bootstrap` 部署
MON/MGR → osd_spec.yaml 加 `ceph orch apply` 部署 OSD → label + `ceph orch apply rgw` 部署
RGW → cephadm shell 中创建存储池与 RGW 账户）。内容提炼自《鲲鹏 BoostKit 分布式存储使能套件
Ceph 编译&部署&调优&测试指南》（文档版本 07，2025-06-30）第 5 章，命令与配置片段逐字保留。

## 边界表

| 场景 | 归属 |
|---|---|
| 对象存储 RGW（ceph-deploy 路径与 cephadm 容器化路径） | **本 skill** |
| 块存储 ceph-deploy 手工部署（MON/MGR/OSD/RBD 使能） | 姊妹 skill `ceph-block-storage-deployment`（同批姊妹 PR，同批交付） |
| 文件存储 MDS（CephFS） | 姊妹 skill `ceph-file-storage-deployment`（同批姊妹 PR，同批交付） |
| 集群运行期参数调优 | skill `ceph-parameter-tuning` |

## 铁律

1. **只读探测先行**：部署前先只读采集现状（`ceph -s`、`lsblk`、`ip add show`、容器环境版本）
   保留 before 证据；现状不明不开跑。
2. **双路径选择判断**：按 §0 对照表选路径——已有 ceph-deploy 运维习惯或需部署 14.2.8/16.2.7
   走路径一；新建 17.2.8 集群/容器化运维走路径二；两条路径的命令不得混用。
3. **版本与 OS 矩阵匹配**：路径一 = Ceph 14.2.8/16.2.7 + ceph-deploy 2.0.1/2.1.0；路径二 =
   Ceph 17.2.8 + Docker≥24（Ubuntu）/Podman≥2.0（openEuler，以 3.4.4 为例）。
4. **容器技术全局一致 + 本地仓库鉴权与 digest 固定**：同一集群 Ubuntu 用 Docker、openEuler 用
   Podman，不得混用；本地镜像仓库必须启用鉴权（htpasswd，口令交互输入或 vault 下发，不落文档
   与台账）并优先认证 + HTTPS（CA 装入各节点信任库），端口只绑管理网段地址（禁止 0.0.0.0 裸
   端口暴露）；cephadm DEFAULT_IMAGE 等生产引用按 skopeo inspect 记录的 digest（@sha256:...）
   固定，push/pull 后核对 digest 一致并记入台账；仓库信任配置（daemon.json/registries.conf）
   与登录凭据须同步到所有拉取节点。
5. **集群健康验证每阶段必过**：bootstrap/加 host/OSD/RGW 每步后执行 `ceph -s` 确认健康；
   路径一 RGW 部署后用 curl 网关 IP:端口 验证出现 RGW 信息；验证不过不进入下一步。
6. **破坏性操作二次确认 + 凭据安全**：卸载 Ceph（`cephadm rm-cluster` 按 fsid）属破坏性
   操作，执行前二次确认目标集群与 fsid；fsid 必须现场读取进变量后引用（如
   `CLUSTER_FSID=$(cephadm shell -- ceph fsid | tr -d '\r')`），且变量必须在宿主机 shell 赋值，
   不得在 cephadm shell 交互块内赋值（退出容器即失效），禁止引用文档示例 fsid，本地实例 fsid
   与台账记录不一致时停止并报告；keyring 与 access_key/secret_key 不入库不进 git 不落日志，
   每步回显证据入台账。
7. **TLS 校验保持开启**：不得为联网安装全局关闭证书校验（不得用 yum `sslverify=false`、不得用
   git 全局 `http.sslVerify false`、不得用 `wget --no-check-certificate`）；内网自签名镜像源把
   内部 CA 装入系统信任库（`cp <internal-ca>.crt /etc/pki/ca-trust/source/anchors/` +
   `update-ca-trust extract`），装 CA 后正常 git clone/wget；确需临时绕过时用命令级
   `git -c http.sslVerify=false`（仅单条命令生效，不写全局配置），并在台账记录。
8. **privileged 容器护栏**：`--privileged` 与 `--net=host`/`--ipc=host` 使容器获得接近宿主机
   root 的能力面（宿主机设备、内核、IPC 命名空间），仅限编译/部署/客户端部署容器场景使用，
   不得用于长期常驻；用后 `podman stop/rm` 回收，创建与回收动作记入台账（容器名、时间、用途）。
9. **写盘前核准清单**：`disk zap`/`wipefs`/`parted` 等写盘操作前必须逐机核准稳定设备 ID 清单
   （`lsblk -o NAME,SIZE,TYPE,MOUNTPOINT` + `ls -l /dev/disk/by-id`；`df -l /` 排根盘、
   `findmnt` 排挂载盘、`ceph-volume lvm list` 排现役 OSD），按清单逐盘执行，禁止范围式循环
   （如 `{a..l}`）直清；写盘前打印清单与规划表逐项核对，含系统盘/挂载盘/现役盘即停止。
10. **身份最小化**：业务客户端不复制 `client.admin` keyring、不加 `_admin` 标签（`_admin` 会
    下发 client.admin）；业务侧仅签发对象账户所需身份——RGW S3 用户即 `radosgw-admin user
    create` 产物，无需集群 CephX 身份；如需 RADOS 访问用限权 `client.<app>`（mon 'allow r'
    osd 'allow rw pool=<rgw 池>'）；管理动作只在管理节点/client.admin 持有者执行。

## 关键动作表

| 序 | 动作 | 参考 |
|---|---|---|
| ① | 两大部署路径选择对照表（版本/OS/容器技术/网关前端/适用判断） | `references/object-storage-deployment.md` §0 |
| ② | 路径一：环境要求、镜像源配置与 ceph-deploy 适配 | `references/object-storage-deployment.md` §1.1/§1.2 |
| ③ | 路径一：系统配置与部署前环境分析（MON/MGR/OSD/RGW 配置） | `references/object-storage-deployment.md` §1.3/§1.4 |
| ④ | 路径一：安装 Ceph 与部署节点（MON/MGR/OSD/RGW 实例） | `references/object-storage-deployment.md` §1.5 |
| ⑤ | 路径一：对象存储功能（crush rule/EC 存储池、RGW 账户、压缩放置策略 compress-placement） | `references/object-storage-deployment.md` §1.6 |
| ⑥ | 路径一：常见问题两则与故障排除三则、路径一回退（去 RGW 实例与存储池清理） | `references/object-storage-deployment.md` §1.7/§1.8/§1.9 |
| ⑦ | 路径二：容器环境安装（Podman/Docker）与 cephadm 安装 | `references/object-storage-deployment.md` §2.2/§2.3 |
| ⑧ | 路径二：自制 Ceph 17.2.8 容器镜像与本地仓库（registry:2、DEFAULT_IMAGE） | `references/object-storage-deployment.md` §2.4 |
| ⑨ | 路径二：bootstrap 部署 MON/MGR、OSD（osd_spec.yaml）、RGW（label + 端口）、客户端与对象存储功能 | `references/object-storage-deployment.md` §2.5/§2.6/§2.7 |
| ⑩ | 路径二：卸载 Ceph（rm-cluster 按 fsid，二次确认）与常见问题三则 | `references/object-storage-deployment.md` §2.8/§2.9 |

## 输出契约

- **部署清单**：所选路径与版本矩阵、节点角色表（MON/MGR/OSD/RGW/客户端）、三网 IP 规划、
  RGW 实例与端口规划（路径一 civetweb bucket 端口 / 路径二 beast port 与 label）。
- **健康验证结果**：每阶段 `ceph -s` 回显；路径一 curl 网关验证出现 RGW 信息、radosgw 进程
  在线；路径二 `ceph orch ls`/host 列表、OSD up、radosgw-admin 账户 access_key/secret_key
  获取回显（密钥不落日志）。
- **回退步骤**：路径一按 references §1.9「路径一回退」小节逐项执行并一一对应（ceph-deploy
  无 rgw delete 子命令，去 RGW 实例用 systemctl stop/disable + /var/lib/ceph/radosgw 清理；
  mon_allow_pool_delete 开/复位两步后 `ceph osd pool delete <pool> <pool>
  --yes-i-really-really-mean-it` 清存储池）；路径二给出卸载 Ceph 路径（现场 `ceph fsid` 读取
  变量化且在宿主机 shell 赋值、二次确认后 rm-cluster，所有节点执行）；容器/repo 配置还原
  （daemon.json、registries.conf 备份）。

## Dependencies

| 工具/资源 | 用途 | 缺失影响与降级 |
|---|---|---|
| ceph-deploy（2.0.1/2.1.0）+ python | 路径一部署 | 缺失时对象存储改走路径二（cephadm 容器化） |
| 容器环境（Ubuntu Docker≥24 / openEuler Podman≥2.0） | 路径二容器化部署与自制镜像 | 缺失时路径二不可用，回退路径一 |
| cephadm（Ubuntu apt / openEuler quincy 脚本） | 路径二编排 | 缺失时先补装并 cephadm -h 验证 |
| 本地镜像仓库（registry:2）与信任配置 | 路径二 openEuler 自制镜像分发 | 缺失时 Ubuntu 可用官方镜像，openEuler 需先搭本地仓 |
| `references/object-storage-deployment.md` | 全量命令 SOP（双路径） | 缺失时输出 blocked，不得凭记忆拼命令 |
