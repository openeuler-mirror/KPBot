# Redis 调优 Playbook（命令、脚本与回滚）

> 本文件是 `redis-optimize` SKILL.md 的执行细节层：场景判定与决策见 SKILL.md，此处提供各层完整命令、脚本模板与回滚步骤。
> 经验数据来源：鲲鹏平台 Redis 多实例绑核/中断/ntuple 实测经验；驱动与平台差异较大，跨环境使用前须复测。
> 约定：`$SKILL_DIR` 为本 skill 根目录（包含 SKILL.md、scripts/）；未特别说明时命令默认在 `$SKILL_DIR` 下执行。

## 1. 变更分级与快照

| 变更 | 风险 | 要求 |
|------|------|------|
| `ps` / `ss` / `taskset -pc` / `ethtool -l` / `ethtool -n` / `/proc/interrupts` | 只读 | 可随时执行 |
| `taskset -cp` 进程绑核 | 低（可回退，可能瞬时不均） | 记录原亲和性 |
| `sysctl` / THP / ulimit | 中（影响全机或该 shell） | 记录原值 |
| `redis.conf`（尤其持久化、maxmemory） | 中高（可能丢数据/重启） | 备份配置 + 用户确认 |
| `ethtool -L` 改队列 | 高（部分驱动需 down/up，瞬断业务） | 维护窗口 + 快照 + 用户确认 |
| `/proc/irq/*/smp_affinity` | 高（影响全机中断分布） | 停 irqbalance + 快照 + 用户确认 |
| `ethtool --config-ntuple` | 中（改分流，可删除恢复） | 结构化快照 + 只删本次新增 + 用户确认 |
| RPS/RFS（`rps_cpus` / `rps_flow_cnt`） | 中（影响软中断分布） | 快照原值 + 复测 |

**统一快照（变更前必做）**：

```bash
SNAP=/root/redis-optimize-$(date +%Y%m%d%H%M%S)
mkdir -p "$SNAP"
SKILL_DIR=${SKILL_DIR:-$(pwd)}    # 在 skill 根目录执行；也可显式指定
ETH=eth2
BDF=$(basename "$(readlink -f /sys/class/net/$ETH/device)")

ethtool -i "$ETH" > "$SNAP/ethtool-i.txt" 2>&1
ethtool -l "$ETH" > "$SNAP/ethtool-l.txt" 2>&1
ethtool -n "$ETH" > "$SNAP/ethtool-n.txt" 2>&1 || true
ethtool -k "$ETH" > "$SNAP/ethtool-k.txt" 2>&1
grep -E "$ETH|$BDF" /proc/interrupts > "$SNAP/interrupts.txt" 2>&1 || true
systemctl is-active irqbalance > "$SNAP/irqbalance.state" 2>&1 || true
sysctl net.core.somaxconn net.ipv4.tcp_max_syn_backlog vm.overcommit_memory > "$SNAP/sysctl.txt" 2>&1 || true
cat /sys/kernel/mm/transparent_hugepage/enabled > "$SNAP/thp.txt" 2>&1 || true

# ntuple：既有规则 location + 结构化快照 + 开关状态（回滚时只删本次新增）
ethtool -n "$ETH" | awk '/^Filter:/{print $2}' | sort -n > "$SNAP/ntuple-locations.txt" 2>/dev/null || true
grep -i 'ntuple' "$SNAP/ethtool-k.txt" | head -1 > "$SNAP/ntuple-state.txt" 2>/dev/null || true
python3 "$SKILL_DIR/scripts/ntuple_safe_rollback.py" snapshot --iface "$ETH" --out "$SNAP/ntuple.json" \
  || echo "WARN: ntuple 结构化快照失败，§10 只能按 $SNAP/ethtool-n.txt 人工恢复" >&2

# RPS/RFS 原状态（本次按 §11 兜底时可能被改写）
: > "$SNAP/rps-rfs.txt"
for q in /sys/class/net/$ETH/queues/rx-*; do
    [ -d "$q" ] || continue
    qn=$(basename "$q")
    printf '%s rps_cpus=%s\n' "$qn" "$(cat "$q/rps_cpus" 2>/dev/null)" >> "$SNAP/rps-rfs.txt"
    printf '%s rps_flow_cnt=%s\n' "$qn" "$(cat "$q/rps_flow_cnt" 2>/dev/null)" >> "$SNAP/rps-rfs.txt"
done
sysctl -n net.core.rps_sock_flow_entries > "$SNAP/rps_sock_flow_entries.txt" 2>/dev/null || true

# 每个 nic IRQ 的原掩码
for irq in $(awk -F: '{gsub(/ /,"",$1); print $1}' "$SNAP/interrupts.txt"); do
    cat /proc/irq/$irq/smp_affinity > "$SNAP/irq-$irq.mask" 2>/dev/null
done

# 每个 redis 实例的原亲和性
for pid in $(pgrep -f '[r]edis-server'); do
    taskset -pc "$pid" > "$SNAP/taskset-$pid.txt" 2>&1
done

# redis 配置：缺失时显式告警，禁止静默跳过
if [ -n "${REDIS_CONF:-}" ] && [ -f "$REDIS_CONF" ]; then
    cp -a "$REDIS_CONF" "$SNAP/redis.conf.orig"
    echo "redis.conf backup: $SNAP/redis.conf.orig"
else
    echo "WARN: REDIS_CONF 未设置或文件不存在（${REDIS_CONF:-<unset>}），未备份 redis.conf；§8 配置变更将无法自动回滚" >&2
fi

echo "snapshot: $SNAP"
```

