# OS Optimization Playbook

本文件承接 `os-optimization/SKILL.md` 的细节。进入 OS 参数分析、HugePages 计算、THP 决策、tuned/A-Tune 评估、容器边界判断或 GRUB 参数评估时读取。

## Scope

- **平台**: ARM aarch64 鲲鹏 916/920/930/950 + openEuler 22.03(OLK-5.10)/24.03(OLK-6.6)
- **不纳入**: x86、非鲲鹏 ARM、其他 OS
- **职责**: Linux 内核态和 OS 服务层参数调优
- **不包含**: 网络协议栈参数(net.core.*/net.ipv4.*)→ network-optimization;网卡硬件→ network-optimization;线程绑核→ cpu-affinity-optimization;BIOS/固件→ bios-optimization;应用参数→ application-config-optimization;编译选项→ compiler-optimization

## Knowledge Base Anchors

### A+K 场景 OS 优化经验库

> A+K = Ascend + Kunpeng（昇腾 NPU + 鲲鹏 CPU 组合场景）。基于鲲鹏 920/930/950 + 昇腾 NPU 实际调优经验沉淀，适用于 AI 训练/推理等计算密集型工作负载。
>
> **⚠️ 本节经验值用于对照判断，不是可直接输出给用户的答案。**
> 必须先通过 OS 侧命令采集当前配置后，将当前值与本节推荐值对照，才能生成 candidate_actions。
> A+K 场景识别逻辑见 `os-optimization/SKILL.md` Step 2。

#### 大页内存相关优化

> 来源：昇腾官方文档（Ascend Extension for PyTorch 6.0.0 - OS 性能优化章节）
> AI 训练/推理场景内存需求量大，默认 4KB 页面导致 TLB Miss 和缺页中断增多。大页内存可显著减少 TLB Miss，提升性能。
> 以下优化手段可搭配使用。

##### 0. 采集状态与推荐决策

Agent 先采集以下环境状态:

```
cat /sys/kernel/mm/transparent_hugepage/enabled    # THP 状态
cat /proc/meminfo | grep Huge                       # 标准大页分配
ldd --version                                       # glibc 版本
rpm -qa glibc                                       # glibc 发行版本号
cat /etc/os-release                                 # OS 版本
ls /sys/devices/system/node/ | grep node            # NUMA 节点数
```

根据采集结果，对每个优化手段判断可用性:

| 优化手段 | 前置条件 | 需重启 | 使用难度 |
|---------|---------|--------|---------|
| 透明大页(THP)=always | 无 | 否 | 低 |
| tmpfs 使用大页 | 有挂载 tmpfs 权限 | 否 | 低 |
| 标准大页-临时生效 | 物理机（非虚拟机） | 否 | 低 |
| malloc 使用大页(透明大页) | glibc >= 2.34 | 否 | 低 |
| malloc 使用大页(标准大页) | glibc >= 2.34 + 已分配标准大页 | 否 | 中 |
| malloc 使用大页(libhugetlbfs) | glibc 2.28~2.33 + 安装 libhugetlbfs | 否 | 中 |
| glibc 动态库大页 | openEuler 22.03 SP1/SP3/SP4 + glibc >= 2.34-h157 + 已分配标准大页 | 否 | 中 |
| tcmalloc 大页优化 | tcmalloc 已加载 + 物理机或容器有挂载权限 + 已分配标准大页 | 是（重启服务） | 中 |
| 标准大页-永久生效 | 物理机（非虚拟机） | 是 | 高 |

推荐排序规则:
1. 优先推荐可用且不需重启、使用难度低的手段
2. 使用难度相同时，按上表从上到下排序
3. 不可用的手段列在最后，标注当前环境的限制原因
4. 向用户展示排序后的完整列表，让用户选择执行哪些

> Agent 不得自动执行任何优化手段，必须向用户展示可用清单并等待用户选择。

用户选择后的执行流程:

