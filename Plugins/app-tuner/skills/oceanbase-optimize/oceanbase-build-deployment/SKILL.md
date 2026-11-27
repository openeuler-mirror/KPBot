---
name: oceanbase-build-deployment
description: >
  OceanBase CE 4.3.5 在鲲鹏/ARM64 与 x86_64 上的编译、部署、租户创建、验收与运维：
  clang/LLVM 工具链与 deps/init 三方依赖（protobuf、boost、isa-l、openssl、libaio、
  mariadb-connector-c、arrow/orc、rocksdb、oblogmsg、obshell 等）、build.sh 模式
  （release/perf/rpm；LTO、AutoFDO、HOTFUNC、BOLT（仅内部构建生效）、LSE/NOLSE）、mydeploy install.cfg
  与 cluster_install.sh、单机/三副本部署、租户与资源单元（primary_zone/locality）、
  NUMA/cgroup/绑核、OS 基线（THP、swap、sysctl、64K 页、nf_conntrack、sockmap）、
  验收、启停顺序、备份恢复、升级与回滚。内容来自鲲鹏 950/920B/7270Z 与 AMD 9T34
  实测部署，已参数化脱敏。用于从零搭建、复制部署、扩容、升级或排查部署问题；
  不适用参数调优（→ oceanbase-performance-optimization）、压测与正确性验证
  （→ oceanbase-test-verification）、热点归因（→ oceanbase-hotspot-analysis）。
  触发词：OceanBase 编译、OB 部署、OB 安装、observer、obproxy、install.cfg、mydeploy、
  OB 租户、资源单元、primary_zone、三副本、OB 升级、三方依赖、LSE、NOLSE、LTO。
license: MulanPSL-2.0
compatibility: OceanBase CE 4.3.5 源码树 4.3.5_CE_BP5_HF2；鲲鹏 950/920B/7270Z、aarch64、openEuler 24.03 / AntOS 6.6 实测；x86_64 可参考但需重新核对依赖清单与编译参数
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: relational_database
  version: "1.0.0"
---
# OceanBase 编译与部署

## 简介

在鲲鹏/ARM64（或 x86_64）上构建 OceanBase CE 4.3.5，并完成单机或三副本部署。
本文把“装依赖 → 编三方库/observer → 规划 NUMA 与端口 → 写 install.cfg → 起集群 →
建租户 → 验收 → 备份/恢复 → 升级/回滚”固定成可复现流程；所有命令参数化，可直接套用。

**实测基线**：鲲鹏 950/920B/7270Z、AMD 9T34；openEuler 24.03 / AntOS 6.6；
OB 4.2.5.5 性能基线、4.3.5 源码；observer 32U/250G 起（32U/64U/128U）。其他机型只改变量与 NUMA 布局。

## 何时触发

- 在鲲鹏/openEuler 上从零搭建或重建 OceanBase 集群（单节点 / 三副本 / 多实例混部）。
- 编译 4.3.5、切换 release/perf/rpm 构建、启用 LTO/AutoFDO/HOTFUNC（BOLT 在开源树不生效）、切换 LSE/NOLSE。
- 更新 `deps/init` 三方依赖、替换 LLVM 工具链、打包 RPM。
- 规划 NUMA（1NPS/2NPS）、cgroup 绑核、端口、数据目录、租户 Unit 与副本。
- 部署后验收失败：observer 起不来、obproxy 连不上、租户建不出、`SHOW` 看不到节点。

**不适用**：冻结/限流/线程/内存调参 → `oceanbase-performance-optimization`；
压测口径与正确性用例 → `oceanbase-test-verification`；火焰图与热点归因 →
`oceanbase-hotspot-analysis`。

## 变量约定

执行前先填表，正文与脚本统一引用这些变量；示例值仅为文档占位，不是真实环境。

