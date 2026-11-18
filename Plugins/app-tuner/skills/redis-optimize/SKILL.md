---
name: redis-optimize
description: Redis 场景专属性能优化技能（鲲鹏/ARM 与 x86 Linux 通用）。当识别到应用使用 Redis（缓存、KV 存储、会话/排行榜/队列后端、Redis Cluster 分片），或用户提出 Redis 性能优化、QPS/吞吐提升、P99 时延下降、多实例互相干扰、绑核、网卡多队列/中断亲和、ntuple 按端口分流等需求时，使用本技能。提供业务实例发现 → 网卡 NUMA 定位 → 进程亲和性 → 网卡队列与中断绑核 → ntuple 端口分流 → redis.conf/OS 参数 → redis-benchmark 基线验证的完整方法，命令自包含可直接套用。
---

# Redis 专属性能优化技能（鲲鹏/ARM 与 x86 Linux 通用）

> 经验口径：NUMA/IRQ/ntuple 布局与“实例-队列-中断”一一映射来自鲲鹏平台 Redis 多实例实测经验，非官方规格；不同网卡/驱动/BIOS 表现有差异，跨环境使用前须按 §4 复测确认。

目标：识别 Redis 实例后，按“发现 → 基线 → 绑核 → 中断 → 分流 → 配置”的分层顺序优化，并用 redis-benchmark 量化验证。
命令与脚本自包含，可直接在目标机执行；系统级变更前必须先做快照（playbook §1）。

约定占位符（使用时按实际环境替换）：
- `$IP` / `$PORT` / `$PID`：实例监听地址、端口、进程号
- `$ETH`：业务网卡名（`ip -o addr show` 按 IP 反查）
- `$DRV` / `$BDF`：网卡驱动名与 PCI 地址（`ethtool -i $ETH`、`readlink -f /sys/class/net/$ETH/device`）
- `$NODE`：网卡所在 NUMA 节点；`$NODE_CORES`：该节点 CPU 列表
- `$CORE_i`：实例 i 绑定核；`$IRQ_i`：队列 i 的中断号
- `$N`：实例数（= 队列数 = ntuple action 数）
- `$REDIS_CONF`：redis 配置文件路径；`$CLIENT_IP`：压测客户端地址
- `$SKILL_DIR`：本 skill 根目录（playbook 中的脚本路径以它为基准）

## 1. 触发条件与场景识别

命中以下任一信号即启用本技能：
- 进程/部署中出现 redis-server、redis.conf、redis-cli、Redis Cluster
- 业务为：缓存、KV 存储、会话、排行榜、消息/任务队列后端
- 需求为：Redis 压测调优、QPS/吞吐提升、P99 时延下降、多实例互相干扰
- 需求为：Redis 绑核、网卡中断亲和、多队列、ntuple 按端口分流
- 平台为鲲鹏（Kunpeng-920 / 920G / 920GPro，aarch64）或 x86 Linux（命令通用）

**先做场景判定**（决定后续优化层是否适用）：

| 要素 | 取值 | 判定方法 |
|------|------|----------|
| 实例形态 | 单实例 / 同机多实例（多端口）/ Cluster 分片 | `ps -ef \| grep [r]edis`、`ss -lntp` |
| 部署形态 | 物理机 / VM / 容器 | `systemd-detect-virt`、`cat /proc/1/cgroup` |
| 网卡类型 | 物理网卡（PCIe）/ virtio / veth / bond / vlan | `ethtool -i $ETH`、`readlink -f /sys/class/net/$ETH/device` |
| 流量路径 | 客户端跨机（走物理网卡）/ 本机 loopback | `redis-cli CONFIG GET bind`、`ip route get $CLIENT_IP` |
| 瓶颈形态 | 单核打满 / softirq 高 / 队列不均 / 时延抖动 | `mpstat -P ALL`、`/proc/interrupts`、`redis-cli --latency-history` |

**适用性门禁**：
- 只有“跨机流量 + 物理网卡 + 驱动支持多队列/ntuple”时，§3.4/§3.5 才有意义。
- 虚拟网卡（virtio/veth）通常不支持 `ethtool -L` 调队列或 `--config-ntuple`，此时跳过网络层，转 RPS/RFS 或只做绑核（playbook §11）。
- Redis 只监听 127.0.0.1，或压测在服务端本机跑 loopback 时，物理网卡分流不生效，先纠正测量/部署方式。

## 2. 优化总工作流