---

## 2. 业务实例发现

```bash
# 进程（PID、用户、完整启动命令：端口/配置文件通常在这里）
ps -eo pid,user,args | grep '[r]edis-server'

# 监听地址（IP:PORT ⇄ PID）
ss -lntp | grep redis-server

# 每个实例当前亲和性
for pid in $(pgrep -f '[r]edis-server'); do
    echo "== PID $pid =="
    taskset -pc "$pid"
done

# bind 与端口（对每个 PORT 执行）
redis-cli -h "$IP" -p "$PORT" CONFIG GET bind
redis-cli -h "$IP" -p "$PORT" INFO server | grep -E 'redis_version|tcp_port|config_file|executable'
```

生成实例表（示例，后续命令按此表取 `$PID`/`$PORT`）：

| 实例 | PID | 监听 IP:PORT | 配置文件 | 当前亲和性 |
|------|-----|--------------|----------|------------|
| redis-6379 | 1234 | 192.0.2.11:6379 | /etc/redis/6379.conf | 0-63 |
| redis-6380 | 1235 | 192.0.2.11:6380 | /etc/redis/6380.conf | 0-63 |

注意：
- `ps -ef` 不保证显示监听地址；`ss` 需要 root 才能看到进程。
- 容器内只看到容器 namespace 的进程；如需按宿主机网卡调优，必须在宿主视角或使用 host 网络。

---

## 3. 网卡定位与 NUMA

```bash
# 1) 由业务 IP 找网卡
ip -o addr show | grep " 192.0.2.11/"
# 输出：2: eth2    inet 192.0.2.11/24 brd ... scope global eth2
ETH=eth2

# 2) 由客户端路由确认业务出口/入口网卡（更贴近真实流量）
ip route get 192.0.2.100 | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}'

# 3) 取 PCI 地址
BDF=$(basename "$(readlink -f /sys/class/net/$ETH/device)")
echo "$BDF"                       # 例：0000:3b:00.0
ethtool -i "$ETH" | sed -n 's/^bus-info: //p'

# 4) 网卡 NUMA（权威）
cat /sys/bus/pci/devices/$BDF/numa_node       # -1 = 未归属/单路/虚拟
lspci -vvv -s "$BDF" | grep -iE 'numa|node'   # 交叉确认

# 5) 节点 CPU 列表
numactl -H | grep -E '^node [0-9]+ cpus:'
lscpu -p=CPU,NODE | grep -v '^#'
```

虚拟设备下钻：

```bash
# bond：取物理成员
cat /sys/class/net/bond0/bonding/slaves
# vlan/bridge：看 lower 设备
ls -l /sys/class/net/$ETH/lower_* 2>/dev/null
ip -d link show "$ETH"
```

判定：
- `numa_node >= 0` → 后续绑核只能使用该节点 CPU。
- `numa_node = -1` → 按全核规划，实例间不重叠即可；在报告中注明“网卡 NUMA 未知”。
- 双路机器上 `numa_node = -1` 且确认是物理卡：检查 BIOS NUMA/IOMMU 设置，不要凭经验猜节点。

---

## 4. 基线采集与压测

```bash
IP=192.0.2.11
PORT=6379

# 实例状态
redis-cli -h $IP -p $PORT INFO server | grep -E 'redis_version|tcp_port|config_file'
redis-cli -h $IP -p $PORT INFO clients | grep -E 'connected_clients|blocked_clients'
redis-cli -h $IP -p $PORT INFO stats | grep -E 'instantaneous_ops_per_sec|rejected_connections|total_net_input_bytes|total_net_output_bytes'
redis-cli -h $IP -p $PORT INFO memory | grep -E 'used_memory_human|maxmemory_human|mem_fragmentation_ratio'
redis-cli -h $IP -p $PORT INFO persistence | grep -E 'rdb_last_bgsave_status|aof_enabled'
redis-cli -h $IP -p $PORT INFO cpu | grep -E 'used_cpu_sys|used_cpu_user'
```

