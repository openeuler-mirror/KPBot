---
name: 3fs-parameter-tuning
description: >
  3FS 分布式存储集群参数调优：覆盖 storage/FUSE/meta/mgmtd 配置项（sync_when_write、
  enableChecksum、traffic_control 等）、拓扑与数据放置（副本数、chain table、HEAD 分布）、
  BIOS/NVMe 固件核对、ClickHouse 观测与 fio 标准压测的调优与 A/B 对比，产出可回滚的
  变更记录与带证据的结论。当需要对 3FS 集群做参数调优、压测口径对齐、配置 A/B、
  chain table 调整，或排查 3FS 慢 / 带宽上不去等配置面问题时使用；
  鲲鹏 920 / openEuler 24.03 实测。
license: GPL-3.0
compatibility: Linux（aarch64 / openEuler 24.03 实测），3FS 集群（admin_cli + mgmtd），fio + hf3fs_usrbio.so，ClickHouse
metadata:
  skill_type: tool_assisted
  category: tuning
  domain: distributed_storage
  version: "1.0.0"
---

# 3fs-parameter-tuning — 3FS 集群参数调优闭环

## 简介

对已部署运行的 3FS 集群做**参数级**性能调优，产出带证据链的 A/B 结论与可回滚的变更记录。闭环：

```
基线测量 → 杠杆定位 → 快照备份 → 改参下发 → 复测验证 → 无效回滚 → 台账记录
   ↑                                                                    │
   └────────────────────── 下一个变量（一次只动一个）────────────────────┘
```

实测背景：两套集群 A/B 对照 —— A（3 storage，顺序写 8~10.5 GiB/s）与 B（2 storage，顺序写 3 GB/s，最终根因是 NVMe 固件）。**部署侧杠杆普遍比代码侧大一个数量级**（配置 `enableChecksum=false` 得 7.8x，同期代码侧 crc32c 替换只有 CPU-秒 -6.1%、带宽 0%）。

## 何时触发

| 场景 | 归属 |
|---|---|
| 3FS 集群参数调优、压测口径、配置 A/B、拓扑/chain table 调整、BIOS/固件核对、ClickHouse 指标对比 | **本 skill** |
| 3FS 部署、安装、二进制编译分发、服务编排、集群初建 | skill `3fs-deployment-installation` |
| 热点归因（火焰图 / bpftrace / perf 取证）、代码级优化（crc32c、fdatasync 根因、Rust/C++ 改动、checksum 实现选型） | skill `3fs-hotspot-code-optimization` |
| 非 3FS 系统（Ceph、GPFS、Lustre 等） | 不适用 —— 本文参数名、admin_cli 命令、指标名均为 3FS 专有 |

**入口前置**：调参前先用 skill `3fs-hotspot-code-optimization` 确认瓶颈在**软件配置面**，而不是代码或硬件。B 集群曾把固件问题当成 CRC 算法问题，据此重建全部 target，白做一轮。

## 核心纪律

1. **一次只动一个变量**。同期发生多件事时，任何"显著收益"都先怀疑是另一件事造成的。
2. **配置面快照以 `get-config` 实际导出为准**，不看节点本地 `/opt/3fs/etc/*.toml` 缓存（两者可能不一致）；A/B 两腿必须同 ConfigVersion、同二进制 md5。
3. **压缩率 / 带宽 / CPU-秒 / 延迟分位数分开记录**，不用一个数字代表"变快了"；延迟看 p50/p99/直方图，均值会掩盖长尾。
4. **先查两张表再动手**：`references/lever-ranking.md` 的收益排序表（该动哪个）与已证伪清单（哪些别再试）。
5. **高风险杠杆必须先与用户确认数据安全性**：`enableChecksum=false`（失去端到端校验）、`sync_when_write=false`（掉电丢 WAL）、副本数 3→2（容错下降）。
6. **执行顺序**：先修部署缺陷类（FUSE 缺 `--cfg`、二进制不一致、`force_use_tcp=true`）→ 再查硬件类（5 秒钟的 `nvme id-ctrl`）→ 再做低风险配置 → 最后才碰高风险配置与拓扑。
7. **改完要同步各节点 `/opt/3fs/etc/` 同名 toml**，否则下次重启用旧文件启动，A/B 结论失效。
8. **密码/Token 不落盘、不进命令行历史**：`read -rs` 注入环境变量，SSH 用 key 或 `sshpass -e`。

## 工作流程

| 步 | 动作 | 工具 / 参考 |
|---|---|---|
| 1 | 基线测量：同口径压测 ≥3 次，记录带宽/IOPS/延迟分位数 + ConfigVersion + 二进制 md5 | `scripts/fio_bench.sh`、`references/benchmarking.md` |
| 2 | 杠杆定位：查收益排序表选定杠杆，查已证伪清单排除死路 | `references/lever-ranking.md` |
| 3 | 快照备份：`get-config` 导出四类节点配置，另存带版本号的文件 | `scripts/set_config_param.sh`（自动备份）、`references/config-mechanics.md` |
| 4 | 改参下发：段落感知改一个 key → `set-config --desc` → 回读校验 → 判断热更新/需重启 → 最小重启面 | `scripts/set_config_param.sh`、`references/config-mechanics.md` §1.3 |
| 5 | 复测验证：同口径重跑 + 每客户端分解 + ClickHouse 延迟拆分 + RDMA 带宽 | `references/observability.md`、`references/benchmarking.md` |
| 6 | 无效回滚：用备份文件重新 `set-config`，按顺序重启，复测确认回到基线；结论写入已证伪清单 | `references/case-sync-when-write.md` §四 |
| 7 | 台账记录：按下方模板落一条记录 | 本文 §调参台账 |