1. **生成 candidate_actions**: 根据用户选择的手段，生成对应的候选动作（含具体命令、rollback、验证方法）
2. **dry-run 展示**: 对每个候选动作先执行 dry-run，向用户展示具体操作内容（将执行的命令、影响的参数）
3. **用户确认**: 用户确认后执行
4. **执行**: 不需重启的手段，Agent 通过 `apply_optimization_action.sh --execute` 直接执行，执行后验证参数是否生效
5. **需重启的手段**（如标准大页永久生效）: 只生成操作手册（含完整命令和步骤），标注"需用户手动执行重启"，Agent 不自动重启物理机
6. **执行后验证**: 对已执行的手段，采集变更后的状态，与变更前对比确认生效

##### 1. 开启大页内存池

**透明大页（THP）**

- 推荐值: `always`（A+K 场景推荐开启，与通用数据库场景不同）
- 采集: `cat /sys/kernel/mm/transparent_hugepage/enabled`
- 生效: `echo always > /sys/kernel/mm/transparent_hugepage/enabled`（重启失效，需持久化）
- 注意: AI 训练场景与数据库场景不同，THP=always 有正向收益

**标准大页（HugePages）**

- 推荐值: 按需分配，建议 5000 个以上 2M 大页（约 10GB）
- 采集: `cat /proc/meminfo | grep Huge`
- 注意: ARM 机器需针对 NUMA 各节点都配置大页；若已绑核限定使用特定 node，可仅设置需要的 node
- 风险: 大页内存池过小可能导致进程 coredump（dmesg 检查 "killed due to inadequate hugepage pool"）；不适用于虚拟机

Agent 采集到当前标准大页配置后，向用户展示以下两种方式供选择:

**方式一: 临时生效（不需重启，推荐优先使用）**

- 优点: 立即生效，不需重启，可随时调整数量
- 缺点: 重启后失效，需配合持久化脚本
- 适用: 快速验证、临时使用

全局分配:
```
sysctl -w vm.nr_hugepages=5000
```

NUMA 节点分配（ARM 推荐，每个节点均匀分配）:
```
echo 2500 > /sys/devices/system/node/node0/hugepages/hugepages-2048kB/nr_hugepages
echo 2500 > /sys/devices/system/node/node1/hugepages/hugepages-2048kB/nr_hugepages
```

确认分配: `cat /proc/meminfo | grep Huge`

**方式二: 永久生效（需重启物理机，危险操作）**

- 优点: 重启后持久生效
- 缺点: 需要重启物理机，修改启动项属于危险操作
- 适用: 长期生产环境

步骤:
1. 确认当前内核启动项: `grubby --info=ALL | grep $(uname -r)`
2. 更新启动项参数: `grubby --update-kernel=<kernel> --args="default_hugepagesz=2M hugepagesz=2M hugepages=5000"`
3. 重启物理机: `reboot`
4. 重启后确认: `cat /proc/cmdline` 和 `cat /proc/meminfo | grep Huge`
5. 如需删除: `grubby --update-kernel=<kernel> --remove-args="hugepages hugepagesz default_hugepagesz"`（删除后同样需重启）

> ⚠️ 方式二需用户确认后执行，Agent 不得自动重启物理机。

##### 2. malloc 使用大页

> glibc 通过 Tunables 参数让 malloc 使用大页，需确认 glibc 版本。
>
> **⚠️ 版本要求以本经验库为准，不要引用互联网上 glibc 社区版本的差异。**
> 昇腾官方文档明确: openEuler 发行版 glibc >= 2.34 即支持 GLIBC_TUNABLES 大页特性。
> 不得将版本门槛提高到 2.35 或其他版本。

- 采集: `ldd --version` 或 `rpm -qa glibc`（openEuler 需看发行版本号如 2.34-h157）
- **glibc >= 2.34**（openEuler 发行版）:
  - 透明大页: `export GLIBC_TUNABLES=glibc.malloc.hugetlb=1`
  - 标准大页: `export GLIBC_TUNABLES=glibc.malloc.hugetlb=2`
- **glibc 2.28~2.33**（需额外安装 libhugetlbfs）:
  ```
  export HUGETLB_MORECORE=yes
  export LD_PRELOAD=/usr/lib64/libhugetlbfs.so
  ```
- 注意: 训练场景使用标准大页若报 "Bus error ... shared memory" 错误，可改用透明大页规避；容器环境需确保有权限申请大页

