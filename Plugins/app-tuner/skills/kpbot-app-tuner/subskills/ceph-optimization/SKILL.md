---
name: ceph-optimization
description: 针对 Ceph 存储集群（RBD 块存储 / RGW 对象存储 / CephFS 文件存储）的系统级与集群级调优体检，输出调优候选动作或已达标结论；当场景为 Ceph 存储集群、瓶颈或热点证据落在 OSD/存储集群路径，或用户显式要求 Ceph 系统/集群级调优时使用。作为服务器应用优化 Agent 候选优化 skill 列表中的 Ceph 存储调优 skill。
---

# Ceph Optimization

使用本 skill 处理架构图中的"Ceph 存储集群"路径：对 Ceph 所在节点的 OS 系统层与 Ceph 集群配置层做只读体检，输出调优候选动作或已达标结论。

## 何时触发

- 场景为 Ceph 存储集群（RBD / RGW / CephFS），目标节点为 OSD/MON/MGR 或 Ceph 客户端
- 瓶颈或热点证据落在 OSD/MON/存储路径：OSD 进程 CPU 高、PG 分布不均告警（个别 OSD 成为瓶颈）、存储 IO 瓶颈、副本复制网络压力
- 用户显式要求对 Ceph 集群做系统/集群级调优

## Inputs

- 节点角色与集群信息：OSD/MON/MGR/MDS/客户端分布、集群名、部署方式（rpm/cephadm/容器）
- 本轮 `current_run_id` 的基线指标与瓶颈证据（由主流程提供）
- Ceph 版本（`ceph -v`）与配置通道（`/etc/ceph/ceph.conf` 或 `ceph config set`）
- `agent_action_mode` 与重启窗口授权（`systemctl restart ceph.target` 属业务中断动作）

## 必读 Reference

- 全量调优项、只读体检命令、变更/持久化/回退 SOP：`references/ceph-tuning-playbook.md`（§1 系统调优体检证据、§2 Ceph 专属调优与候选动作范围、§3 变更与回退 SOP、§4 验证）
- 机器可读参数清单（默认值/建议值/持久化/影响面，含归属边界 ownership）：`references/ceph-tuning-kb.json`

## 安全与架构红线

1. **体检只读**：`ceph -s`、`ceph osd tree`、`ceph balancer status`、`ceph daemon osd.N config show`、`sysctl -n`、`blockdev --getra`、`cat /sys/block/*/queue/*`、`cat /sys/class/net/*/device/numa_node` 等只读命令。`sysctl -w`、`blockdev --setra`、写 sysfs、修改 ceph.conf、重启 ceph.target 等变更只能以候选动作输出，由主框架 `approved_execute` 后落地。
2. **重启窗口**：`systemctl restart ceph.target` 会中断 Ceph 服务，必须确认重启窗口授权；未授权时该类动作挂起为 `blocked` 等授权。
3. **备份与多节点契约**：ceph.conf 修改前必须备份（`cp /etc/ceph/ceph.conf{,.bak.<run_id>}`）；ceph.conf 只对当前节点生效，候选动作必须声明"逐节点统一应用到全部 Ceph 节点"，并逐节点记录 forward/reverse。
4. **持久化**：sysctl/sysfs/blockdev 类运行时参数重启后失效，候选动作必须附带持久化方案（`/etc/sysctl.d/`、tuned profile、udev rules、rc-local），否则该动作不得标记完成。
5. **禁止历史数据替代现场证据**：体检结论基于本轮现场配置读取；历史报告仅作参考。

## Workflow

**硬门控**：Step 0 集群确认完成前不得生成 gap 清单与候选动作。