拓扑类改动（副本数、chain table、HEAD 分布）走同一闭环，但第 4 步换成 chain table 重建/rotate 流程，且**切换 chain table 只需换压测目录、无需重启服务**：见 `references/topology-and-placement.md`。

## 节点与路径约定

references 中一律用角色名指代节点，示例 IP 为文档占位地址（RFC 5737 / RFC 3330 文档段），实际以集群 inventory 为准：

| 角色 | 示例管理网 IP | 示例 RDMA/IB IP | 说明 |
|---|---|---|---|
| meta | 192.0.2.10 | 203.0.119.10 | MGMTD + META + monitor_collector + ClickHouse + FDB + FUSE 客户端 |
| storage1 | 192.0.2.11 | 203.0.119.11 | `storage_main`，node_id 10001 |
| storage2 | 192.0.2.12 | 203.0.119.12 | `storage_main`，node_id 10002 |
| storage3 | 192.0.2.13 | 203.0.119.13 | 3 副本拓扑时启用 |
| fuse-client-1 | 198.51.100.21 | 203.0.119.21 | FUSE 客户端（压测发起端） |
| fuse-client-2 | 198.51.100.22 | 203.0.119.22 | FUSE 客户端（压测发起端） |

约定路径（可作默认值）：`/opt/3fs/bin/admin_cli`、`/opt/3fs/etc/admin_cli.toml`、`/opt/3fs/etc/{storage_main,meta_main,mgmtd_main,hf3fs_fuse_main,hf3fs_fuse_main_launcher,monitor_collector_main}.toml`、`/opt/3fs/etc/token.txt`、`/usr/lib64/hf3fs_usrbio.so`、挂载点与压测目录用 `${MOUNT_POINT}` / `${FIO_DIR}` 占位。

## 脚本工具

- `scripts/fio_bench.sh` —— 压测统一入口。三维度：`ENGINE=usrbio|fuse`、`MODE=single|multi`、`WORKLOAD=micro|ai|mixed`；含彩色日志、PASS/FAIL 计数、环境检查、读数据预写入（存在即跳过）、BW/IOPS 解析与 GiB/MiB/KiB 统一折算、多客户端 SSH 并行与集群总带宽汇总、退出时清理远程 fio、可选 RDMA 带宽监控集成（`RDMA_MON_LIB`）。**无任何明文密码**（`SSHPASS` + `sshpass -e`）。
- `scripts/set_config_param.sh` —— 单项配置热更新范式：`get-config` 导出 → awk 状态机段内定位 → python3 段落感知替换（匹配数 ≠ 1 即报错）→ 幂等 → `set-config --desc` → 回读校验 → 打印 ConfigVersion 变化与回滚命令。用法 `set_config_param.sh <STORAGE|META|FUSE|MGMTD|CLIENT> <toml_section> <key> <value>`。

```bash
# 基线（改参前后各跑一次）
NUMJOBS=32 BS=4M IODEPTH=8 WRITE_SIZE=2g bash scripts/fio_bench.sh seq-write
# 改一个参数（含备份、下发、回读、回滚命令）
bash scripts/set_config_param.sh STORAGE server.targets.storage_target.kv_store sync_when_write false
# 多客户端集群总带宽 + RDMA 监控
read -rs SSHPASS; export SSHPASS
MODE=multi NODES="<fuse-client-1> <meta> <fuse-client-2>" \
  RDMA_MON_LIB=/path/to/rdma_monitor.sh REMOTE_NODES="<storage1> <storage2>" \
  bash scripts/fio_bench.sh seq-write
```

## 参考资料索引

| 文件 | 何时读 |
|---|---|
| `references/lever-ranking.md` | **动手前必读**：9 项杠杆的收益/风险排序表 + 无效/反效果/已证伪清单（配置类、代码类、拓扑类、部署缺陷类、归因错误、环境陷阱） |
| `references/config-mechanics.md` | 要读/改/下发配置时：快照与热更新机制（`get-config`/`set-config`/ConfigVersion、`CONFIG_HOT_UPDATED_ITEM`、哪些参数需重启）、改参标准流程、逐项配置详解 |
| `references/topology-and-placement.md` | 动副本数/chain table/HEAD 分布时：写路径跳数与网卡双工约束、2 storage 下 HEAD 分散失败证据、chain table 与目录绑定、2 副本重建通用流程（CSV 循环）、时钟同步伪故障 |
| `references/hw-and-firmware.md` | 软件面排查完仍不达标、或跨集群对比前：Redfish 读 BIOS（鲲鹏属性名与 x86 不同名）、实测属性值、无 cpufreq 说明、NVMe 固件 fdatasync 长尾的 5 秒前置检查与定界证据链 |
| `references/observability.md` | 要用指标证明改动有效时：monitor_collector + ClickHouse 架构、关键指标名、标准 SQL 与聚合规则、时钟偏移校正（必做）、延迟拆分定位层次、RDMA 带宽监控用法 |
| `references/benchmarking.md` | 要产生可入账的带宽数字时：标准压测命令、口径纪律、`scripts/fio_bench.sh` 的三维度用法与 env 旋钮表、输出解读、USRBIO 与 FUSE 路径差异与选型 |
| `references/case-sync-when-write.md` | 需要"标准调参动作序列"模板时：`sync_when_write=false` 的完整闭环案例（证据 → 备份 → 改参 → 重启 → 回读 → 10068→12767 MiB/s +26.8% → 回退 → 风险评估 → 后续方向） |