##### 3. tmpfs 使用大页

> tmpfs 使用透明大页后，程序和动态库会自动在代码段使用大页映射

- 使用:
  ```
  mkdir -p /mnt/temp
  mount -t tmpfs -o huge=always tmpfs /mnt/temp
  export TMPDIR=/mnt/temp
  ```
- 关闭: `umount /mnt/temp`
- 注意: 容器环境需有挂载 tmpfs 权限，或宿主机配置后挂载到容器

##### 4. glibc 动态库大页

> OpenEuler 提供的 glibc 方案，将动态库映射到大页，减少 iTLB cache miss

- 版本要求: openEuler-22.03-LTS-SP1/SP3/SP4，glibc >= 2.34-h157
- 采集: `rpm -qa glibc`
- 方法一（推荐）:
  ```
  export LD_HUGEPAGE_LIB=1
  ```
  在程序入口设置，外部 shell 脚本不生效；会让所有动态库尝试映射大页
- 方法二（细粒度控制）:
  ```
  hugepageedit libtorch_npu.so
  export HUGEPAGE_PROBE=1
  ```
  仅标记的动态库段使用大页，需安装 glibc-devel 包获取 hugepageedit 工具
- 前置依赖: 需先分配标准大页内存池（见第 1 项）

##### 5. tcmalloc 大页优化

> 将 tcmalloc 的内存分配映射到大页文件系统上，减少 TLB Miss，提升内存分配性能。
> 前提: 服务启动前已配置 tcmalloc 库（通过 LD_PRELOAD 或编译期链接），并设置了相关环境变量。

**tcmalloc 库检测**:

Agent 检测 tcmalloc 是否已加载：
```
echo $LD_PRELOAD | grep -i tcmalloc
ldd $(which python3) | grep tcmalloc
```
- 检测到 tcmalloc → 继续大页优化
- 未检测到 tcmalloc → 按以下模式处理：
  - **standalone 模式**: Agent 直接引导用户安装 tcmalloc（`dnf install gperftools`），用户安装并设置 `LD_PRELOAD` 后继续大页优化
  - **subagent 模式**: 在 candidate_actions 中输出该 action，标注前置依赖不满足:
    ```json
    {
      "action_id": "os-tcmalloc-hugepage",
      "title": "tcmalloc 大页优化",
      "category": "tcmalloc-hugepage",
      "precondition_skill": "performance-library-selection",
      "precondition_description": "需要先通过 performance-library-selection 完成 tcmalloc 库替换",
      "precondition_check": "echo $LD_PRELOAD | grep -i tcmalloc",
      "precondition_met": false,
      "status": "blocked_by_dependency"
    }
    ```
    > 该 action 的前置条件不满足时，应排在其他已满足条件的 action 之后。最终执行顺序由主 SKILL 决定。如果主 SKILL 在执行完其他优化后完成了库替换，可回到此 action 继续执行；如果主 SKILL 未完成库替换，则此 action 跳过，不影响其他优化手段的执行。

**优化步骤**:

1. 设置大页挂载点：
   ```
   mkdir -p /dev/hugepages/
   mount -t hugetlbfs hugetlbfs /dev/hugepages
   ```

2. 服务启动前指定 tcmalloc 使用大页内存：
   ```
   export TCMALLOC_MEMEFS_MAP_PRIVATE="true"
   export TCMALLOC_MEMFS_MALLOC_PATH="/dev/hugepages/tcmalloc"
   ```

3. 使用完卸载：
   ```
   umount /dev/hugepages
   ```

**环境适配**:
- 物理机: 直接执行上述步骤
- 容器场景: 需要在容器内设置大页挂载点（容器需有挂载权限，或宿主机配置后挂载到容器）
- 虚拟机: 不适用（大页挂载需要内核支持，虚拟机可能受 hypervisor 限制）

> 需重启服务使环境变量生效。前置依赖: 需先分配标准大页内存池（见第 1 项）。

##### 大页优化手段兼容性