压测（客户端在另一台机器执行）：

```bash
# 吞吐：pipeline 16
redis-benchmark -h $IP -p $PORT -n 2000000 -c 100 -P 16 -t set,get -q

# 时延：无 pipeline
redis-benchmark -h $IP -p $PORT -n 200000 -c 50 -t set,get -q

# 随机 key，避免反复覆盖同一个 key（-r 基数）
redis-benchmark -h $IP -p $PORT -n 2000000 -c 100 -P 16 -r 1000000 -t set,get -q

# 持续观测
redis-cli -h $IP -p $PORT --latency-history -i 1

# 系统内在时延（在 redis 所在机执行，测调度/中断抖动）
redis-cli --intrinsic-latency 100
```

**纪律与安全**：
- 客户端 `taskset` 绑定自己的核，用 `mpstat -P ALL 1` 确认客户端未打满。
- SET 会真实写库：生产实例只跑 GET 或使用独立测试实例/独立 db；禁止 FLUSHALL/SHUTDOWN。
- 每轮 ≥3 次取均值，波动 >5% 重测；记录 QPS、P50/P99、`mpstat`、IRQ 增量。

---

## 5. 进程绑核

```bash
NODE_RAW=$(cat /sys/bus/pci/devices/$BDF/numa_node 2>/dev/null || echo -1)   # 例：1

if [ "$NODE_RAW" -ge 0 ] 2>/dev/null; then
    NODE="$NODE_RAW"
    # 节点 CPU 列表（二选一）
    mapfile -t NODE_CORES < <(lscpu -p=CPU,NODE | grep -v '^#' | awk -F, -v n="$NODE" '$2==n {print $1}')
    # 或
    # mapfile -t NODE_CORES < <(numactl -H | awk -v n="$NODE" '$1=="node" && $2==n && $3=="cpus:" {for(i=4;i<=NF;i++) print $i}')
else
    NODE=""
    # 网卡 NUMA 未知：使用全机核池，禁止固定回落 node 0（否则双路机可能全量跨 NUMA）
    mapfile -t NODE_CORES < <(lscpu -p=CPU,NODE | grep -v '^#' | awk -F, '{print $1}')
    echo "WARN: 网卡 NUMA 未知（numa_node=$NODE_RAW），按全机核池规划，仅保证实例间不重叠；报告中需注明" >&2
fi

if [ ${#NODE_CORES[@]} -eq 0 ]; then
    echo "错误：无法获取可用核列表（lscpu 不可用？）" >&2
    exit 1
fi

echo "NODE=${NODE:-unknown} cores=${NODE_CORES[*]}"
```

按实例分配（示例：4 个实例，各占一个核）：

```bash
PIDS=(1234 1235 1236 1237)          # 顺序与 PORTS 一致
PORTS=(6379 6380 6381 6382)

if [ ${#PIDS[@]} -gt ${#NODE_CORES[@]} ]; then
    echo "错误：实例数超过可用核池大小（${#NODE_CORES[@]}），需减实例或改用共享核（需复测）"
    exit 1
fi

for i in "${!PIDS[@]}"; do
    core=${NODE_CORES[$i]}
    taskset -cp "$core" "${PIDS[$i]}"
    taskset -pc "${PIDS[$i]}"
done
```

验证：
- `taskset -pc` 输出应为单核，且属于网卡 NUMA 节点（已知时）或全机核池（未知时）。
- `ps -o pid,psr,pcpu,comm -p $PID` 观察实际运行核（采样值，仅辅助）。
- 多实例核集合无交集：把各实例输出汇总比对。

`io-threads`（Redis 6+）需要更多核时，从同一节点按实例连续分配，例如实例 0 用 `32-35`、实例 1 用 `36-39`；中断仍绑该实例第一个核。

---

## 6. 网卡队列与中断绑核

### 6.1 队列数

```bash
ethtool -l $ETH
# Pre-set maximums:  Combined: 64
# Current hardware settings: Combined: 1

N=${#PORTS[@]}
ethtool -L $ETH combined "$N"
```

注意：
- `combined` 不能超过 Pre-set maximums；部分驱动只能整体 down/up 后生效。
- 改队列数会重建中断并可能清空 ntuple 规则 → **顺序必须是：改队列 → 绑中断 → 配 ntuple**。
- 同机还有非 Redis 流量时，用 `combined = N + 1`，并把额外队列的 IRQ 也绑到预留核（该核不跑 Redis），避免杂流干扰实例核。
- 若 SSH 走同一网卡，先准备 BMC/串口；`ethtool -L` 可能瞬断。