```
Step 0: 集群环境确认（只读）
         ↓ 产出: ceph_cluster_status{health, version, deploy_mode, config_channel, osd_count}
Step 1: 系统调优体检（指南 9.3.3：OS 配置 + NUMA 亲和）
         ↓ 产出: os_tuning_findings[]（OS 层证据，移交 os-optimization）
Step 2: Ceph 配置体检（指南 9.3.4：ceph.conf 参数）
         ↓ 产出: ceph_conf_gaps[]
Step 3: PG 分布体检（公式比对 + balancer 状态）
         ↓ 产出: pg_gaps[]
Step 4: OSD 绑核体检（OSD NUMA vs 网卡软中断 NUMA）
         ↓ 产出: numa_binding_gaps[]
Step 5: 结论与候选动作（recommended / already_tuned / blocked）
         ↓ 产出: tuning_gaps[]、candidate_actions[]、validation_plan、rollback
```

### Step 0：集群环境确认（只读）

- `ceph -s`（健康状态、quorum）、`ceph osd tree`（OSD 拓扑与总数）、`ceph -v`。
- 识别部署方式与配置通道：传统 rpm 部署用 `/etc/ceph/ceph.conf`；cephadm/容器化部署优先用 `ceph config set` 等价映射（在结论中标注 `config_channel`）。
- `ceph` 命令不可用或无权限时：降级为读 `/etc/ceph/ceph.conf` + 系统命令体检，标注 `degraded`；两者都不可用时输出 `blocked`。

### Step 1：系统调优体检（指南 9.3.3）

按 `references/ceph-tuning-kb.json` 的 `os_tuning` 逐项读取现状并与建议值比对，形成 gap 清单（含现状值/建议值/是否持久化/影响面）。检查项：`vm.swappiness=0`、`MTU=9000`、`kernel.pid_max=4194303`、`fs.file-max=MemTotal(KB)`、`read_ahead 8192KB`（所有数据盘）、IO 调度器（HDD=deadline，SSD/NVMe=none）、`nr_requests`（HDD=512，SSD/NVMe=1023）。命令见 playbook §1。
NUMA 亲和：检查 NVMe SSD 与网卡的 `numa_node` 是否在各 NUMA 节点间均衡分布（只读 `cat /sys/block/nvme*/device/numa_node` 与 `cat /sys/class/net/*/device/numa_node`）；分布不均衡写入 `os_tuning_findings[]`（不生成动作），绑核与 IRQ 亲和动作交 `cpu-affinity-optimization` / `network-optimization` 承接。

**归属边界（OS 层证据移交）**：OS 层参数（vm.swappiness/MTU/pid_max/file_max/read_ahead/IO 调度器/nr_requests/NUMA 设备亲和）只做只读体检并输出 `os_tuning_findings[]` 证据，对应候选动作由 `os-optimization` 统一输出；本 skill 不产出 OS 层动作，避免与 os-playbook 建议值冲突及 `/etc/sysctl.d/` 同键持久化文件互相覆盖。

### Step 2：Ceph 配置体检（指南 9.3.4 配置项）

- 只读比对 `/etc/ceph/ceph.conf` 或 `ceph daemon osd.0 config show | grep -E '<param>'` 与 kb `ceph_conf` 建议值：`osd_pool_default_min_size=1`、`cluster_network=<独立集群网段>`、`mon_max_pg_per_osd=3000`、`mon_max_pool_pg_num=300000`、全量 `debug_*`（应 0/0）、`throttler_perf_counter=False`、`ms_dispatch_throttle_bytes=2097152000`、`ms_bind_before_connect=True`、`rbd_cache=False`、`osd_max_write_size=256`、`osd_client_message_size_cap=1073741824`、`osd_map_cache_size=1024`、`bluestore_rocksdb_options`（完整建议值见 playbook §2）、`mon_osd_full_ratio=0.97`、`mon_osd_nearfull_ratio=0.95`、`osd_min_pg_log_entries=10`、`osd_max_pg_log_entries=10`、`bluestore_cache_meta_ratio=0.8`、`bluestore_cache_kv_ratio=0.2`。
- 参数在当前 Ceph 版本不存在时标记 `version_mismatch`，不输出该动作。