| 优化手段 | 支持透明大页 | 支持标准大页 | 需重启 |
|---------|------------|------------|--------|
| malloc 使用大页 | 支持（glibc >= 2.34） | 支持 | 否 |
| tmpfs 使用大页 | 支持 | 不支持 | 否 |
| glibc 动态库大页 | 不支持 | 支持 | 否 |
| tcmalloc 大页优化 | 不适用 | 支持（使用 hugetlbfs） | 是（重启服务） |

---

### 通用 OS 优化经验库

| 技术 | 适用信号 | 案例/收益口径 | 验证指标 |
|---|---|---|---|
| CPU governor → performance | governor≠performance | 鲲鹏 920 无 Turbo,收益 <5%(防 C-state) | TPS、P95、C-state residency |
| THP → never/madvise | Database + THP=always | 实测 MySQL +0.2%;消除 defrag 延迟抖动 | TPS、P95 tail latency、defrag 次数 |
| HugePages on | PostgreSQL + buffer pool>16GB | 官方 ~10%;MySQL 不受益 | TLB miss rate、HugePages utilization |
| swappiness → 1 | 当前>10 且有 swap | 0-3%(官方推荐) | swap I/O、TPS |
| dirty_ratio → 5 | NVMe + 当前>10 | 0-2%(官方推荐) | write latency、TPS |
| irqbalance off + IRQ 绑核 | 手动 IRQ 亲和场景 | 实测 +5.5% | IRQ 分布、P95 |
| 64KB Page Size | TLB 密集型应用 | 3-8%(需实测) | TLB miss rate |
| kpti=off + mitigation=off | 安全非敏感场景 | 2-5%(官方推荐) | context switch rate、TPS |
| overcommit_memory=1 | Redis + fork 失败 | 不可量化(防止 fork 失败) | fork 成功率 |
| noatime mount | 读密集 + 当前未设 | 0-2%(官方推荐) | I/O wait、TPS |
| IO scheduler → deadline | HDD + 当前=cfq | 0-3%(官方推荐) | I/O wait、TPS |
| nr_requests → 2048 | HDD + 高 IO | 0-2%(官方推荐) | I/O wait、TPS |
| sched_steal_node_limit | 多 NUMA 节点(950 192核) | 需实测 | 调度延迟、跨节点 steal |

## Governor Decision

### 鲲鹏 governor 特殊性

| 型号 | 频率 | SMT | governor 收益 | 说明 |
|------|------|-----|-------------|------|
| 916 | 2.4GHz 固定 | 无 | <5%(防 C-state) | 无 Turbo |
| 920 | 2.6GHz 固定 | 无 | <5%(防 C-state) | 无 Turbo,governor 主要防止 C6 降频 |
| 930 | 2.2GHz | SMT2 | 待确认 | SMT2 下 governor 行为待实测 |
| 950 | 1.2-2.3GHz 动态 | SMT2 | 有实际意义 | 动态频率,governor 影响更大 |

### 决策规则

```
IF governor == performance → 跳过(已最优)
IF governor != performance → 推荐改为 performance
  风险: low(可即时回退)
  持久化: cpupower frequency-set --governor performance(自带持久化)
  OLK-5.10 EAS: findings 标注"EAS 调度器下 governor 策略建议验证"
```

### openEuler 默认值

服务器安装通常为 `performance`,需通过 `cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor` 验证。

## THP And HugePages And 64KB Page Size Decision

### 决策树

```
IF 64KB Page Size 已设(内核重编译):
  → 跳过 THP 和 HugePages(已通过 Page Size 优化,拮抗关系)
  → THP 无意义(已是 64KB),HugePages 收益减小

IF THP 当前值 != 推荐值:
  应用类型决定推荐值:
    Redis → never(消除延迟抖动,官方强制)
    MySQL → madvise(官方推荐;never 也可,但需配 HugePages)
    PostgreSQL → never(官方推荐;配 HugePages)
    MongoDB/MariaDB → 不涉及(官方未提及)
    通用/未知 → madvise(保守)
  IF THP=never 且 HugePages 未配置:
    → 鲲鹏 920 L2 TLB +11c,无大页会反噬(TLB miss 增加)
    → 自动追加 HugePages 检查(依赖图触发)
  IF THP=madvise/always 且 HugePages 未配置:
    → THP 提供动态大页,HugePages 非必须
    → 不自动追加,findings 提示"可选配置 HugePages 获得确定性大页"
```

