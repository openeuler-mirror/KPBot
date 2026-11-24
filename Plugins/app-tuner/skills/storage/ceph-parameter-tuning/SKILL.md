---
name: ceph-parameter-tuning
description: >
  Ceph 存储集群参数调优：OS 系统层（vm.swappiness/MTU/pid_max/file_max/read_ahead/IO 调度器/
  nr_requests）、Ceph 配置层（ceph.conf 全量参数：副本/PG 阈值/debug 开关/throttle/rocksdb/
  bluestore cache）、PG 分布与 balancer upmap、OSD NUMA 绑核；带备份/持久化/逐节点应用/重启窗口/
  回退的变更流程与 A/B 验证。当需要 Ceph 集群参数调优、性能瓶颈定位到 OSD/存储配置时使用。
  鲲鹏 920 / openEuler 22.03&24.03 实践。
license: MulanPSL-2.0
compatibility: Linux（aarch64 / 鲲鹏 920；openEuler 22.03&24.03）、Ceph 集群（ceph/ceph-daemon CLI，参数基线 Ceph 14.2/15.2 世代，v15.2+/v16+ 默认值差异见 kb version_note）、sysctl/blockdev/numactl
metadata:
  skill_type: tool_assisted
  category: tuning
  domain: distributed_storage
  version: "1.0.0"
---

# ceph-parameter-tuning — Ceph 集群参数调优闭环

## 简介

对已部署运行的 Ceph 集群（RBD 块存储 / RGW 对象存储 / CephFS 文件存储）做**参数级**性能调优：OS 系统层与 Ceph 配置层分层体检、分层变更，产出带证据链的 A/B 结论与可回滚的变更记录。闭环：

```
现状体检 → 参数比对 → 单变量变更 → 逐节点应用 → 健康与 A/B 验证 → 回退判定 → 台账记录
   ↑                                                                    │
   └────────────────────── 下一个变量（一次只动一个）────────────────────┘
```

体检全部只读先行（`ceph -s`、`ceph osd tree`、`ceph daemon osd.N config show`、`sysctl -n`、`blockdev --getra`、`cat /sys/block/*/queue/*`）；变更只能以候选动作输出，经确认后按"备份 → 逐节点 → 重启窗口 → 生效验证 → 回退预案"的契约落地。

## 实战背景

以下事实来自已检视定稿的调优知识（《鲲鹏 Ceph 官方指南》2025-06-30 系统调优与 Ceph 调优部分，参数基线对应 Ceph 14.2/15.2 世代，v15.2+/v16+ 默认值差异见 `references/tuning-kb.json` 各条目 version_note，以及社区一线实践），全量参数与命令见 `references/tuning-playbook.md` 与 `references/tuning-kb.json`：

- **参数分层**：OS 系统层（vm.swappiness=0、MTU=9000、kernel.pid_max=4194303、fs.file-max 取 MemTotal(KB)、read_ahead 8192KB 所有数据盘、IO 调度器 HDD=deadline/SSD/NVMe=none、nr_requests HDD=512/SSD/NVMe=1023）与 Ceph 配置层是两个参数面，分开体检、分开变更；sysctl 类持久化统一落 `/etc/sysctl.d/99-os-optimization.conf`，避免同键持久化文件互相覆盖。
- **debug 全关与计数器关闭收益**：全量 59 项 `debug_*` 建议关闭（0/0），减少日志打印开销；`throttler_perf_counter` 默认开启仅用于观察阈值是否达瓶颈，tracker 影响性能，性能调到最佳后建议关闭（False）。
- **bluestore cache 比例**：`bluestore_cache_meta_ratio` 0.4→0.8、`bluestore_cache_kv_ratio` 0.4→0.2（即 0.8/0.2 比例），cache 向 meta 倾斜。前置：`bluestore_cache_autotune` 自 v15.2 起默认 true，开启时按优先级动态分配、仅改比例不生效，调整比例前须先置 false（关闭后 cache 不再随 osd_memory_target 自动伸缩，需自行管理 bluestore_cache_size，TCMalloc 前提）
- **rocksdb compaction 参数**（`bluestore_rocksdb_options`）：`max_write_buffer_number` 4→64、`min_write_buffer_number_to_merge` 1→32、`recycle_log_file_num` 4→64、`max_background_compactions` 2→16、`compaction_threads` 8、`flusher_threads` 4、level0 触发阈值 64/128/256、`max_bytes_for_level_base` 6GB、`write_buffer_size`/`target_file_size_base` 4MB。
- **PG 公式与 balancer**：Total PGs = (OSD 总数 × 100) / 最大副本数，向上取整到最近的 2 的整数次幂，pgp_num 与 pg_num 相同；balancer 模式设为 `upmap` 并开启（默认 none/关闭），开启后每 60 秒自动调整少量 PG 分布，`ceph balancer eval` / `ceph pg dump` 观察到分布不再变化即收敛；每个 OSD 承载的 PG 数量应相同或非常接近，主 PG 也应尽量均匀分布。
- **OSD 绑核与网卡软中断异 NUMA**：OSD 进程应与网卡软中断分属不同 NUMA 节点——`osd_numa_node` 绑到网卡所属节点之外的空闲 NUMA 节点（ceph.conf [osd.n] 节段），避免网络压力大时 OSD 与软中断争抢同一 NUMA 节点的 CPU。
- **ceph.conf 生效面**：ceph.conf 只对当前节点生效，必须逐节点统一修改并 `systemctl restart ceph.target` 才对整个集群生效；cephadm/容器化部署用 `ceph config set` 等价映射。
- **已运行集群 full/nearfull 走 OSDMap 运行时阈值**：已运行集群 mon_osd_full_ratio/mon_osd_nearfull_ratio 走 OSDMap 运行时命令（ceph osd set-full-ratio/set-nearfull-ratio），ceph.conf 重启不改变现有阈值。