```
实例发现(§3.1) → 网卡与 NUMA 定位(§3.2) → 基线压测(§4)
  → L1 进程绑核（§3.3，同 NUMA、实例间不重叠）              → 复测
  → L2 队列数 + 中断绑核（§3.4，实例-队列-中断一一对应）     → 复测
  → L3 ntuple 按端口分流（§3.5，action 0..N-1）               → 复测
  → L4 redis.conf / OS 参数（playbook §8）                    → 复测
  → 仍不达标：mpstat / softirq / perf 热点分析（playbook §12）
```

**原则**：逐层实施、单变量复测，只保留有增益的变更；禁止一次改完全部参数后无法归因。每层变更前先快照，失败立即按 playbook §10 回滚。

## 3. 核心操作（完整命令与脚本见 references/redis-tuning-playbook.md）

### 3.1 业务实例发现

```bash
ps -ef | grep [r]edis                 # 进程号、启动用户、配置文件
ss -lntp | grep redis-server          # 监听 IP:PORT 与 PID 的对应关系
taskset -pc $PID                      # 每个实例当前允许的核
redis-cli -h $IP -p $PORT CONFIG GET bind   # bind 地址（0.0.0.0 表示全地址）
```

产出实例表（后续每一步都按此表执行）：

| 实例 | PID | 监听 IP:PORT | 配置文件 | 当前亲和性 | 已绑核? |
|------|-----|--------------|----------|------------|---------|

注意：`ps -ef` 往往看不到监听地址，IP:PORT 以 `ss` 与 `CONFIG GET` 为准；容器内 `ss -p` 需 root。

### 3.2 网卡与 NUMA 定位

```bash
ip -o addr show | grep " $IP/"                 # 由业务 IP 反查网卡
ETH=eth2
BDF=$(basename "$(readlink -f /sys/class/net/$ETH/device)")
cat /sys/bus/pci/devices/$BDF/numa_node        # 权威：网卡所属 NUMA
lspci -vvv -s $BDF | grep -i numa              # 交叉确认（部分 lspci 版本无此行）
lscpu -p=CPU,NODE | grep -v '^#'               # 节点 → CPU 列表
```

判定：
- `numa_node >= 0`：`$NODE` 取该值，绑核只能从该节点 `cpulist` 中选。
- `numa_node = -1`：单路/虚拟化/未归属，**按全机核池规划，不得固定回落 node 0**；仅保证实例间核不重叠，并在报告中注明“网卡 NUMA 未知”。
- bond/vlan/bridge：`/sys/class/net/$ETH/device` 不存在，需下钻到真实物理口（playbook §3）。

### 3.3 进程亲和性（L1）

规则：
1. 每个 Redis 实例（含 io-threads）必须在**网卡所在 NUMA 节点内**（NUMA 未知时按全机核池，仅保证实例不重叠）。
2. 多实例**各占不同核**，禁止重叠；主线程 1 核，`io-threads` 另加同节点核。
3. 本技能采用“**实例核 = 其队列中断核**”布局：流量按端口分流到队列，队列中断落在实例所在核，避免跨核唤醒。若实例已 CPU 打满，可把中断移到同节点相邻核（playbook §6）。

```bash
taskset -pc $PID                    # 查看
taskset -cp $CORE_i $PID            # 重绑（$CORE_i 取自 $NODE_CORES）
taskset -pc $PID                    # 验证
```

### 3.4 网卡队列数与中断绑核（L2）

```bash
ethtool -l $ETH                          # 查看当前/最大 combined 队列
ethtool -L $ETH combined $N              # 队列数 = 实例数 N
grep -E "$ETH|$DRV|$BDF" /proc/interrupts   # 队列中断列表（改队列后必须重读）
echo $CORE_i > /proc/irq/$IRQ_i/smp_affinity_list   # 逐队列绑到不同核
```

要点：
- 先 `ethtool -L` 再读 `/proc/interrupts`：改队列数会重建中断，旧 IRQ 号失效；改队列后必须重读 /proc/interrupts，不得沿用旧 IRQ 号。
- 必须**逐队列写不同核**；所有队列写同一掩码会让中断集中到单核，效果相反。
- 每个队列 IRQ 绑定后校验 `/proc/irq/$IRQ_i/effective_affinity_list`，并用中断增量确认。
- 手工绑核前停 `irqbalance`（记录原状态，结束恢复），否则会被改回。
- 同机若还有其它业务流量，建议 `combined = N + 1`，把额外队列绑到预留核，避免非 Redis 流量落到实例核。
- **风险**：部分驱动改队列数需要网卡 down/up，会瞬断业务；若 SSH 走同一网卡，必须走 BMC/串口或维护窗口，先做快照。

### 3.5 ntuple 按端口分流（L3）

```bash
ethtool -K $ETH ntuple on                                       # 特性名以 ethtool -k 显示为准
ethtool --config-ntuple $ETH flow-type tcp4 dst-port $PORT action $i
ethtool -n $ETH                                                 # 查看规则与 action
```