### HugePages 应用特定规则

| 应用 | HugePages | 原因 | 收益预估 |
|------|-----------|------|---------|
| MySQL | ❌ 不推荐 | InnoDB 自管内存,官方文档和实测不受益 | — |
| PostgreSQL | ✅ 强烈推荐 | shared_buffers 适合大页 | ~10% |
| Redis | ❌ 不推荐 | Redis 自管内存(zmalloc) | — |
| MongoDB | — | 官方未提及 | 需实测 |
| MariaDB | ❌ 不推荐 | 与 MySQL 一致(InnoDB) | — |
| openGauss | ✅ 推荐 | 与 PostgreSQL 同源 | ~10% |

### HugePages 计算流程

1. **获取应用内存配置**:
   | 应用 | 读取来源 | 提取字段 |
   |------|---------|---------|
   | MySQL | mysql_variables.txt | innodb_buffer_pool_size |
   | PostgreSQL | pg_settings.txt | shared_buffers |
   | Redis | redis_config.txt | maxmemory |
   | MongoDB | mongo_config.txt | wiredTiger.cacheSizeGB |
   | openGauss | pg_settings.txt | shared_buffers |
   | 未知 | 系统内存 × 60% 估算 | — |

2. **计算数量**: `hugepages = ceil(app_memory / 2MB) × 1.1`(10% safety buffer)
   - PostgreSQL + shared_buffers > 64GB: 建议可选 1GB hugepage(`ceil(app_memory / 1GB) × 1.1`),需 GRUB 参数 + 重启

3. **NUMA 分配**:
   - 有 numactl -H: 按 NUMA node 均分 `per_node = ceil(total / num_active_nodes)`
   - 无 numactl -H: 全局分配 `sysctl -w vm.nr_hugepages=<total>`,降级标注

4. **验证当前状态**: 读取 `HugePages_Total`/`HugePages_Free`,已满足则跳过,部分配置则追加差额

5. **内存超限保护**:
   ```
   remaining = total_memory - (nr_hugepages × page_size) - 4GB(reserved)
   IF remaining < 0 → 拒绝执行
   IF remaining < 4GB → 警告,需用户确认
   ```

### 64KB Page Size

- 方法: 内核重编译(make menuconfig → Page size → 64KB),需重启
- 风险: 极高(可能无法启动、兼容性问题)
- 强制 `analysis_only`,不自动执行
- 与 THP/HugePages 互斥(拮抗关系)
- 前置检查: 确认有备用内核(GRUB 菜单)、远程管理通道、磁盘空间(~5GB)

## sysctl Tuning Playbook

### vm.swappiness

| 场景 | 推荐值 | 理由 |
|------|--------|------|
| Database(MySQL/PG/MongoDB/MariaDB/openGauss) | 1 | 减少 swap,官方推荐 |
| Redis | 1 或 0 | 内存数据库,减少 swap 影响 |
| Compute/Batch | 10 | 允许适度 swap |

```
IF current == 1(或无 swap) → 跳过
持久化: echo 'vm.swappiness=1' >> /etc/sysctl.d/99-os-optimization.conf
```

### vm.dirty_ratio / dirty_background_ratio

| 存储类型 | dirty_ratio | dirty_background_ratio | 理由 |
|---------|------------|----------------------|------|
| NVMe | 5 | 3 | NVMe 写延迟低,减少脏页积压 |
| HDD | 3 | 1 | HDD 写延迟高,更激进减少脏页 |
| SSD/未知 | 10 | 5 | 保守默认 |

存储类型从 disk-info.txt 的 `rotational` 字段推断: rotational=0→NVMe/SSD, rotational=1→HDD。

### vm.overcommit_memory

| 应用 | 推荐值 | 理由 |
|------|--------|------|
| Redis | 1 | **必须**: fork bgsave/AOF rewrite 需 overcommit |
| 其他 | 不涉及 | — |

## tuned Profile And A-Tune

### 检测优先级

