---
name: 3fs-deployment-installation
description: >
  鲲鹏平台（aarch64，920 实测）/ openEuler 24.03 上 DeepSeek 3FS (hf3fs) 分布式存储的部署安装与
  日常变更：全集群组件部署（mgmtd / meta / storage / FUSE / ClickHouse / FoundationDB /
  monitor_collector）、3FS 与 USRBIO（hf3fs_usrbio.so）源码编译、chain table 与 target 创建、
  部署验收、FUSE 客户端扩容、二进制滚动替换与回滚、admin_cli 日常运维（服务启停、配置下发、
  chain/target 管理），配套部署前置检查与滚动替换脚本；内容提炼自两套真实集群部署实录，
  已脱敏参数化，含 FUSE systemd 缺 --cfg 写性能降为 1/4、异构网卡名 filter_list 等实录坑位。
  当需要搭建、编译、验收、扩容或升级 3FS 集群，以及 admin_cli 运维与部署排障时使用；
  不适用非 3FS 项目（参数调优 → skill `3fs-parameter-tuning`，热点与代码优化 →
  skill `3fs-hotspot-code-optimization`）。
license: GPL-3.0
compatibility: 鲲鹏系列 (aarch64, 920 实测), openEuler 24.03 LTS (SP2/SP3/SP4), 3FS commit 49794159, Mellanox ConnectX-5 RDMA (IB/RoCE)
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: distributed_storage
  version: "1.0.0"
---

# 3FS 部署安装

## 简介

在鲲鹏平台 / openEuler 24.03 上从零部署 DeepSeek 3FS 分布式存储并完成日常变更（步骤与具体机型无强绑定，实测环境为鲲鹏 920）。知识提炼自两套鲲鹏 920 真实集群
（stage：2 storage + 3 FUSE 客户端；tester：3 节点从零安装）的部署实录，已脱敏：正文一律引用集群变量表，
示例 IP 使用文档保留网段 192.0.2.x / 198.51.100.x / 203.0.119.x。

## 何时触发

- 在鲲鹏 / openEuler 上从零搭建（或整体重建）3FS (hf3fs) 集群：组件部署、chain table / target 创建、验收
- 编译 3FS / USRBIO（`hf3fs_usrbio.so`）、切换 folly 优化分支重编译、排查 3FS 的 CMake 或毕昇编译器报错
- 向既有集群新增 FUSE 客户端（扩容）
- 重编译后滚动替换 / 升级 / 回滚集群二进制
- 日常运维：admin_cli 巡检、服务启停、配置下发、chain/target 管理、UNAVAILABLE chain 恢复

## 工作流总览

| 阶段 | 关键动作 | 材料 |
|------|---------|------|
| 1. 规划 | 节点角色、双网段、端口规划、node_id/chain/target 编号方案 | `references/installation-guide.md` §1 |
| 2. 前置检查 | hostname/hosts、RDMA link 与网卡名、时间同步、端口空闲、依赖 | `scripts/preflight_check.sh`（逐节点执行，只读） |
| 3. 构建环境 | yum 依赖、Rust / FoundationDB / 毕昇编译器 / Libfuse | `references/build-notes.md` |
| 4. 源码编译 | 3FS + openEuler 补丁、folly 优化分支、三处必需 CMake 修改、USRBIO/fio | `references/build-notes.md` |
| 5. 集群部署 | ClickHouse → FDB → monitor_collector → admin_cli → mgmtd → meta → storage → chain table → FUSE | `references/installation-guide.md` §5 |
| 6. 验收 | list-nodes / list-chains / 挂载 / 容量 / 端口 | `references/installation-guide.md` §6 |
| 7. 日常运维 | 升级（二进制替换）、客户端扩容、配置变更、chain 管理、排障 | `references/cluster-operations.md`、`references/binary-replacement.md`、`references/fuse-client-expansion.md`、`scripts/replace_3fs_bin.sh` |

部署顺序强约束：monitor_collector → mgmtd → meta → storage → chain table/target → FUSE；
停服按反序（FUSE → storage → meta → mgmtd → monitor_collector）。跳步会导致心跳或 chain 状态异常。

## 集群变量表约定

所有文档与脚本均以下列变量参数化，执行前先按实际集群填好（示例值为文档保留网段，非真实环境）：