| 变量 | 含义 | 示例 |
| --- | --- | --- |
| `${OB_SRC}` | 源码树根 | `/opt/src/oceanbase` |
| `${OB_HOME}` | observer 运行根目录 | `/opt/oceanbase` |
| `${OB_BASE}` | 数据/日志父目录 | `/ob` |
| `${BUILD_DIR}` | 构建目录（`build_<type>`） | `${OB_SRC}/build_release` |
| `${DEPS_DIR}` | 三方依赖安装前缀 | `${OB_SRC}/deps/3rd/usr/local/oceanbase` |
| `${ADMIN_USER}` | 部署账号（非 root） | `admin` |
| `${ADMIN_HOME}` | 部署账号 home | `/var/lib/oceanbase-admin` |
| `${CLUSTER_NAME}` | 集群名 | `obcluster` |
| `${MYSQL_PORT}` | observer MySQL 端口 | 2882 |
| `${RPC_PORT}` | observer RPC 端口 | 2881 |
| `${PROXY_PORT}` | obproxy 端口 | 2828 |
| `${OBSERVER_HOSTS}` | observer 地址列表 | `<host1>,<host2>,<host3>` |
| `${TENANT_NAME}` / `${TENANT_CPU}` / `${TENANT_MEM}` | 业务租户名 / CPU / 内存 | `mysql_tenant` / 10 / `20G` |
| `${PRIMARY_ZONE}` / `${LOCALITY}` | 主副本 Zone / 副本分布（手工 `CREATE TENANT` 写带 Zone 形式；install.cfg 模板才用 `FFF`/`F`） | `z1` / `F@z1`（单副本） |
| `${DATA_DIR}` / `${CLOG_DIR}` / `${BACKUP_ROOT}` | 数据 / clog / 备份目录 | `${OB_BASE}/data/1/obm`、`${OB_BASE}/log/obm`、`${OB_BASE}/backup` |
| `${OBS_CPUS}` / `${PROXY_CPUS}` / `${BENCH_CPUS}` / `${OBS_NUMA}` / `${PROXY_NUMA}` / `${STOP_TIMEOUT}` | 绑核范围 / 绑内存节点 / 停止等待秒 | `0-31`/`32-95`/`96-191`/0/1/120 |

凭据只允许通过环境变量或交互式 `read -rs` 注入，不写入配置文件、脚本、命令行或报告。

## 工作流总览

| 阶段 | 关键动作 | 材料 |
| --- | --- | --- |
| 1-2. 规划+前置 | 形态/NUMA/端口/磁盘/租户规划；只读预检（FAIL 即止） | §部署形态；`topology-and-os-baseline.md`；`scripts/preflight_check.sh` |
| 3-4. 构建 | `build.sh init`（deps/dep_create.sh）→ release/perf/rpm（LTO/AutoFDO/LSE） | `references/build-and-deps.md` |
| 5-6. 配置+部署 | `install.cfg` → `cluster_install.sh` → 租户/用户 → obproxy → 绑核 | `references/deploy-and-acceptance.md` |
| 7. 验收 | 节点/Zone/租户/Unit、读写路径、日志/目录 | 本文 §部署与验收 |
| 8-9. 备份/升级 | 数据+日志归档备份、恢复校验；二进制替换与回滚 | `references/operations-and-troubleshooting.md` |

## 部署形态选择

| 形态 | 组成 | 适用 | 注意 |
| --- | --- | --- | --- |
| 单节点单副本 | 1 observer（+obproxy） | 功能验证、单机基线 | `zone_num=1`、`locality=F`；无多数派 |
| 单机三副本 | 1 机 3 进程或 3 机各 1 | 架构/Paxos 验证 | 单机三副本仍共享 NUMA/IO |
| 三机三副本 | 3 observer + obproxy + 压测机 | 性能与业务模型 | 一主两备；备节点也有复制流量 |
| 多实例混部 | 32U×2+64U×1 等 | 容器化生产模拟 | 需 VF/中断/NUMA 隔离 |

选择原则：功能验证用单节点单副本；测 Paxos/多数派用三副本；贴近生产容器用多实例 + cgroup 绑 NUMA + VF 隔离（先验证线性度）。

## 前置检查