映射规则：
- `action` 从 0 开始依次递增（即目标队列号），且必须小于当前 combined 队列数。
- 多实例必须**逐条执行**，`dst-port` 填各自 Redis 端口，`action` 与实例一一对应。
- 两个实例同端口不同 IP 时，`dst-port` 无法区分，需加 `dst-ip`（playbook §7）。
- 规则删除用 `ethtool --config-ntuple $ETH delete <location>`，location 取自 `ethtool -n`。

典型布局（4 实例，网卡位于 NUMA node 1，node1 核 32-63）：

| 实例 | 端口 | 绑核 | 队列 / action | 中断 |
|------|------|------|---------------|------|
| redis-0 | 6379 | 32 | 0 | IRQ_0 |
| redis-1 | 6380 | 33 | 1 | IRQ_1 |
| redis-2 | 6381 | 34 | 2 | IRQ_2 |
| redis-3 | 6382 | 35 | 3 | IRQ_3 |

## 4. 基线建立与性能验证

```bash
# 吞吐（pipeline 16；按实例端口分别压测）
redis-benchmark -h $IP -p $PORT -n 2000000 -c 100 -P 16 -t set,get -q
# 时延（无 pipeline）
redis-benchmark -h $IP -p $PORT -n 200000 -c 50 -t set,get -q
# 持续时延观测
redis-cli -h $IP -p $PORT --latency-history -i 1
# 服务端系统内在时延（在 redis 所在机执行）
redis-cli --intrinsic-latency 100
```

验证纪律：
- 客户端放**另一台机器**，并给压测进程绑核/多进程压测，确认客户端未先打满；同机 loopback 测不出网卡分流收益。
- `redis-benchmark` 的 SET 会真实写库：生产实例先确认，或只用 `-t get`、指定独立 db/实例；禁止在承载业务的 db 上灌数据。
- 每层优化后 **≥3 轮取均值**，波动 >5% 视为无效重测；输出 L0 → +L1 → +L2 → +L3 对比表，只保留有增益的变更。
- 同时记录：QPS、P50/P99、每核 CPU（`mpstat -P ALL`）、网卡队列中断增量、`/proc/softirqs` 的 NET_RX。

## 5. 常见问题速查

- **`ethtool -L` 报 Operation not supported**：virtio/veth/部分云网卡不支持调整队列；先 `ethtool -l` 确认，别反复试。
- **`--config-ntuple` 报不支持**：驱动无 ntuple（虚拟网卡常见）；该层跳过，改 RPS/RFS 或只做绑核。
- **ntuple 规则不生效**：`action` ≥ 队列数、规则被后写覆盖、或该网卡不支持该字段组合；用 `ethtool -n` 核对并查驱动文档。
- **改完 IRQ 绑核又漂了**：`irqbalance` 未停；或队列数变更后仍用旧 IRQ 号（必须重读 `/proc/interrupts`）。
- **两个实例同端口**：仅 `dst-port` 无法区分，需 `dst-ip` + `dst-port`；仍不支持则退化为 RSS 哈希，接受混流。
- **绑核后性能更差**：Redis 主线程与中断同核已饱和，把该实例中断移到同节点相邻核；或检查是否跨 NUMA 绑核。
- **压测数字不变**：客户端与服务端同机/loopback、流量未经过物理网卡；或客户端先瓶颈（`mpstat` 确认）。
- **时延周期性抖动**：THP 未关、RDB fork/BGSAVE、AOF fsync；纯缓存场景评估关闭持久化（需用户确认）。
- **`numa_node = -1` 但确为双路**：BIOS/IOMMU 未给出归属或设备为虚拟；按全核规划并记录，不要臆造节点。
- **回滚**：按 playbook §10 顺序恢复（恢复队列数 → 恢复 IRQ 掩码/irqbalance → 仅删本次新增 ntuple 规则并按快照重建既有规则 → 恢复 ntuple 开关/RPS/RFS → 恢复 taskset/配置）；禁止无条件删除网卡全部 ntuple 规则。

## 配套工具

- 采集现状证据：`kptools-proc-collector`（进程/绑核）、`kptools-network-collector`（队列/IRQ/RSS/NUMA/softirq，只读）。
- 更上层编排与瓶颈路由：`kpbot-app-tuner`。

## 参考资料

- `references/redis-tuning-playbook.md` — 快照/回滚、实例发现、NUMA 定位、绑核脚本、IRQ 绑定脚本、ntuple 单/多实例模板、redis.conf 与 OS 参数、验证与故障排查。
- `scripts/ntuple_safe_rollback.py` — ntuple 快照/安全回滚：只删除本次新增规则，并按快照重建既有规则。