### 6.2 识别队列中断

```bash
DRV=$(ethtool -i $ETH | sed -n 's/^driver: //p')
BDF=$(basename "$(readlink -f /sys/class/net/$ETH/device)")

# 常见命名一：<ifname>-TxRx-<queue>
grep -E "${ETH}-TxRx-[0-9]+" /proc/interrupts \
  | sed -E 's/^ *([0-9]+):.*TxRx-([0-9]+).*/\2 \1/' | sort -n

# 常见命名二：mlx5_comp<vec>@pci:<bdf>
grep -E "mlx5_comp[0-9]+@pci:${BDF}" /proc/interrupts \
  | sed -E 's/^ *([0-9]+):.*mlx5_comp([0-9]+)@.*/\2 \1/' | sort -n

# 兜底：名称不含队列号时，过滤管理/杂项中断后按 IRQ 号升序
grep -E "$ETH|$DRV|$BDF" /proc/interrupts \
  | grep -viE 'mgmt|mailbox|admin|misc|link|ptp|fec|async' \
  | awk -F: '{gsub(/ /,"",$1); print $1}' | sort -n
```

**必须 dry-run 确认**。很多驱动改 `-L` 后按队列顺序重建中断，IRQ 号升序即队列顺序；但 mlx5 的 completion vector、部分国产网卡命名不完全对应队列号。确认方法：
- 驱动文档/`ethtool -S $ETH | grep -iE 'rx[-_]?queue|packets'` 对照每队列计数；
- 先绑一个队列，压该端口，看该 IRQ 增量是否显著（见 §6.4）；
- 名称含队列号时，以名称中的 N 为准，不以 IRQ 号为准。

### 6.3 绑核

先停 irqbalance：

```bash
systemctl stop irqbalance      # 记录过原状态，结束按 §10 恢复
```

生成映射并写绑定（示例使用 IRQS 数组，元素顺序 = 队列顺序；下面以 `TxRx` 命名为例，mlx5 换成 §6.2 的 `comp` 提取，兜底用 IRQ 号升序）：

```bash
mapfile -t IRQS < <(grep -E "${ETH}-TxRx-[0-9]+" /proc/interrupts \
  | sed -E 's/^ *([0-9]+):.*TxRx-([0-9]+).*/\2 \1/' | sort -n | awk '{print $2}')

if [ ${#IRQS[@]} -lt "$N" ]; then
    echo "错误：只找到 ${#IRQS[@]} 个队列中断，少于实例数 $N，检查 naming/队列是否生效"
    exit 1
fi

# 只取前 N 个队列，实例 i → 队列 i → 核 i
for i in $(seq 0 $((N-1))); do
    irq=${IRQS[$i]}
    core=${NODE_CORES[$i]}
    echo "queue#$i irq=$irq -> core=$core"
    echo "$core" > /proc/irq/$irq/smp_affinity_list
    echo "  effective_affinity_list: $(cat /proc/irq/$irq/effective_affinity_list)"
done
```

兜底命名（无队列号）时 IRQS 取 IRQ 号升序，脚本同上，但必须先 dry-run 人工核对。

若某实例主线程已打满，而中断与它同核造成争抢：把该队列 IRQ 改绑同节点相邻核（该核不再分配实例），再复测；不要跨节点。

十六进制掩码等价写法（内核同时接受两种）：

```bash
mask=$(printf '%x' $((1 << core)))
echo "$mask" > /proc/irq/$irq/smp_affinity
```

**禁止**给所有队列写同一个核/同一个掩码——中断会集中，RSS 失效。

### 6.4 验证

```bash
# 绑定结果
for i in $(seq 0 $((N-1))); do
    irq=${IRQS[$i]}
    echo "irq=$irq affinity=$(cat /proc/irq/$irq/effective_affinity_list)"
done

# 中断增量落核验证：
grep -E "$ETH|$DRV|$BDF" /proc/interrupts > /tmp/irq.before
# …按端口压测 30s…
grep -E "$ETH|$DRV|$BDF" /proc/interrupts > /tmp/irq.after
awk 'NR==FNR{for(i=2;i<=NF;i++) b[$1","i]=$i; next}
     {mx=0; mc=-1; for(i=2;i<=NF;i++){d=$i-b[$1","i]; if(d+0>mx){mx=d+0; mc=i-2}}
      printf "irq=%s 最大增量 CPU%d (+%d)\n", $1, mc, mx}' /tmp/irq.before /tmp/irq.after
```