```
# 只读检查：架构/NUMA/内存/磁盘/依赖/端口/THP/swap/selinux/ulimit/chrony/残留进程
OB_BASE=${OB_BASE} OB_SRC=${OB_SRC} \
  MYSQL_PORT=${MYSQL_PORT} RPC_PORT=${RPC_PORT} PROXY_PORT=${PROXY_PORT} \
  bash scripts/preflight_check.sh
```

要求：

- 存在 `FAIL` 时不得开始部署；修复后重跑至无 FAIL。
- `WARN` 项（THP、numa_balancing、ulimit、chrony、nvme 调度器等）先确认影响再继续。
- 端口检查必须同时覆盖 observer（mysql/rpc）与 obproxy，避免“observer 起来了但代理连不上”。

## 构建（详见 references/build-and-deps.md）

最小路径：`./build.sh init`（装 deps/3rd 三方依赖）→ `./build.sh release --make -j` →
可选 `./build.sh perf --make -j`（LTO/AutoFDO/hotfunc）；RPM/DEB 用 `./build.sh rpm --make -j`。
产物：`build_release/src/observer/observer` 与运维工具；**obproxy 取自独立社区 RPM，非本源码树产物**。

版本事实（详见 build-and-deps.md §3/§4）：4.3.5 开源树 BOLT 门控恒假（不要记为已生效优化）、
无 PGO/CSPGO 开关、devtools 无 ninja。编译优化只在要对比性能时用，且单独记录编译参数
（编译器/目标 CPU/LSE/LTO/AutoFDO/HOTFUNC），不与基线混用。

## 配置要点（完整模板见 references/deploy-and-acceptance.md）

- **端口**：以 `install.cfg` 为准（本系列 mysql 2882 / rpc 2881 / proxy 2828）；客户端、obproxy、observer 三处一致。
- **CPU/内存**：`cpu_count`/`memory_limit` 按规格（32U→250G，64U/128U 按倍数）；租户 `cpu_num`/`memory_size` 随规格放大，否则跑不满。
- **目录**：data/slog 与 clog 分盘，XFS + `noatime,nodiratime,pquota`。
- **locality/zone**：模板单节点 `zone_num=1`、`locality=F`（三副本 `FFF`）；手工 `CREATE TENANT` 按资源池实际 Zone 写（`F@z1,F@z2,F@z3`/`F@z1`）；`primary_zone` 放压测机同 Zone 减少跨机 RPC。
- **绑核/obproxy**：绑核绑内存必须在**启动时**（`numactl -C -m` 或 cgroup；后 `taskset` 只改 CPU 留跨 P 访存）；obproxy `work_thread_num` 匹配连接数并与 observer 同 NUMA/P。

## 部署与验收

启动顺序（强约束）：

```
observer（三副本全部起来，Zone 注册完成）→ 建租户/用户 → obproxy
```

停止顺序：

```
obproxy → 业务停压 → observer（先备后主，或按运维平台顺序）
```

> 部署脚本入口：`cluster_install.sh --observer <path> --obproxy <path> --conf install.cfg --deps_path ${ADMIN_HOME}`。脚本需以部署账号运行，路径权限归部署账号。

验收清单（完整命令与通过标准见 `references/deploy-and-acceptance.md` §6/§7）：

| 检查 | 命令族 | 通过标准 |
| --- | --- | --- |
| 进程/监听 | `ps -ef`、`ss -lntp`（observer mysql/rpc + obproxy 三端口） | 进程数与规划一致、无反复重启、端口全在 |
| 节点/Zone | `oceanbase.DBA_OB_SERVERS`、`DBA_OB_ZONES` | 全部 `ACTIVE`，Zone 数与 `zone_num` 一致 |
| 租户/Unit | `DBA_OB_TENANTS`、`DBA_OB_UNITS` + `DBA_OB_UNIT_CONFIG`（两步核对） | 业务租户存在，CPU/内存与规划一致 |
| 读写路径 | 业务表 `COUNT(*)`/抽样行；隔离库建表 INSERT→SELECT→DELETE | 返回非空、值与事务可见性正确 |
| 日志/目录 | `tail observer/clog 日志`、`du -sh` 数据/clog 目录 | 无 `ERROR`/`disk full`/`paxos` 异常，分盘合理 |