### Step 3：PG 分布体检

- 计算建议值：Total PGs = (OSD 总数 × 100) / 最大副本数，向上取整到最近的 2 的整数次幂；与各 pool 实际 `ceph osd pool get <pool> pg_num/pgp_num` 比对（pgp_num 应与 pg_num 相同）。
- `ceph balancer status`：mode 应为 `upmap` 且 balancer 为 on；未开启输出开启候选动作（说明每 60 秒自动调整少量 PG、可用 `ceph balancer eval`/`ceph pg dump` 观察收敛）。

### Step 4：OSD 绑核体检

- 网卡 NUMA：`cat /sys/class/net/<iface>/device/numa_node`；OSD 侧：`osd_numa_node` 配置值与 OSD 进程实际 NUMA 位置。
- 判定：OSD 进程应与网卡软中断在不同 NUMA 节点（`osd_numa_node` 设为网卡所属节点之外的空闲 NUMA 节点）；同节点时输出绑核候选动作。

### Step 5：结论生成与候选动作

- `tuning_gaps[]` 仅保留 Ceph 专属项（ceph.conf/PG 分布与 balancer/osd_numa_node）；OS 层 gap 不生成候选动作，写入 `os_tuning_findings[]` 移交 `os-optimization`；无 Ceph 专属 gap → `already_tuned` 并列出已核对项；证据不足 → `blocked` 附最小补采建议。
- 所有 `ceph.conf`/重启类动作在 `implementation_plan` 中必须含：备份命令、逐节点应用说明、`systemctl restart ceph.target`、生效验证（`ceph daemon osd.0 config show | grep <param>` 或 `ceph -s` 回到 HEALTH_OK）。

### Step 6：验证流程设计（不执行变更）

1. 配置生效验证：逐参数 `ceph daemon osd.0 config show` / `sysctl -n` / `blockdev --getra` 复查。
2. 集群健康验证：`ceph -s` 无新增 HEALTH_WARN；balancer `ceph balancer eval` 观察 PG 分布收敛。
3. 业务级 A/B：rbd fio（4K 随机读写、顺序吞吐）或业务压测在主流程轮次内 before/after 对比，收益回写 `measured`。

## 建议优先级

| 优先级 | 项 | 说明 |
|---|---|---|
| high | ceph.conf 参数纠偏（副本/min_size/cluster_network/debug 全关/PG 阈值/throttle/rocksdb 等） | 配置事实明确、收益直接；需重启窗口但无数据迁移风险 |
| high | PG 分布按公式修正 + balancer upmap | PG 不均时直接消除热点 OSD；balancer 自动均衡需观察期 |
| medium | OSD 绑核 `osd_numa_node` | 与网卡软中断分离，需先确认空闲 NUMA 节点（ceph.conf，属 Ceph 专属） |
| evidence | OS 层体检证据（vm.swappiness/MTU/pid_max/file_max/read_ahead/IO 调度器/nr_requests/NUMA 设备亲和） | 仅输出 `os_tuning_findings[]` 移交 `os-optimization` 出动作；建议值以其 os-playbook 为准 |

## 输出契约

- **`ceph_cluster_status`** — `{health, version, deploy_mode, config_channel, osd_count}`
- **`tuning_gaps[]`** — 仅 Ceph 专属项；`{item, section(9.3.3/9.3.4), current, suggested, persisted, node_scope, evidence_refs}`
- **`os_tuning_findings[]`** — OS 层证据（`{item, section(9.3.3), current, guide_suggested, persisted, node_scope, evidence_refs}`），移交 `os-optimization`，本 skill 不据此生成候选动作
- **`conclusion`** — `recommended` / `already_tuned` / `blocked`（附 `conclusion_reason`）
- **`candidate_actions[]`** — 仅 `recommended` 输出；`already_tuned`/`blocked` 不产生候选动作
- **`validation_plan` / `rollback`**