## 输出契约

每轮调参产出三件套，缺一即未完成：

1. **变更记录**：变量（`段.键`）、改前 → 改后、ConfigVersion 变化、下发通道（热更新 / 需重启及最小重启面）、备份文件路径、回滚命令（`scripts/set_config_param.sh` 执行后自动打印）
2. **A/B 复测结果**：数字分列 —— 总带宽 + 每客户端分解、CPU-秒、延迟分位数（p50/p99）、RDMA TX/RX 峰值与均值；附两腿口径（ENGINE/MODE/WORKLOAD、`--size`、numjobs、bs、iodepth、fio 版本、客户端数、重复次数）与二进制 md5，两腿不同口径的数字不得对比
3. **台账条目**：按 §调参台账 模板落一行；结论只有 有效 / 无效 / 劣化 三种，无效与劣化必须同步记入已证伪台账（与 `references/lever-ranking.md` 的清单同格式）

高风险杠杆（`enableChecksum=false`、`sync_when_write=false`、副本数 3→2）必须附用户数据安全确认记录；无确认记录不得标记"有效"。

## 错误处理

| 症状 | 处置 |
|------|------|
| `fio_bench.sh` 报"挂载点不可用" | 查 FUSE 挂载（`df -h` / `mount`）；挂载失败移交 skill `3fs-deployment-installation` 的排查清单，不要带病压测 |
| `fio_bench.sh` 报 USRBIO 引擎不存在 | `USRBIO_SO=` 指定实际路径，或先编译 `hf3fs_usrbio.so`（见 `3fs-deployment-installation` 的 build-notes） |
| multi 模式节点预检失败 | 按脚本提示逐节点核对：SSH 认证（key / SSHPASS）、fio 路径、引擎 so、挂载点；报"设置了 SSHPASS 但未安装 sshpass"→ `yum install -y sshpass` 或改用 key |
| 带宽异常低（比排序表低一个量级） | 停止调参，先查部署缺陷类：FUSE systemd 缺 `--cfg`、`force_use_tcp=true`、各节点二进制 md5 不一致（`references/lever-ranking.md` 部署缺陷类——修了就是数倍收益） |
| `set_config_param.sh` 报替换匹配数 ≠ 1 或拒绝重复段 | 目标 key 不在该 section，或命中 `[[array.of.tables]]` 重复段；人工核对 `get-config` 导出的段结构后再操作，不得强改 |
| `set-config` 后 ConfigVersion 未递增 / 回读不一致 | 查 admin_cli 的 mgmtd 地址与 token；重试一次并以 `get-config` 回读为准；仍不一致按变更失败处理并回滚 |
| ClickHouse 查询为空或数字对不上 | 核对时钟偏移校正（必做）、指标名的版本存在性、monitor_collector `filter_list` 网卡名（`references/observability.md`） |
| 改参后复测无收益 | 按工作流程第 6 步用备份回滚并复测确认回到基线；结论记入已证伪台账；**不得叠加第二个变量"补救"** |
| 改参后服务异常 | 立即用备份文件 `set-config` 回滚 + 按最小重启面重启；保留 `journalctl` 现场再分析原因 |

## 调参台账

每改一个变量记一行；数字分列，不合并成"性能提升 X%"：

| 字段 | 示例 |
|---|---|
| 日期 / 操作人 | |
| 变量（段.键） | `server.targets.storage_target.kv_store.sync_when_write` |
| 改前 → 改后 | `true` → `false` |
| ConfigVersion | 15 → 16 |
| 二进制 md5（两腿） | |
| 压测口径 | ENGINE/MODE/WORKLOAD、`--size`、numjobs、bs、iodepth、fio 版本、客户端数、重复次数 |
| 带宽（总 + 每客户端） | 10068 → 12767 MiB/s（+26.8%） |
| CPU-秒 / 压缩率 / 延迟分位数 | 分列记录 |
| RDMA TX/RX 峰值与均值 | |
| 重启面 | storage 全节点 + FUSE 全客户端 |
| 风险与用户确认 | 掉电丢 WAL；已确认测试环境 |
| 回滚命令 / 备份文件 | |
| 结论 | 有效 / 无效 / 劣化（无效与劣化同步写入已证伪清单） |