| 变量 | 含义 | 示例 / 默认 |
|------|------|------------|
| `${CLUSTER_ID}` | 集群 ID | stage |
| `${META_IP}` | meta 节点 RDMA 网 IP | 192.0.2.10 |
| `${STORAGE1_IP}` / `${STORAGE2_IP}` | storage 节点 RDMA 网 IP | 192.0.2.11 / 192.0.2.12 |
| `${FUSE_CLIENT_IPS}` | FUSE 客户端 RDMA 网 IP 列表 | 192.0.2.20 … |
| `${MGMT_NET}` / `${RDMA_NET}` | 管理网 / RDMA 网段 | 198.51.100.0/24 / 192.0.2.0/24 |
| `${RDMA_IFACE}` | RDMA 网卡名（配置 filter_list 用） | ibp129s0f0（IB 模式）/ enp43s0f0np0（以太模式） |
| `${SSH_USER}` | 节点间 SSH 用户 | root |
| `${SRC_ROOT}` | 源码根目录（含 3fs/ 与 fio/ 两棵源码树） | /opt/src/3FS |
| `${BUILD_DIR}` | 编译输出目录 | ${SRC_ROOT}/3fs/build |
| `${INSTALL_PREFIX}` | 线上安装前缀 | /opt/3fs |
| `${MOUNT_POINT}` | FUSE 挂载点 | /3fs/stage |
| `${CK_PORT}` / `${CLICKHOUSE_PASSWORD}` | ClickHouse TCP 端口 / 密码 | 9123 / `read -rs` 注入环境变量（clickhouse-client 自动读取，不落 argv），不入库 |
| `${FDB_PORT}` | FoundationDB 端口 | 4500 |

主机角色名（meta、storage1、storage2、client2 等）在文档中保留为示例角色名；
node_id/chain_id/target_id 编号（1、100、10001、900100001 等）为示例编号方案，按集群规划替换。

## 安全基线（凭据不入库、不落 argv）

- 任何明文密码 / token 不得写入脚本、文档、配置文件样例或 shell history。
- **凭据不得出现在进程命令行**：argv 可被同机任意用户经 `ps -ef` / `/proc/<pid>/cmdline` 读取。
  ClickHouse 密码用 `CLICKHOUSE_PASSWORD` 环境变量注入（`read -rs` 交互输入，clickhouse-client
  自动读取；或 `--password` 不带值触发交互提示），禁止 `--password "<值>"`；
  admin_cli 的 token 写入 `${INSTALL_PREFIX}/etc/admin_cli.toml` 的 `[user_info]` 段
  （`chmod 600`）随 `-cfg` 携带，禁止 `--config.user_info.token <值>` 命令行覆盖。
- 交互式注入：`read -s -p 'Password: ' SSHPASS; export SSHPASS`，远程命令统一 `sshpass -e`；
  严禁用 sshpass 的 `-p` 选项在命令行携带明文密码。
- 优先配置 SSH key 免密（`ssh-copy-id ${SSH_USER}@<节点>`），仅在无法使用 key 时退回 sshpass -e；
  集群侧建议关闭口令登录。
- **SSH 主机密钥默认校验**：统一 `StrictHostKeyChecking=accept-new`（首次纳管信任并持久化
  known_hosts，密钥变更则拒连）；禁止 `no` 与 `UserKnownHostsFile=/dev/null` —— 管理网被劫持时
  可冒充节点截获口令、投放恶意二进制，且不留取证痕迹。主机密钥变更先查明原因（如节点重装），
  再 `ssh-keygen -R <host>` 清除旧键后重连。
- **下载物安装前必须校验完整性**（sha256 / GPG 签名，见 `references/build-notes.md` §2 门禁），
  禁止 `curl … | sh` 把远程内容直接交给 root shell，禁止 `-k` 关闭 TLS 校验。
- admin 用户 token 文件 `${INSTALL_PREFIX}/etc/token.txt` 与 `admin_cli.toml` 均 `chmod 600`。
- 会话结束清理：`unset SSHPASS RSYNC_RSH CLICKHOUSE_PASSWORD`。

## 参考资料索引（渐进披露）

| 文件 | 何时读 |
|------|--------|
| `references/installation-guide.md` | 从零搭建集群或整体重建时：环境要求、前置检查、逐组件部署、chain table、验收、逐节点必改文件汇总表 |
| `references/build-notes.md` | 编译 3FS / USRBIO、切换 folly 分支重编译、排查 CMake 与编译器报错时 |
| `references/cluster-operations.md` | 日常巡检、配置变更（get-config/set-config 范式）、chain/target 管理、端口规划、日志与 coredump 排查时 |
| `references/fuse-client-expansion.md` | 向既有集群新增 FUSE 客户端（扩容）时 |
| `references/binary-replacement.md` | 重编译后滚动替换 / 回滚集群二进制（手工 SOP，含 UNAVAILABLE chain 恢复）时 |

## 脚本索引