## 边界表

| 场景 | 归属 |
|---|---|
| Ceph 参数（ceph.conf 全量）、PG 分布与 balancer、OSD 绑核、OS 系统层参数证据与调优 | **本 skill** |
| Ceph 编译 | skill `ceph-build`（姊妹 PR #327，同批交付） |
| Ceph 安装、部署、集群初建 | skill `ceph-deployment-installation`（规划中） |
| 基准压测与验证方法学（rbd fio 口径、A/B 压测框架搭建） | skill `ceph-testing`（规划中） |
| 编排器双轨 | 已合入的编排器 subskill `ceph-optimization` 保留派发入口；其"OS 层证据移交 os-optimization"的边界仅适用于编排器流程——独立使用本 skill 时，OS 层参数属本 skill 调优范围（见 `references/tuning-playbook.md` 文首独立使用说明） |

## 铁律

1. **只读体检先行**：先跑只读体检（`ceph -s`、`ceph osd tree`、`ceph daemon osd.N config show`、`sysctl -n`、`blockdev --getra`、`cat /sys/block/*/queue/*` 等）形成 gap 清单，再谈变更；`sysctl -w`、`blockdev --setra`、写 sysfs、改 ceph.conf、重启 ceph.target 等变更动作只能以候选动作输出，经确认后落地。
2. **ceph.conf 备份 + 逐节点 + 重启窗口**：修改前必须备份（`cp /etc/ceph/ceph.conf{,.bak.<run_id>}`）；ceph.conf 只对当前节点生效，候选动作必须声明"逐节点统一应用到全部 Ceph 节点"并逐节点记录 forward/reverse；`systemctl restart ceph.target` 中断 Ceph 服务，必须确认重启窗口授权（`requires_restart_window=true`），未授权时该类动作挂起 `blocked` 等授权。
3. **单变量**：一次只动一个变量，改前记录原值、改后同口径复测；同期发生多件事时，任何"显著收益"都先怀疑是另一件事造成的；不得叠加第二个变量"补救"无效改动。
4. **持久化归属 99-os-optimization.conf**：sysctl 类运行时参数的持久化统一落 `/etc/sysctl.d/99-os-optimization.conf`，不新建自有同键持久化文件互相覆盖；sysfs/blockdev 类变更（read_ahead/IO 调度器/nr_requests）必须附持久化方案（udev rules/tuned/rc-local），否则该动作不得标记完成。
5. **numa_node sysfs 只读禁写**：`/sys/block/*/device/numa_node` 与 `/sys/class/net/*/device/numa_node` 由 PCIe 拓扑决定，属性只读，写入不可行也不会迁移设备；分布不均衡只记录证据，插槽/拓扑调整属硬件层面。
6. **禁止历史数据替代现场**：体检结论基于本轮现场配置读取；历史报告仅作参考，不得据此生成"已达标"结论。
7. **负收益标注**：参数收益与负载相关——MTU=9000 需全链路支持 jumbo frame 且要网络中断窗口，小报文为主或链路不支持时可能负收益；read_ahead 8192KB 大预读对随机读为主的负载可能负收益。候选动作必须附负载适用性标注与 A/B 验证要求，不得默认"建议值即收益"。
8. **证据与 run_id**：变更与回退记录绑定本轮 `run_id`（备份文件 `.bak.<run_id>`、逐节点 forward/reverse）；无 run_id 证据的结论不得标记完成。
9. **版本适配**：参数在当前 Ceph 版本不存在时标记 `version_mismatch` 并跳过，不得跨版本套用建议值。
10. **confidence 恒不标 measured**：本 skill 输出的收益预期不标 `measured`，收益由 A/B 验证回写。
11. **占位/命令替换类建议值必须现场求值**：suggested 含命令替换或占位符（如 fs.file-max 的 `<MemTotal_kB>`）时，必须现场执行 resolve_cmd 求值后代入比对与写入，禁止原样写入 ceph.conf 或 sysctl.d（systemd-sysctl 只做 `key = value` 字面解析、不做 shell 展开）；缺现场值时该项输出 blocked。