```
1. 检测 atuned 服务运行中(systemctl status atuned)
   → 优先建议使用 A-Tune,不输出 tuned 候选
   → findings: "A-Tune 正在运行,建议通过 A-Tune 进行动态调优"
2. 检测 tuned 服务运行中(tuned-adm active)
   → 采集当前 profile,检查覆盖的参数
   → 不输出 tuned 已管理的参数的候选动作(避免冲突)
3. 两者均未运行
   → 正常输出单项 sysctl 候选动作
```

### tuned profile 参数覆盖表

| 参数 | throughput-performance | latency-performance | balanced |
|------|:-:|:-:|:-:|
| governor | performance | performance | 动态 |
| swappiness | 1 | 1 | 10 |
| dirty_ratio | 40 | 10 | 20 |
| dirty_background_ratio | 10 | 5 | 10 |
| THP | always | never/always | always |
| numa_balancing | 1 | 1 | 1 |
| IO scheduler | mq-deadline | mq-deadline | — |

> OLK-6.6 (EEVDF) 上 `sched_*_ns` 语义变化,tuned profile 中的调度参数可能不再最优。
> ARM aarch64 上 `energy_perf_bias` 不存在(被 tuned 跳过)。

### 执行顺序(若用 tuned)

```
1. 先设 tuned profile(批量基线)
2. 再手动微调 tuned 未覆盖或覆盖不合理的参数
3. 禁止: 先手动设 sysctl 再设 tuned(tuned 会覆盖手动值)
```

## I/O Scheduler

### 决策规则

| 存储类型 | 推荐 | 理由 |
|---------|------|------|
| NVMe | none | NVMe 自带调度,官方确认 |
| SSD | mq-deadline | 多队列 deadline |
| HDD | mq-deadline 或 kyber | 旋转介质需调度 |

```
IF NVMe + current == none → 跳过(已最优)
IF HDD + current != mq-deadline → 推荐改为 mq-deadline
  同时推荐 nr_requests=2048(MongoDB/MariaDB 独有强调)
持久化: udev rule
```

### nr_requests

| 场景 | 推荐值 | 来源 |
|------|--------|------|
| HDD + 数据库 | 2048 | 官方 0010 |
| NVMe | 不涉及 | NVMe 自带调度 |

## ulimit And systemd

### nofile 计算

```
nofile = max(current_value, max_connections × 2)
```

- MySQL: `max_connections` 从 mysql_variables.txt 读取
- PostgreSQL: `max_connections` 从 pg_settings.txt 读取
- Redis: 推荐至少 10032(默认 maxclients=10000 + 32 reserve)

### 持久化方式

- systemd unit: 修改 `LimitNOFILE`/`LimitNPROC` → `systemctl daemon-reload` → 重启服务
- limits.conf: 修改 `/etc/security/limits.conf`
- 容器内: Docker `--ulimit nofile=<value>` 或 `ulimit -n <value>`

### 目标进程 limits 采集

`cat /proc/<pid>/limits` 获取当前进程的实际 limits(shell `ulimit -a` 不可替代)。

## Kernel Version And Features

### OLK 内核特性差异

| 特性 | OLK-5.10 (22.03) | OLK-6.6 (24.03) | OS 调优影响 |
|------|------------------|------------------|-----------|
| 调度器 | CFS + EAS | EEVDF(替代 CFS) | tuned profile 的 sched 参数可能不准确 |
| THP | 标准 THP | mTHP(multi-size THP) | THP 行为变化 |
| sched_ext | 无 | 可选 | 可自定义调度策略 |
| io_uring | 增强 | 改进 | IO scheduler 重要性降低 |
| cgroup v2 sysctl | 标准命名空间化 | 同 | 无差异 |

### GRUB 调度参数

| 参数 | 推荐值 | 说明 | 风险 |
|------|--------|------|------|
| `sched_steal_node_limit=4` | 4(多 NUMA) | 限制跨节点 steal 调度范围 | 低 |
| `kpti=off` | 非生产环境 | 关闭内核页表隔离,减少上下文切换 | 中(安全降级) |
| `mitigation=off` | 非生产环境 | 关闭 Spectre 缓解 | 中(鲲鹏不受 Meltdown 影响,但受 Spectre) |

### stealtask 调度特性(两步操作)