判据：每条队列 IRQ 的增量应集中在绑定核，其余核增量接近 0；活跃 IRQ 的 max/min 增量比 >3 视为不均衡（RSS 热点），>50 视为 RSS 塌缩。

---

## 7. ntuple 端口分流

### 7.1 前置检查

```bash
ethtool -k $ETH | grep -i ntuple        # 需为 on；查看实际特性名（ntuple / ntuple-filters）
ethtool -n $ETH                         # 查看现有规则，记录原始输出（快照已存）
ethtool -K $ETH ntuple on               # 若 -k 显示的是 ntuple-filters，则用该名称；[fixed] 表示不可改
```

若 `Cannot get ntuple filters: Operation not supported`，该网卡/驱动不支持，跳到 §11 的替代方案。

> §1 已用 `scripts/ntuple_safe_rollback.py` 快照既有规则；回滚时只删本次新增并按快照重建既有（§10 第 3 步）。

### 7.2 多实例逐条配置

```bash
PORTS=(6379 6380 6381 6382)

for i in "${!PORTS[@]}"; do
    port=${PORTS[$i]}
    ethtool --config-ntuple "$ETH" flow-type tcp4 dst-port "$port" action "$i"
done

ethtool -n "$ETH"
```

要点：
- `action` 从 0 递增，对应队列 0..N-1；必须 `< combined`。
- 与 §6 的“实例 i → 队列 i → 核 i”严格一致，否则流量会落到别人的核。
- 多实例逐条执行，不要用一条命令写多个端口。
- `flow-type tcp4` 只匹配 IPv4；IPv6 客户端需 `tcp6`（驱动支持为准），否则流量仍走 RSS 哈希。
- Redis Cluster 的节点间 bus 端口（`port+10000`）不在这条规则里，仍按 RSS 分散，属正常。

`ethtool -n` 输出示例：

```
Filter: 1
  Flow Type: TCP over IPv4
  Dest port: 6379 mask: 0xffff
  Action: 0
```

### 7.3 同端口不同 IP

```bash
# 两个实例都监听 6379，bind 不同 IP
ethtool --config-ntuple $ETH flow-type tcp4 dst-ip 192.0.2.11 dst-port 6379 action 0
ethtool --config-ntuple $ETH flow-type tcp4 dst-ip 192.0.2.12 dst-port 6379 action 1
```

部分驱动要求字段全掩码匹配或只支持 `dst-ip` 单独匹配；用 `ethtool -n` 确认字段已写入。若驱动无法组合，则退化为 RSS 哈希分流（接受混流），不要伪造成功。

### 7.4 删除与验证

```bash
# 列出并删除（location 为 Filter 编号）
ethtool -n $ETH
ethtool --config-ntuple $ETH delete <location>
```

验证分流：
```bash
grep -E "$ETH|$DRV|$BDF" /proc/interrupts > /tmp/irq.before
redis-benchmark -h $IP -p 6380 -n 500000 -c 50 -t get -q   # 只压目标端口
grep -E "$ETH|$DRV|$BDF" /proc/interrupts > /tmp/irq.after
# 观察队列 1 的 IRQ 增量是否集中在 core 1（§6.4 的 awk）
```

---

## 8. redis.conf 与 OS 参数（L4）

### 8.1 redis.conf（按场景取用）

```conf
# 网络
tcp-backlog 4096                 # 同步调大 net.core.somaxconn
timeout 0
tcp-keepalive 300

# 多线程 I/O（Redis 6+，仅网络读写并发，命令执行仍单线程）
io-threads 4                     # 4 核以上再启用；不超过实例可用核数
io-threads-do-reads yes

# 内存
maxmemory 16gb                   # 按业务容量设置，避免 OOM
maxmemory-policy allkeys-lru     # 按业务语义选择

# 持久化：纯缓存且允许丢数据时（必须用户确认）可关闭
# save ""
# appendonly no

# 淘汰/删除异步化，降低主线程阻塞
lazyfree-lazy-eviction yes
```

说明：
- `io-threads` 只加速收包/发包与协议解析，不加速单条命令执行；网络不是瓶颈时可能反而增加切换开销，必须 A/B。
- 单 key 热点/单命令慢查询属于应用层问题，绑核与网卡调优无法解决；应评估分片、pipeline、改数据结构。
- 修改 `maxmemory`/持久化/`io-threads` 通常需重启，先在从库/测试实例验证。

### 8.2 OS 参数

