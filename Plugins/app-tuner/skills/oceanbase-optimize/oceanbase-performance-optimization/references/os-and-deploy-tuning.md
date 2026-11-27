# OS 与部署优化

所有收益为实测基线（OB 4.2.5.5，鲲鹏 950/920B、AMD 9T34，sysbench 32U），换版本/机型
必须重测。先读 `oceanbase-hotspot-analysis` 确认瓶颈，再选下面的杠杆。

## 1. 跨 NUMA/跨 P：优先级最高

### 1.1 机制

- observer 启动拉起上千线程，OS 分布到全机 NUMA，内存就近分配。
- 启动后 `taskset` 只迁移线程 CPU，不迁移已分配内存 ⇒ 跨 P 访存。
- 950 的 `numa_distance` 默认值小于 30（调度域初始化阈值），首次选核更分散；920B 跨 P
距离大于 30，首次选核集中。
- 影响：insert 最敏感（实测 950 可差 10%~38%），读场景也会受影响。

### 1.2 方案对比

| 方案 | 做法 | 实测 | 适用 |
| --- | --- | --- | --- |
| cgroup 绑 NUMA | 为 observer/obproxy 建 cgroup，cpuset + memory node | 平均 +10%，insert +38% | 容器化生产、可重启 |
| 启动时 numactl | `numactl -C 0-31 -m 0 ./bin/observer` | 与 cgroup 接近 | 裸机、可重启 |
| 1NPS | BIOS/OS 设 1NPS，内存只在一个节点 | 950 读 +12.8%、写 +14.2% | 950；920B 无变化 |
| `node_reclaim_distance` | 调内核参数 | 比 migratepages +0.4%，insert 更稳 | 950；需内核支持/重启 |
| `migratepages` | `migratepages <pid> <from> <to>` | 950 平均 +4%，920B +1.1% | 不能重启时的临时手段 |

纪律：

- 禁止“先启动后 taskset”当作绑定；必须核验 `/proc/<pid>/numa_maps` 或 `numastat -p`。
- cgroup 配置后要确认 cpuset/memory 实际生效（线程落点、内存分布）。
- 1NPS/BIOS 变更需要窗口与回滚计划。

### 1.3 绑核策略（cgroup 实测）

| 机型 | 最优 | 次优 | 最差 |
| --- | --- | --- | --- |
| 950 | 绑核（observer 0-31，obproxy 32-95） | 绑 NUMA | 绑 1P |
| 920B | 绑 NUMA | 绑核 | 绑 1P |
| AMD 9T34 | 三种差异不大 | — | — |

统一原则：observer 与 obproxy 不同域；sysbench 尽量靠近 obproxy；网卡中断与对应进程同
NUMA。多实例用 VF + 独立中断。

## 2. grub / 内核启动参数

```
# /etc/default/grub 的 GRUB_CMDLINE_LINUX 追加
nohz=off cgroup_disable=files idle=poll kpti=off
```

| 参数 | 作用 | 风险 |
| --- | --- | --- |
| `nohz=off` | 关闭无滴答，减少上下文切换 | 功耗/调度行为变化 |
| `cgroup_disable=files` | 关闭 cgroup v1 files 控制器 | 影响容器/监控 |
| `idle=poll` | idle 轮询，降低唤醒延迟 | 空载功耗升高 |
| `kpti=off` | 关闭页表隔离 | 安全缓解失效，需评审 |

实测约 +1.8%。修改后 `grub2-mkconfig -o /boot/efi/EFI/*/grub.cfg` 并重启；保留原参数以便回滚。

## 3. 网络

| 手段 | 原理 | 实测 | 注意 |
| --- | --- | --- | --- |
| nf_conntrack 卸载 | 去掉连接跟踪开销 | 约 +6.8% | 非客户标准模型；影响 NAT/防火墙 |
| 网卡中断绑核 | 中断与 observer/obproxy 同 NUMA | 约 +1%；开 sockmap 后无影响 | 多实例需 VF |
| sockmap/hisock | eBPF socket 直连，绕过部分协议栈 | 场景相关；sql_nio 占比下降幅度需实测 | 与 SMC-D 重叠；需内核/权限 |
| SMC-D | 共享内存绕过 TCP | 约 +5% | 与 sockmap 冲突；需网络支持 |
| RPS 均衡 | 中断迁移 | 与 sockmap 重叠 | 需逐场景验证 |
| 多队列/VF | 每实例独立队列/中断 | 多实例线性损耗约 5% | 需网卡支持 |

顺序建议：先确认网络是否瓶颈（RPC/网卡利用率/off-CPU），再选一种（sockmap 或 SMC-D），
不要叠加未验证的手段。

## 4. 页表与内存

| 手段 | 原理 | 实测 | 风险 |
| --- | --- | --- | --- |
| 64K 页内核 | 降低 TLB miss、提升访存 | 约 +5% | 需重编内核；驱动/组件兼容 |
| THP never | 避免大页抖动 | 基线要求 | 重启失效需固化 |
| HugePages | 预分配大页 | OB 场景未验证出收益 | 内存碎片/配置复杂 |
| `swapoff` | 避免换页 | 基线要求 | — |
| `vm.zone_reclaim_mode=0` | 避免本地回收抖动 | 基线 | — |

64K 页属于“客户现场难落地”的杠杆：先确认客户是否接受重编内核与周边验证。

## 5. BIOS / 芯片

| 手段 | 实测 | 条件 |
| --- | --- | --- |
| uncore turbo（950） | +7%~8% | BIOS 版本支持；现场可能不可改 |
| 内存刷新率 1x（Custom Refresh Rate） | 常规数据库调优 | 机型/BIOS 支持 |
| Power Policy=Performance | 常规 | 功耗上升 |
| 关闭自适应预取 | 账务用例 +5% | 需逐用例验证 |
| 关闭 Tidcmp | 账务用例 +5% | 需逐用例验证 |
| `-march`/LSE | 见编译章节 | 需重编 |

> BIOS 变更属于高风险，必须与用户确认窗口与回滚（恢复默认）。

## 6. 磁盘

| 手段 | 实测 | 说明 |
| --- | --- | --- |
| data/clog 分盘 | 降低互相干扰 | 生产推荐 |
| V6 → V7 NVMe | 仅只写 +2.5% | 收益有限 |
| 内存盘 | 只写 +3%~6% | 仅用于判断 IO 是否瓶颈 |
| observer 与盘同 NUMA | 波动 0.8% | 不是主要因素 |
| XFS + noatime/pquota | 基线 | — |

结论：在本系列测试中，硬盘 IO 不是主要瓶颈；不要先换盘，先看 off-CPU 与 io 指标。

## 7. 部署形态与规格

| 手段 | 实测 | 说明 |
| --- | --- | --- |
| 32U→64U | 约 1.7 倍 | 需同步放大租户/网络线程 |
| 64U→128U | 基本无提升（测试方法问题） | 需按业务方口径重测 |
| 多实例 VF 隔离 | 线性损耗约 5% | 否则实例间差 10%+ |
| 单机三副本 | 共享 NUMA/IO | 仅架构验证 |

## 8. 变更顺序建议

```
1) 确认瓶颈（hotspot）
2) 绑核/绑内存（cgroup 或启动时 numactl）
3) 1NPS / numa distance（950，需窗口）
4) uncore turbo / grub / nf_conntrack（需窗口）
5) sockmap 或 SMC-D（二选一）
6) 64K 页 / 内核（最后，风险最高）
```

每步单独 A/B，记录回滚方式。