```
步骤 1(运行时,online):
  echo STEAL > /sys/kernel/debug/sched_features
  → 立即生效,重启后失效

步骤 2(GRUB,system_reboot):
  修改 /etc/default/grub,追加 sched_steal_node_limit=4
  grub2-mkconfig -o /boot/efi/EFI/openEuler/grub.cfg
  → 需重启生效,持久化
```

## File System Mount

### noatime

| 场景 | 推荐 | 理由 |
|------|------|------|
| 数据库数据目录 | noatime | 减少 atime 更新 I/O,官方推荐 |
| 临时文件 | noatime | 同上 |
| 系统根分区 | 不建议 | 部分应用依赖 atime |

```
IF 当前已设 noatime → 跳过
IF 数据库数据目录未设 noatime → 推荐添加
  命令: mount -o remount,noatime <target>
  持久化: 修改 /etc/fstab 对应行追加 noatime
  ⚠️ 禁止 nobarrier(openEuler 不支持,数据丢失风险)
  ⚠️ 快速模式禁止,仅精确模式(需验证)
  ⚠️ 执行前检查: 文件系统健康(xfs_repair -n / e2fsck -n)、活跃进程(lsof/fuser)
  ⚠️ 执行前: sync 刷盘
  ⚠️ 执行后: 验证 noatime 生效 + 文件系统可读写
```

## NUMA Topology

### 型号差异

| 型号 | NUMA 节点 | distance | SMT | numa_balancing | HugePages 分配 |
|------|----------|----------|-----|---------------|---------------|
| 920 | 4(每 SCCL=1 node) | 同=10,跨socket=20-24 | 无 | 关闭收益高(跨SCCL无L3共享) | 按 SCCL |
| 930 | 4 | 待确认 | SMT2 | 需考虑 SMT 线程 | 按 NUMA node |
| 950 | 4 | 待确认 | SMT2 | 192核下自动迁移开销大 | 按 NUMA node |
| 916 | 简单 | — | 无 | — | — |

### Node Interleaving 感知

```
IF numactl -H 显示单节点(Node Interleaving=on):
  → NUMA 调优无效应
  → 跳过 numa_balancing/HugePages NUMA 分配
  → 建议: "BIOS Node Interleaving 已开启,NUMA 优化无效应,建议关闭(归 bios-optimization)"
```

### numa_balancing 决策(独立判断,不依赖 cpu-affinity 时序)

三个信号综合判断:
1. cpu-affinity 是否输出了绑核动作(输入信号之一)
2. 应用是否已做手动 NUMA 内存绑定(numactl --membind)
3. perf/numastat 证据是否显示跨 NUMA 访问严重

| 场景 | 建议 |
|------|------|
| 绑核 + 手动内存绑定 | 关闭(避免干扰手动绑定) |
| 绑核但未绑内存 | 看证据(远端→保持开启;本地→关闭) |
| 未绑核 | 不需要动(内核调度正常工作) |

**独立模式降级**(无 cpu-affinity 信号):
- irqbalance: 不输出候选(安全降级,无 IRQ 绑核时停 irqbalance 会导致中断集中)
- numa_balancing: 仅基于 numactl -H distance 判断

## Container Boundary

### 容器内可改参数(仅 2 项)

1. `ulimit -n`(RLIMIT_NOFILE) — 通过 `ulimit -n <value>` 或 Docker `--ulimit nofile=<value>`
2. `memory.swappiness`(per-cgroup) — 通过 Docker `--memory-swappiness=<value>` 或写 cgroup 文件

### 需宿主机修改的参数(13 项)

vm.swappiness(全局)、vm.dirty_ratio、vm.dirty_background_ratio、vm.overcommit_memory、vm.nr_hugepages、kernel.numa_balancing、THP enabled/defrag、governor、irqbalance、I/O scheduler、tuned、sched_features、GRUB 参数

> **特权容器**(`--privileged`)虽可写 `/sys/` 和 `/proc/sys/`,但改的是宿主机全局值,不安全。SKILL 对特权容器仍标注"需宿主机改"。

### 降级输出