```bash
# 内存过量提交（Redis fork 依赖）
sysctl -w vm.overcommit_memory=1

# 连接队列（配合 redis tcp-backlog）
sysctl -w net.core.somaxconn=4096
sysctl -w net.ipv4.tcp_max_syn_backlog=4096

# 文件句柄（在启动 redis 的同一 shell/systemd 单元设置）
ulimit -n 65535

# 关闭透明大页（THP 会造成 fork/时延抖动）
echo never > /sys/kernel/mm/transparent_hugepage/enabled

# 持久化
cat >/etc/sysctl.d/99-redis.conf <<'EOF'
vm.overcommit_memory = 1
net.core.somaxconn = 4096
net.ipv4.tcp_max_syn_backlog = 4096
EOF
sysctl --system
```

THP 的持久化方式按发行版选择（tuned profile、grub `transparent_hugepage=never` 或 systemd `ExecStartPre`）；重启后必须复查：
```bash
cat /sys/kernel/mm/transparent_hugepage/enabled   # 应为 [never]
```

可选：CPU governor 固定 performance（需确认整机功耗策略）：
```bash
cpupower frequency-set -g performance
```

---

## 9. 验证与判据

| 指标 | 采集 | 判据/解读 |
|------|------|-----------|
| 实例绑核 | `taskset -pc $PID` | 单核且属于网卡 NUMA 节点；实例间无交集 |
| 中断绑核 | `/proc/irq/$IRQ/effective_affinity_list` | 每队列 IRQ 落在对应实例核 |
| 中断增量 | `/proc/interrupts` 前后差 | 目标队列 IRQ 增量在其绑定核；max/min >3 不均衡 |
| 实例 CPU | `mpstat -P ALL 1 10` | 实例核 `%usr`≈100 且其他核空闲 → CPU 瓶颈，绑核/分流收益有限 |
| softirq | `/proc/softirqs` 的 NET_RX 前后差 | IRQ 核 `%soft` 高而实例核空闲 → 分流/队列不对齐 |
| 丢包 | `ethtool -S $ETH \| grep -iE 'drop\|miss\|error'` | delta >0 零容忍，查 ring/队列/流控 |
| 连接拒绝 | `INFO stats` 的 `rejected_connections` | >0 → backlog/somaxconn/文件句柄 |
| 时延 | `redis-cli --latency-history` | 周期性尖峰对齐 BGSAVE/AOF fsync/THP |
| 调度时延 | `redis-cli --intrinsic-latency 100` | 最大值远高于均值 → 中断/调度干扰 |

对比表模板：

| 阶段 | QPS | P50 | P99 | 实例核 %usr | 备注 |
|------|-----|-----|-----|-------------|------|
| L0 基线 | | | | | |
| +L1 绑核 | | | | | |
| +L2 队列/IRQ | | | | | |
| +L3 ntuple | | | | | |
| +L4 配置 | | | | | |

---

## 10. 回滚

按以下顺序执行，避免中间态叠加。**ntuple 禁止无条件删除整张网卡的全部 filter**：只删本次新增，既有规则按快照重建。