`confidence` 取值：`high`（配置现状与建议值差异明确且来源为现场读取）/ `medium`（降级读取）/ `low` / `experience_only`（仅历史参考）；本 skill 恒不标 `measured`，收益由主框架 A/B 回写。

## Candidate Action Contract

每个 `candidate_actions[]` 必须包含 `action_id`、`title`、`category`、`priority`、`change_mode`、`requires_root`、`risk`、`implementation_plan`、`validation_plan`、`rollback`、`expected_effect`、`expected_gain_metric`、`rejection_criteria` 和 `evidence_refs`。特有要求：

- ceph.conf 类动作：`implementation_plan` 含备份 + 逐节点应用 + `systemctl restart ceph.target` + 生效验证；`rollback` 为还原备份文件并再次重启；涉及副本数/PG 变更的动作必须标注数据迁移风险。
- OS 层参数动作（sysctl/sysfs/blockdev/MTU）不由本 skill 输出；由 `os-optimization` 承接，持久化统一 `/etc/sysctl.d/99-os-optimization.conf`，避免同键覆盖；本 skill 的 `candidate_actions[]` 仅限 Ceph 专属项。
- `systemctl restart ceph.target`：标注 `requires_restart_window=true`；未获授权时挂起 `blocked`。

## 错误处理

| 场景 | 处理方式 |
|------|---------|
| `ceph` 命令不存在/无权限 | 降级读 `/etc/ceph/ceph.conf` + 系统命令体检，`degraded`；ceph.conf 也缺失时 `blocked` |
| cephadm/容器化部署 | 配置通道改用 `ceph config set <section> <key> <value>` 等价映射，标注 `degraded` 并在报告中说明 |
| 非 Ceph 场景 | 输出 `not_applicable`（归入 conclusion_reason），交回其他 subskill |
| 参数在当前 Ceph 版本不存在 | 标记 `version_mismatch`，跳过该动作 |
| 重启窗口未授权 | 重启类动作挂起 `blocked` 等授权 |
| 基线/压测证据缺失 | 候选动作照常输出，但 `expected_gain_metric` 标注"待主流程建立基线后验证" |

## Dependencies

| 工具/资源 | 用途 | 缺失影响 |
|------|------|---------|
| `ceph`/`ceph daemon` CLI | 集群健康/配置/PG/balancer 体检 | 降级读 ceph.conf |
| `/etc/ceph/ceph.conf` | 配置现状读取 | 配置体检降级 |
| `sysctl`/`lsblk`/`blockdev`/`numactl` | OS 层体检 | 对应项标 `unknown` |
| 主流程基线与压测框架 | 业务级 A/B | 收益无法量化，仅配置生效验证 |
| `references/ceph-tuning-kb.json` | 参数清单与建议值 | 体检降级为经验比对 |

## Knowledge Mapping

- 指南 9.3.3 OS 层参数（表 9-14）与 NUMA 设备亲和 → 体检证据移交 `os-optimization`（建议值以其 os-playbook 为准；不得新增自有 `/etc/sysctl.d/` 持久化文件，统一由 `os-optimization` 落 `/etc/sysctl.d/99-os-optimization.conf` 以免同键覆盖）
- 指南 9.3.2 BIOS 项（Performance Policy/关闭 SMMU/关闭 CPU Prefetching/内存刷新 64ms）→ 交回 `bios-optimization` 承接，本 skill 不重复覆盖。
- 指南 9.3.1 硬件插卡 NUMA 均衡与硬件规格不足 → `hardware-upgrade-analysis`。
- `cluster_network` 带宽规划、网卡多队列/软中断绑核 → `network-optimization`、`cpu-affinity-optimization`。
- librbd/librados 库级与源码级热点 → `performance-library-selection` / `other-optimization`。
- 通用路由映射见 `../../references/knowledge-technique-routing.md`。