## 备份、恢复与升级

完整命令与校验清单见 `references/operations-and-troubleshooting.md`。规则：

- 备份分**数据备份**与**日志归档**（两者都要才能恢复到任意时间点）；目标（NFS/S3/OSS）先做连通与容量检查，恢复前确认集群无写入、同集群/租户名不覆盖生产。
- 升级前备份 + 记录二进制 md5/commit 与编译参数 + 确认数据格式兼容；升级后跑功能与性能验收。
- 二进制替换必须先停进程再覆盖（运行中会 `Text file busy`），保留 `bin.bak.<ts>`；优先隔离环境演练恢复。

## 安全基线

- **凭据不入库不落 argv**（命令行可被 `ps`/`/proc/<pid>/cmdline` 读）：用环境变量 + `read -rs` 注入，会话结束 `unset`；禁止 `-p "<密码>"` 写法。
- **SSH**：`StrictHostKeyChecking=accept-new`（禁止 `no` 与 `/dev/null` known_hosts）；密钥变更先查原因再 `ssh-keygen -R`。
- **下载物** sha256/GPG 校验；**禁止从网络管道直接执行下载的脚本**（先把脚本落盘、校验后再执行）；observer 不以 root 运行，备份目录单独授权。
- **变更可回滚**：保留原配置与二进制备份，失败按停止顺序回滚并复验。

## 错误处理

| 症状 | 处置 |
| --- | --- |
| `build.sh` 未初始化依赖/找不到 clang | 先 `./build.sh init`；查 `deps/3rd/.../devtools/bin/` 与 `obdevtools-llvm-*`；aarch64 链接 `-latomic` 失败补静态 libatomic |
| `-march` 与预编译库不匹配 | LSE/NOLSE 与三方库/工具链保持一致，不混用 |
| observer 启动即退出 | 看 observer 日志与 `dmesg`：内存/目录权限/clog 空间/core dump |
| 租户建不出来/一直 `CREATING` | 单节点 `locality=FFF` 错——改 `F`，确认 Zone/Unit 足够 |
| obclient 连不上/租户不存在 | 端口混用（2881/2882/2828）或用户名格式错（`user@tenant`）；备节点不同步查 RPC 端口/时钟/Paxos 日志 |
| 启动后跨 NUMA 访存 / `Text file busy` | 绑定须在启动时（`numactl`/cgroup，勿“先启动后 taskset”）；覆盖二进制先停进程 |
| 磁盘满导致冻结/写入失败 | 查 data/clog 分盘、`datafile_size`/`log_disk_size`，清理归档/备份 |

## 输出契约

- **新部署**：变量表、形态、编译参数与工具链、端口/NUMA/磁盘布局、验收结果、二进制 md5 与 commit。
- **构建**：模式、依赖与编译器版本、LTO/AutoFDO/HOTFUNC/LSE 状态（BOLT 开源树恒不生效）、产物清单。
- **租户/备份/升级**：租户名与 Unit、副本与 primary_zone；备份类型与路径、恢复后校验；改前/改后 md5、停机窗口、回滚命令与复验。
- **任何变更**：改前→改后、验证命令、回滚方式、风险与确认记录。

## 参考资料与脚本

| 文件 | 何时读 |
| --- | --- |
| `references/build-and-deps.md` | 三方依赖作用、`dep_create.sh`、build.sh 模式、编译参数、打包与编译报错 |
| `references/topology-and-os-baseline.md` | 规格与 NUMA（1NPS/2NPS）、cgroup/绑核、端口、sysctl/THP/swap/ulimit、磁盘与网络基线 |
| `references/deploy-and-acceptance.md` | install.cfg 逐项说明、cluster_install、建租户/用户、obproxy、启停、验收 SQL |
| `references/operations-and-troubleshooting.md` | 备份/恢复、升级/回滚、日志位置、常见故障与排查顺序 |
| `scripts/preflight_check.sh` | 部署前只读检查（PASS/WARN/FAIL，有 FAIL 退出非零） |