```bash
SNAP=/root/redis-optimize-xxxxxxxx        # 快照目录
SKILL_DIR=${SKILL_DIR:-$(pwd)}            # 在 skill 根目录执行
ETH=eth2

# 1) 恢复 combined 队列数（取快照中的 Current hardware settings；可能清空 filter，下一步会按快照重建）
ORIG_COMBINED=$(awk '/Current hardware settings/{f=1} f&&/Combined:/{print $2; exit}' "$SNAP/ethtool-l.txt")
if [ -n "$ORIG_COMBINED" ]; then
    ethtool -L "$ETH" combined "$ORIG_COMBINED"
fi

# 2) 恢复 IRQ 掩码（队列数恢复后 IRQ 编号可能已变，逐项核对）
for f in "$SNAP"/irq-*.mask; do
    [ -e "$f" ] || continue
    irq=$(basename "$f" .mask | sed 's/^irq-//')
    [ -f /proc/irq/$irq/smp_affinity ] && cat "$f" > /proc/irq/$irq/smp_affinity
done
# 若编号已变，以 irqbalance 默认分布兜底
if [ "$(cat "$SNAP/irqbalance.state" 2>/dev/null)" = "active" ]; then
    systemctl start irqbalance
fi

# 3) ntuple：只删本次新增；既有规则按 ntuple.json 快照重建
#    先按快照开关把 ntuple 打开到可操作状态
NT_FEAT=""; NT_STATE=""
if [ -s "$SNAP/ntuple-state.txt" ]; then
    NT_FEAT=$(awk -F: '{gsub(/[[:space:]]/,"",$1); print $1}' "$SNAP/ntuple-state.txt")
    NT_STATE=$(awk -F: '{print $2}' "$SNAP/ntuple-state.txt" | awk '{print $1}')
fi
if [ "$NT_STATE" = "on" ] && [ -n "$NT_FEAT" ]; then
    ethtool -K "$ETH" "$NT_FEAT" on 2>/dev/null || true
fi

if [ -f "$SNAP/ntuple.json" ] && command -v python3 >/dev/null 2>&1; then
    python3 "$SKILL_DIR/scripts/ntuple_safe_rollback.py" restore \
        --iface "$ETH" --snapshot "$SNAP/ntuple.json" --report "$SNAP/ntuple-restore.json" \
        || echo "ERROR: ntuple 自动恢复存在失败项，见 $SNAP/ntuple-restore.json；按 $SNAP/ethtool-n.txt 人工核对" >&2
else
    # 兜底：至少不误删既有规则（原始 location 集合来自快照）
    if [ -s "$SNAP/ntuple-locations.txt" ]; then
        for loc in $(ethtool -n "$ETH" | awk '/^Filter:/{print $2}'); do
            grep -qxF "$loc" "$SNAP/ntuple-locations.txt" || \
                ethtool --config-ntuple "$ETH" delete "$loc"
        done
        echo "WARN: 未重建既有规则；若队列变更清空了原规则，请按 $SNAP/ethtool-n.txt 人工恢复" >&2
    else
        echo "WARN: ntuple-locations.txt 为空或缺失，无法区分既有/新增规则，拒绝自动删除；请按 $SNAP/ethtool-n.txt 人工核对" >&2
    fi
fi

# 4) 恢复 ntuple 开关原状态（原为 off 时最后关闭）
if [ "$NT_STATE" = "off" ] && [ -n "$NT_FEAT" ]; then
    ethtool -K "$ETH" "$NT_FEAT" off 2>/dev/null || true
fi

# 5) 恢复 RPS/RFS（§11 兜底可能改写）
if [ -f "$SNAP/rps-rfs.txt" ]; then
    while read -r qn kv; do
        key=${kv%%=*}; val=${kv#*=}
        f="/sys/class/net/$ETH/queues/$qn/$key"
        [ -n "$val" ] && [ -e "$f" ] && echo "$val" > "$f"
    done < "$SNAP/rps-rfs.txt"
fi
if [ -s "$SNAP/rps_sock_flow_entries.txt" ]; then
    sysctl -w net.core.rps_sock_flow_entries="$(cat "$SNAP/rps_sock_flow_entries.txt")"
fi

# 6) 恢复进程亲和性
for pid in $(pgrep -f '[r]edis-server'); do
    [ -f "$SNAP/taskset-$pid.txt" ] && \
      taskset -cp "$(sed -E 's/.*affinity list: //' "$SNAP/taskset-$pid.txt")" "$pid"
done

# 7) 恢复 redis.conf / sysctl / THP
if [ -f "$SNAP/redis.conf.orig" ] && [ -n "${REDIS_CONF:-}" ]; then
    cp -a "$SNAP/redis.conf.orig" "$REDIS_CONF"
else
    echo "ERROR: 快照中无 redis.conf.orig 或 REDIS_CONF 未设置，redis.conf 未回滚；请人工恢复并确认当前配置" >&2
fi
awk -F'=' '{gsub(/ /,"",$1); gsub(/ /,"",$2); print $1"="$2}' "$SNAP/sysctl.txt" \
  | xargs -r -n1 sysctl -w

# THP 按快照恢复（取方括号中的当前值）
THP=$(sed -E 's/.*\[([a-z]+)\].*/\1/' "$SNAP/thp.txt")
[ -n "$THP" ] && echo "$THP" > /sys/kernel/mm/transparent_hugepage/enabled

# 若本次新建了 /etc/sysctl.d/99-redis.conf，回滚时按需删除
```

回滚后复验：`ethtool -l/-n`、`taskset -pc`、`systemctl is-active irqbalance`、压测回到基线量级。

---

## 11. 故障排查与平台差异