容器环境下需宿主机修改的参数降级为 `analysis_only`,输出到 `container_boundary_notes`:
- `container_modifiable`: 容器内可改参数清单
- `host_required`: 需宿主机修改的参数清单(含 reason + suggested_value)
- `host_action_required`: true

## Coordination With Adjacent Skills

### cpu-affinity-optimization

| 参数 | 协同关系 | 时序 |
|------|---------|------|
| irqbalance | os 停 irqbalance → cpu-affinity 设 IRQ 亲和 → cpu-affinity 绑核 | os 先 |
| numa_balancing | os 独立判断(综合绑核信号+内存绑定+numastat) | 无时序依赖 |
| HugePages + NUMA 绑定 | os 分配 HugePages → 应用重启 → cpu-affinity NUMA 绑定 | os 先 |

### network-optimization

| 参数 | 归属 | os 的行为 |
|------|------|---------|
| net.core.somaxconn | network | 只采集不输出动作 |
| net.core.netdev_budget | network | 只采集不输出动作 |
| net.ipv4.* | network | 只采集不输出动作 |

### bios-optimization

| 参数 | 归属 | os 的行为 |
|------|------|---------|
| Node Interleaving | bios | 感知(读 numactl -H),建议关闭但不执行 |
| SMT | bios | 感知(读 smt/active),影响 IRQ 策略 |
| C-State | bios | governor=performance 时 C-state 行为变化,记录但不执行 |
| Power Profile | bios | 记录 BIOS 限制,governor 降级为 analysis_only |

## Evidence Requirements

### 必需证据清单

| 证据项 | 采集命令 | 必需性 | 缺失影响 |
|--------|---------|--------|---------|
| CPU 平台 | `lscpu` | 必需 | 无法识别鲲鹏型号,全量降级 |
| OS 版本 | `cat /etc/os-release` | 必需 | 无法识别 openEuler 版本,全量降级 |
| sysctl 全量 | `sysctl -a` | 必需 | 无法检查当前参数值,全量降级 |
| governor | `cat .../scaling_governor` | 必需 | 跳过 governor 参数 |
| THP 状态 | `cat .../transparent_hugepage/{enabled,defrag}` | 必需 | 跳过 THP 参数 |
| HugePages 状态 | `grep Huge /proc/meminfo` | 必需 | 跳过 HugePages 参数 |
| 环境类型 | cgroup/container/hypervisor 检测 | 必需 | 无法判断容器/虚拟机,全量降级 |
| NUMA distance | `numactl -H` | HugePages NUMA 分配必需 | HugePages 降级为全局分配 |
| 应用配置 | 按应用类型(MySQL/PG/Redis/MongoDB) | HugePages 计算必需 | HugePages 降级为系统内存×60%估算 |
| 目标进程 limits | `cat /proc/<pid>/limits` | ulimit 参数必需 | 跳过 ulimit 参数 |
| mount 选项 | `mount` | noatime 参数必需 | 跳过 noatime 参数 |
| I/O scheduler | `cat .../queue/scheduler` | I/O scheduler 参数必需 | 跳过 I/O scheduler 参数 |
| irqbalance 状态 | `systemctl status irqbalance` | irqbalance 参数必需 | 跳过 irqbalance 参数 |
| /proc/cmdline | `cat /proc/cmdline` | GRUB 参数必需 | 跳过 GRUB 参数 |

### 可选证据

| 证据项 | 采集命令 | 缺失影响 |
|--------|---------|---------|
| tuned 状态 | `tuned-adm active` | 不检查 tuned 覆盖关系 |
| A-Tune 状态 | `systemctl status atuned` | 不检测 A-Tune |

## Reject Conditions

候选出现以下任一情况应跳过或降级:

- 参数已为推荐值(前置判断跳过)
- 容器内且参数需宿主机改(降级 analysis_only)
- 虚拟机且参数受 hypervisor 限制(降级 analysis_only)
- 非 root 用户(所有 execute 降级 dry-run)
- OS 调优整体预估收益 <1%(跳过,建议转向其他 subskill)
- 瓶颈归因为网络(OS 不主责,只采集不输出动作)
- 鲲鹏 930/950 待确认参数(标注 low confidence,需实测验证)