| 脚本 | 用途 |
|------|------|
| `scripts/preflight_check.sh` | 部署前置检查（只读、无副作用）：hostname/hosts、RDMA、时间同步、端口、依赖；输出 PASS/FAIL 汇总，有 FAIL 时退出码非零 |
| `scripts/replace_3fs_bin.sh` | 集群二进制滚动替换：预检查 → 顺序停服 → 备份（`bin.bak.<时间戳>`）→ 覆盖 + md5 校验 → 顺序启动 → 验证；内建 `--rollback [bin.bak.<时间戳>]` 一键回滚（停服 → 现场保留为 `bin.failed.<时间戳>` → 恢复备份 → md5 抽查 → 启动 → 验证）；节点清单/用户/路径全部 env 或 inventory 参数化，凭据仅支持 SSH key 或 SSHPASS 环境变量，SSH 默认 `StrictHostKeyChecking=accept-new` |

## 不适用

- 非 3FS（hf3fs）项目的通用存储部署。
- 3FS 性能参数调优（num_bg_threads、traffic_control、chain table HEAD 分布调优等）→ skill `3fs-parameter-tuning`。
- 3FS 热点定位与代码级优化（含 CRC32C/校验和专项案例）→ skill `3fs-hotspot-code-optimization`。

## 输出契约

按场景产出（均要求可复验、可回滚）：

- **新部署 / 重建**：验收清单结果 —— `list-nodes` 全部节点 `HEARTBEAT_CONNECTED`（mgmtd 为 `PRIMARY_MGMTD`）、`list-chains` 状态统计、挂载点 `df`、端口监听核对；附逐节点必改文件汇总表（`references/installation-guide.md` §7）
- **前置检查**：`scripts/preflight_check.sh` 的 PASS/WARN/FAIL 汇总；存在 FAIL 时退出码非零，修复后必须重跑至无 FAIL
- **二进制替换**：`scripts/replace_3fs_bin.sh` 执行记录 —— 备份目录路径、逐文件 md5 校验结果、停启顺序回显、验收结果（list-nodes 计数 + 挂载 + 版本串）、回滚命令
- **客户端扩容**：新客户端 `list-nodes` 注册证据 + `df`/`mount` 验证 + rsync 推送文件清单
- **任何变更类操作**：变更记录（节点、文件/服务、改前 → 改后、验证命令、回滚方式）+ 凭据注入方式与 `unset` 清理声明

## 错误处理

| 症状 | 处置 |
|------|------|
| `preflight_check.sh` 出现 FAIL | 按项修复（hostname/hosts 解析、RDMA link ACTIVE、时间偏差、端口占用、依赖包）后重跑；WARN 项确认影响后再开始部署 |
| `list-nodes` 缺节点或无 `HEARTBEAT_CONNECTED` | 核对服务启动顺序（mgmtd → meta → storage → FUSE）与 mgmtd 地址/端口可达性；`journalctl -u <服务> -n 100 --no-pager`；逐组件排查见 `references/installation-guide.md` |
| chain 状态 UNAVAILABLE / 非 UPTODATE | 查 target 盘挂载（`mount`/`df`）与 storage 日志；二进制替换后按 `references/binary-replacement.md` 用 `rotate-lastsrv` 恢复（chain 列表从 `list-chains` 动态获取） |
| FUSE 挂载失败或写性能只有其它客户端的 1/4 | 按优先级排查：systemd ExecStart 缺 `--cfg` → `filter_list` 网卡名与本机实际不符 → token/fdb.cluster 未推送或权限不对 → `ldd` 缺库 → RDMA link 非 ACTIVE → mgmtd 不可达（`references/fuse-client-expansion.md` §12） |
| 启动前 `ldd` 显示 not found | 绝不启动服务；补齐缺失库（`libfdb_c.so`、`libhf3fs_api_shared.so` 等）并 `ldconfig` 后复检 |
| `replace_3fs_bin.sh` 中途 die（停止/启动失败、挂载无法释放等） | 脚本 trap 已打印全部服务状态快照；执行 `replace_3fs_bin.sh --rollback`（自动选最新 `bin.bak.*`，现场保留为 `bin.failed.*`）恢复后重新定位原因，或按 `references/binary-replacement.md` 手工介入 |
| CMake / 编译报错 | 对照 `references/build-notes.md` 的三处必需修改（zstd `BUILD_INTERFACE` 包装、删 `-fcoroutines-ts`、rocksdb `FAIL_ON_WARNINGS=OFF`）与 folly 本地 patch 清单 |
| ClickHouse / FoundationDB 连接失败 | 核对端口（CK `${CK_PORT}` 默认 9123、FDB 4500）、密码注入方式（`read -s`，不进 history）、`fdb.cluster` 与 public-address 配置 |
| 节点重启后配置"回退" | 改参后未同步 `${INSTALL_PREFIX}/etc/` 下同名 toml —— 以 `get-config` 实际导出为准修正本地文件（配置管理范式见 `references/cluster-operations.md`） |