| 现象 | 原因 | 处理 |
|------|------|------|
| `ethtool -L: Operation not supported` | virtio/veth/云网卡/驱动不支持可调队列 | `ethtool -l` 确认；跳过该层，或由虚拟化侧配置多队列 |
| `ethtool -L: Invalid argument` | combined 超过最大值，或需先 `ip link set down` | 查 Pre-set maximums；维护窗口内 down/up（SSH 风险） |
| `Cannot get ntuple filters: Operation not supported` | 网卡/驱动无 ntuple | 跳过；RPS/RFS 兜底（见下） |
| `Cannot insert filters: Operation not supported` | 不支持指定字段组合（如 dst-ip+dst-port） | 简化规则；仍失败则退化为哈希分流 |
| `action` 设置成功但不生效 | action ≥ 队列数；或规则被后续规则覆盖 | 确保 action < combined；`ethtool -n` 核对 |
| IRQ 绑定后又被改回 | irqbalance 未停 | `systemctl stop irqbalance`；或 `irqbalance --banirq=<irq>` |
| 改 `-L` 后中断没了/名字变了 | 驱动重建队列与 MSI-X | 重新读 `/proc/interrupts`，按 §6 重新绑定 |
| `/proc/irq/...: Permission denied` | 非 root / 容器无权限 | 宿主 root；容器需 privileged 且 host netns |
| `taskset: Invalid argument` | 核不在 cgroup cpuset 允许范围 | 查 `cat /sys/fs/cgroup/cpuset.cpus.effective`（v2） |
| 压测无变化 | loopback/同机压测；或客户端瓶颈 | 换远程客户端；`mpstat` 确认 |
| 双路机 `numa_node=-1` | BIOS/IOMMU 未归属或设备虚拟 | 查 BIOS；报告中注明未知，不臆造 |

**RPS/RFS 兜底**（ntuple 不可用、且软中断集中在单核时）：

```bash
# 把 rx-0 的软中断分散到节点内多个核（十六进制掩码，按实际核调整，f0 = 核 4-7）
echo f0 > /sys/class/net/$ETH/queues/rx-0/rps_cpus
# RFS：按流哈希分散，需同时设置全局表
sysctl -w net.core.rps_sock_flow_entries=32768
echo 4096 > /sys/class/net/$ETH/queues/rx-0/rps_flow_cnt
```

注意：RPS 只能按哈希分散，不能按端口指定核；启用后必须复测，部分场景会增加跨核 cache miss。

> 本节改动前已由 §1 快照记录 `rps_cpus`/`rps_flow_cnt`/`net.core.rps_sock_flow_entries`；回滚见 §10 第 5 步。

**网卡/驱动差异**：
- Intel ice/i40e/ixgbe：中断名常为 `<ifname>-TxRx-<queue>`，队列号可直接读取。
- Mellanox mlx5：`mlx5_comp<N>@pci:<bdf>`，`N` 为 completion vector；通常与队列顺序一致，但仍需增量验证。
- 华为 hns3、virtio、部分云网卡：名称可能不含队列号，按 IRQ 号顺序 + dry-run + 增量验证。
- 任何平台改动前先 `ethtool -k $ETH | grep ntuple`、`ethtool -n $ETH` 确认能力。

**容器场景**：
- 需要 host 网络或 `--privileged`/`CAP_NET_ADMIN` 才能改队列、IRQ、ntuple；容器内改的是宿主机网卡，影响全机。
- 容器内 `/proc/interrupts` 可能是宿主视图；若不可读，到宿主机执行。
- cgroup cpuset 限制可用核，`taskset` 不能越过。

---

## 12. 深度分析（分层优化仍不达标时）

```bash
# 每核 CPU 分布（看 %usr/%sys/%soft/%irq/%idle）
mpstat -P ALL 1 30

# softirq 增量（NET_RX 是否集中）
grep NET_RX /proc/softirqs

# 实例热点（用户态函数）
perf top -C $CORE_i

# Redis 自身时延
redis-cli -h $IP -p $PORT --latency-history -i 1
redis-cli --intrinsic-latency 100

# 连接与队列
ss -s
ss -lntp | grep redis
redis-cli -h $IP -p $PORT INFO clients | grep -E 'connected_clients|blocked_clients'
```

解读：
- 实例核 `%usr`≈100、其他核空闲 → 单线程 CPU 瓶颈：优化命令/数据结构、pipeline、分片扩实例；绑核与网卡调优无法突破。
- IRQ 核 `%soft` 高、实例核空闲 → 中断未对齐或队列分配错误，回到 §6。
- `%sys` 高 → 系统调用/上下文切换多，检查连接数、pipeline、是否小包过多。
- P99 尖峰与 BGSAVE/AOF fsync 对齐 → 持久化策略或 fork 开销；纯缓存可评估关闭持久化（需确认）。
- `rejected_connections` 增长 → `tcp-backlog`/`somaxconn`/文件句柄不足。
- `mem_fragmentation_ratio` 高 → 内存碎片，评估 `activedefrag` 或重启（需确认）。

> perf 内核符号显示 `[unknown]` 时，确认 `kernel.kptr_restrict=0` 且具备 root/CAP_SYSLOG；无法解析时以用户态符号（redis-server/libc）做热点分析即可。