## 关键动作表

| 序 | 动作 | 参考 |
|---|---|---|
| ① | 全量只读体检与 gap 清单（集群状态/OS 层/ceph.conf/PG 与 balancer/OSD 绑核，逐项现状 vs 建议值） | `references/tuning-kb.json` |
| ② | OS 层参数调优（vm.swappiness/MTU/pid_max/file_max/read_ahead/IO 调度器/nr_requests，含 NUMA 设备亲和体检） | `references/tuning-playbook.md` §1 |
| ③ | ceph.conf 参数纠偏（global/mon/client/osd 全量：副本/PG 阈值/full-nearfull 阈值/debug 开关/throttle/rocksdb/bluestore cache） | `references/tuning-playbook.md` §2 |
| ④ | PG 公式修正与 balancer upmap | `references/tuning-playbook.md` §2.6 |
| ⑤ | OSD 绑核（`osd_numa_node` 与网卡软中断异 NUMA） | `references/tuning-playbook.md` §2.7 |
| ⑥ | 变更/回退 SOP（备份/逐节点/重启窗口/持久化/回退） | `references/tuning-playbook.md` §3 |
| ⑦ | 验证（配置生效/集群健康/PG 收敛/业务级 A/B） | `references/tuning-playbook.md` §4 |

## 输出契约

每轮调优产出四件套，缺一即未完成：

1. **gap 清单** `tuning_gaps[]`：仅保留核实过的 gap，每项含 `{item, section(系统调优/Ceph 调优), current, suggested, persisted, node_scope, evidence_refs}`；OS 层证据同格式记录（独立使用时据此出 OS 层候选动作）。
2. **候选动作** `candidate_actions[]`：每个动作含 `action_id`、`title`、`category`、`priority`、`change_mode`、`requires_root`、`risk`、`implementation_plan`、`validation_plan`、`rollback`、`expected_effect`、`expected_gain_metric`、`rejection_criteria`、`evidence_refs`；ceph.conf 类动作的 `implementation_plan` 必须含备份命令 + 逐节点应用 + `systemctl restart ceph.target` + 生效验证，重启类动作标注 `requires_restart_window=true`，副本数/PG 变更标注数据迁移风险。
3. **验证计划** `validation_plan`：配置生效逐参数回读（`ceph daemon osd.0 config show` / `sysctl -n` / `blockdev --getra`）、集群健康（`ceph -s` 回到 HEALTH_OK、`ceph osd tree` 拓扑正常）、PG 收敛（`ceph balancer eval` 分值改善并趋稳）、业务级 A/B（rbd fio 或业务压测 before/after）。
4. **回退** `rollback`：还原 ceph.conf 备份并重启；sysctl/sysfs/blockdev 恢复原值并清理持久化；回退后复跑 `ceph -s` 与基线确认。

结论三态：`recommended`（输出候选动作）/ `already_tuned`（列出已核对项，不产生动作）/ `blocked`（附 `conclusion_reason` 与最小补采建议）。`confidence` 取值 `high`（现状与建议值差异明确且来源为现场读取）/ `medium`（降级读取）/ `low` / `experience_only`（仅历史参考），恒不标 `measured`，收益由 A/B 验证回写。

## Dependencies

| 工具/资源 | 用途 | 缺失影响与降级 |
|---|---|---|
| `ceph` / `ceph daemon` CLI | 集群健康/配置/PG/balancer 体检 | 降级为读 `/etc/ceph/ceph.conf` + 系统命令体检，标注 degraded；两者都不可用时输出 blocked |
| `/etc/ceph/ceph.conf` | 配置现状读取 | 配置体检降级为经验比对，标 low/experience_only |
| `sysctl` / `lsblk` / `blockdev` / `numactl` | OS 层只读体检与变更 | 对应项标 unknown，不得凭经验补值 |
| `/etc/sysctl.d/`、tuned、udev rules、rc-local | OS 层参数持久化通道 | 持久化缺失时变更动作不得标记完成（重启后失效） |
| 基准压测框架（rbd fio 等，见 skill `ceph-testing`） | 业务级 A/B 验证 | 收益无法量化，仅输出配置生效验证；`expected_gain_metric` 标注待基线建立后验证 |
| `references/tuning-kb.json` | 参数清单与建议值（机器可读） | 体检降级为经验比对 |
