# DPDK 调优双 Skill 手工实操手册

对应两个 skill 的目标环境手工执行手册：**`kptools-dpdk-tuning`**（app-tuner，应用/配置层调优）与
**`dpdk-optimization`**（code-optimizer，数据面业务代码调优）。顺序固定：**先 A 后 B**——
配置层没对齐就做代码优化，收益会被环境问题吃掉。

> 本手册所有"实测"标注均来自 Kunpeng 920 5258F（aarch64，80 vCPU，单 NUMA）+
> openEuler 24.03（kernel 6.6.0）+ DPDK 26.11.0-rc2 的真机验证；x86 环境命令通用，
> 个别参数（simd 位宽、IOMMU GRUB 项）按平台调整。
>
> 红线：不重启机器；不碰承载管理网络的网卡（先 `ip route` 确认默认路由走哪个口）。

## 安装与触发

```bash
# 把两个 skill 装进目标项目的 AI 工具（OpenCode 示例）
bash /path/to/KPBot/install.sh project opencode /path/to/your-dpdk-app
opencode debug skill | grep -E "kptools-dpdk|dpdk-optimization"   # 确认注册
```

装好后可直接对话触发：*"DPDK 应用 PPS 低，帮我排查"*（→ kptools-dpdk-tuning）、
*"优化这个转发应用的收发包热路径"*（→ dpdk-optimization）。手工执行按下面流程。

---

# Part A · kptools-dpdk-tuning（应用/配置层）

## A1. 事实收集（第 0 步，一次性拿全）

```bash
# --- 硬件拓扑 ---
lscpu | grep -E 'NUMA|Socket|Model name|CPU\(s\)'
lspci -nn | grep -i eth
for d in <业务网卡PCI地址>; do
  cat /sys/bus/pci/devices/0000:$d/numa_node      # 期望与 worker 核同 node
  lspci -s 0000:$d -vv | grep LnkSta              # Speed/Width 都要看，x16 槽走 x8 也不少见
done
dmidecode -t memory | grep -E 'Locator|Speed' | head -20

# --- 内核/OS ---
cat /proc/cmdline                                 # 有无 isolcpus/nohz_full/irqaffinity/大页
cat /sys/devices/system/cpu/isolated              # 空 = 没做核隔离
grep -E 'HugePages_Total|Hugepagesize' /proc/meminfo
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null   # 期望 performance
systemctl is-active irqbalance                    # active = 中断会乱跑
cat /sys/kernel/mm/transparent_hugepage/enabled   # DPDK 场景建议 never/madvise

# --- 应用（已运行时）---
ps -eLo pid,psr,comm | grep -i <app>              # worker 线程落在哪些核
taskset -pc <worker_pid>                          # 实际亲和掩码
```

**检查点**：如果 `isolated` 为空、governor 非 performance、HugePages_Total=0，
先记下来——这三项是后面调优的主目标，不用现在改。

## A2. 症状分流（诊断模式入口）

| 症状 | 走向 | 首查 |
|------|------|------|
| 吞吐/PPS 低于预期 | 树 A | 有基线吗？→ 每队列收包分布 → CPU 是否满 |
| 时延高/毛刺 | 树 B | 均值还是尾部？尾部毛刺查隔离核 ctx-switches |
| 丢包 | 树 C | **xstats 三层判据**：imissed（描述符不足）/ nombuf（mbuf 池空或泄漏）/ ierrors（设备错误） |
| 启动失败/OOM | 树 D | EAL 日志逐行读 + A1 的大页数 |
| CPU 满但吞吐不涨 | 树 A→E | perf top -C worker 核 → 热点在业务代码则移交 Part B |

分层归因铁律：硬件/BIOS → 内核/OS → EAL → mbuf/内存池 → PMD/网卡 → 应用配置 → 代码。
上层症状往往是下层根因，**自底向上排查**。

## A3. 配置层体检清单（顾问模式，每项可回退）

按性价比顺序执行，**每改一项测一项**：

```bash
# 1. NUMA 三点一线（收益最大的第一项）：worker 核 socket == 网卡 numa_node == mempool socket_id
cat /sys/bus/pci/devices/<nic>/numa_node; lscpu | grep NUMA

# 2. 核隔离三件套（需 GRUB，标注"需重启"项，攒齐一次改）
#    isolcpus=<worker核列表> nohz_full=<同> irqaffinity=<housekeeping核>  且不给 CPU0
# 运行时验证（改完重启后）：
cat /sys/devices/system/cpu/isolated
perf stat -C <isolated_cpu> -e context-switches -- sleep 5   # 期望接近 0
taskset -pc <worker_pid>                                     # worker 真的落在隔离核

# 3. 大页（64 位应用官方推荐 1G 页；先试运行时，失败才动 GRUB）
echo 8 > /sys/kernel/mm/hugepages/hugepages-1048576kB/nr_hugepages   # 1G 页试运行时预留
grep Huge /proc/meminfo                                              # Free 数必须增长，否则回退走 GRUB
# 1G 页必须专用挂载（默认 /dev/hugepages 是 2M 页的！）[实测]
mkdir -p /mnt/huge1G && mount -t hugetlbfs -o pagesize=1G nodev /mnt/huge1G
# 应用侧配 --huge-dir /mnt/huge1G 才能用上

# 4. governor / 中断
cpupower frequency-set -g performance    # 或 tuned-adm profile throughput-performance
systemctl disable --now irqbalance       # 配合内核 irqaffinity= 使用

# 5. 队列-核映射 + 描述符 + burst（应用启动参数层，改完重启用 A5 基线复测）
#    每 worker 独占一条 RX 队列、同 socket；rxd 默认 128 偏小，突发丢包场景 1024 起试
```

## A4. DPDK 特有观测（M8，已运行/可运行时用）

```bash
# secondary 附加运行中的主进程（应用不能是 --in-memory/--no-shconf 起的）
dpdk-proc-info --file-prefix <主进程前缀> -- --stats        # 端口统计
dpdk-proc-info --file-prefix <主进程前缀> -- --xstats       # 驱动扩展统计（丢包分层判据在这）

# telemetry（v2 socket 是 SEQPACKET；带 --file-prefix 的主进程，客户端要传同样前缀）
ls /var/run/dpdk/<prefix>/dpdk_telemetry.v2
# [实测注意] dpdk-telemetry.py 用管道喂 stdin 拿不到输出（需要交互 TTY），
# 脚本化查询直接用裸 socket（3 行）：
python3 -c "
import socket, re
s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)   # 不是 SOCK_STREAM
s.connect('/var/run/dpdk/<prefix>/dpdk_telemetry.v2')
s.recv(1024)                                  # 握手：{\"version\":\"DPDK ...\"}
s.send(b'/ethdev/stats,0\n')                  # 端点名不加 {} 占位符
print(s.recv(65536).decode())
"
# [实测注意] --stats-period 输出里的 Throughput 段需要 --bitrate-stats=N 才非 0

# testpmd 自包含演示架（可选 — 手头没有业务应用时用它演示测量链）[实测可跑]
dpdk-testpmd -l 2-4 --numa-mem 1024 --huge-dir /mnt/huge1G \
  --vdev virtio_user0,path=/dev/vhost-net --vdev virtio_user1,path=/dev/vhost-net -- \
  -i --nb-cores=2 --forward-mode=txonly --rxd=1024 --txd=1024 --burst=32 --mbcache=64 \
  --record-core-cycles
# 交互内：start → 等 20s → stop → read fwd core cycles stats
# 关键输出行：CPU cycles/packet=84.47 (busy cycles=... / total txonly packets=...)
# 脚本化（非交互）：必须带 --stats-period N，否则启动转发后立即退出 [实测]
# 四轮实测 cycles/packet：84.94 / 84.47 / 83.66 / 82.84（离散 ~2.5%，口径稳定）

## A4.1 测量口径（应用无关 — 不绑定 testpmd）

| 测什么 | 方法（来源优先级从高到低） |
|--------|------------------------------|
| PPS | 业务应用自身计数 > telemetry `/ethdev/stats` > `proc-info --stats` > 外部打流对端计数 > `--stats-period` 日志差分 |
| cycles | `perf stat -t <worker TID>` — TID 用 `ps -eLo tid,psr,comm` 按 CPU 号查（`-p/-t` 是 PID/TID，不是 lcore 号！）|
| cycles/packet | 同一时间窗内 cycles ÷ Δpackets，两数必须同窗且绑定同一应用；不同应用的绝对值不可互比 |
| 热点 | perf record + report（天然应用无关）|

铁律（90.60 实测依据）：
1. telemetry 是 **EAL 级能力，任何 DPDK 应用都有**（除非构建裁剪或 `--no-telemetry`）；
   被禁时的降级链：`--stats-period` 日志差分（零依赖，实测可用）→ `proc-info`（走 mp_socket）→ 交互 `read fwd`
2. 被测机自发自测会失真：tap/vhost 反压会让 opackets 冻结、worker 空转烧 cycles [实测]。
   A/B 判定优先外部流量源；纯计算口径演示用 `net_null` PMD（发送永不反压，实测 87.7Mpps 稳定取数）
3. testpmd 不构成口径依赖——真实场景一律以业务应用为测量主体
```

有真实网卡压测时把 vdev 换成 `-a <pci1> -a <pci2>` + `--forward-mode=macswap`，
流量从对端机器打（被测机自身不发流量）。

## A5. 常见坑速查（实测记录，全部在 90.60 踩过）

| 坑 | 现象 | 处置 |
|----|------|------|
| 1G 大页挂载 | EAL 报 "no mounted hugetlbfs found for that size" | 默认 `/dev/hugepages` 是 2M 页；1G 页 `mount -o pagesize=1G` + `--huge-dir` |
| 1G 大页运行时预留失败 | `echo N > nr_hugepages` 写入成功但 Total 不涨 [实测复现：内存碎片化后] | 换 2M 页运行时预留（4096×2M 实测成功），或 GRUB 启动参数预留 |
| 1G 大页重启后仍失败 | 重启清零内存后运行时预留依旧 Total=0 [实测：排除碎片化假设] | GRUB 启动参数预留是唯一路径，别再试运行时 |
| 0 大页时 EAL 全挂 | 大页未配时连 `--version` 都 `Cannot init EAL: Permission denied` [实测] | 版本号仍会打出；脚本化采集前先确认大页已配 |
| perf 附着全 not counted | `perf stat -p 3,4` 把 lcore 号当 PID 传 [实测] | `-p/-t` 收 PID/TID：`ps -eLo tid,psr,comm` 查 worker 线程再 `-t`；且 `perf_event_paranoid` 重启后回 2，需 `echo 1` |
| 非交互 testpmd 秒退 | nohup/脚本里不带 `-i` 的 testpmd 启动转发后 1s 内退出 | 非交互模式必须加 `--stats-period N`（主线程保持显示循环）[实测复现] |
| telemetry 客户端无输出 | `echo \| dpdk-telemetry.py` 管道方式拿不到结果 | 客户端需要交互 TTY；脚本化用裸 SEQPACKET socket（见 A4 代码块） |
| hinic/Hi1822 初始化 | `NET_HINIC: Check card workmode failed` → PMD 起不来 | 卡固件/工作模式级问题，别在 PMD 参数上试；先查固件 |
| mlx5 PMD 静默消失 | 构建无报错但产物里没有 mlx5 | 只装了运行库没装 `rdma-core-devel`；核对 `ls build/drivers/librte_net_mlx5.a` |
| telemetry 连不上 | 客户端找不到 socket | 主进程带 `--file-prefix` 时 socket 在 `/var/run/dpdk/<prefix>/`，客户端传同样前缀；socket 是 SEQPACKET |
| Throughput 恒 0 | stats-period 有统计但速率 0 | 需要 `--bitrate-stats=N` 指定统计 lcore |
| mlx5 旧参数 | `txqs_max_vec`/`tx_vec_en` 等调了没反应 | 26.11 已弃用且被忽略；inline 用 `txq_inline_max`（默认 290/上限 930） |
| EAL 版本名 | 参数不生效 | EAL 参数随版本变（`--socket-mem`→`--numa-mem`）；先 `dpdk-testpmd --version` 确认版本再查文档 |
| telemetry 被禁用 | `--no-telemetry`/构建裁剪后 socket 不存在 [实测] | 走 A4.1 降级链：日志差分 → proc-info → 交互 read fwd |
| 自发自测计数冻结 | virtio_user 自发+对端无人收 → opackets 不涨、worker 空转 [实测] | 外部流量源优先；纯计算演示换 net_null PMD |

---

# Part B · dpdk-optimization（业务代码层）

前置：Part A 的 A3 体检全过（三点一线、隔离核、大页、governor）。没过就先回 A。

## B0. 输入五要素（缺则补齐，不猜）

| 字段 | 说明 |
|------|------|
| repo | 业务代码路径 |
| worker_entry | worker 主循环/收发包回调所在源文件 |
| benchmark_cmd | 可复现基准（testpmd 或业务基准，含流量模型：包长/流数/速率） |
| optimization_goal | Mpps / P99 时延 / cycles per packet |
| constraints | 允许改数据布局？允许改并发语义（worker 模型）？正确性红线？ |

## B1. 线速预算（决定上限，先算再动手）

```
pps = 线速 bps / ((64+20)×8)
10GE: 14.88 Mpps ≈ 67.2 ns/包 ≈ 201 cycles/包 @3.0GHz
25GE: 37.20 Mpps ≈ 26.9 ns/包 ≈  81 cycles/包 @3.0GHz
100GE: 148.8 Mpps ≈ 6.72 ns/包 ≈ 20 cycles/包 @3.0GHz（单核 run-to-completion 基本不可行 → 先考虑 pipeline/分流）
```
按目标机器实际主频换算。预算超支一个量级 → 微优化无意义，先改 worker 模型。

## B2. 基线冻结（每项落档：配置快照 + 命令原文 + 数字）

```bash
# 总 PPS + 每 worker 分布 + cycles/pkt（A4 的 testpmd 命令或业务基准）
# xstats 基线：imissed/nombuf/ierrors 必须无持续增长
# 正确性口径：丢包率=0 / 业务收发计数对账（收包数=发包数±声明）
```

## B3. 热点采样（worker 核，attach 真身）

```bash
# 热点与调用链（按核，稳定区间采样，排除预热段）
perf record -g --call-graph fp -C <worker_cpu> -- sleep 20
perf report --no-children | head -40

# 一阶定性（有 DevKit 用 DevKit，没有用 perf stat 近似）
perf stat -C <worker_cpu> -e task-clock,context-switches,cache-misses,instructions,cycles -- sleep 5
# [实测参照] 90.60 上的健康 worker：task-clock=5.003s(100%忙)、ctx-switches≈20/5s、IPC=2.69
# cyc 段异常 → 检查隔离核；cache-misses 占比高 → 走 memory-latency 路径

# 行级访存归因（SPE，平台支持时）
devkit tuner miss -m 1 -C <worker_cpu>     # miss 落在哪个数据结构哪一行
devkit tuner top-down -L 1 -C <worker_cpu> # Retiring/Backend/BadSpec 一级占比
```

**假热点排除**：泛型薄包装、驱动内部函数（`hns3_*`/`mlx5_*` → 配置层问题，不是代码问题）、
日志/统计函数占比大 → 先修热路径纪律（B4-0）再复测。

## B4. 瓶颈判定 → 对应杠杆

| 一阶结果 | 瓶颈类 | 杠杆（references/code-levers.md） |
|---------|--------|----------------------------------|
| Retiring 高 + PPS 低 | compute | §1 循环结构 / §3 查表解析 / §6 指令层 |
| Backend→Memory，LLC miss 高、DDR 不满 | memory-latency | §2 per-lcore 布局 / §3 查表 / §5 内存管理 |
| DDR 带宽实测打满 | bandwidth | §5（先证伪：多数是冗余拷贝/跨核搬运） |
| 锁/原子/系统调用占比高 | sync-syscall | §4 无锁统计/SP-SC/批提交 |
| Bad Speculation 高 | 分支误预测 | §3.4 热冷分离 + branch-elimination |
| ring 满 + 下游慢 | pipeline 阻塞 | 定位下一级瓶颈点，转下一段分析 |

**任何瓶颈类都先过纪律表（§0）**：每包 printf/LOG、malloc、每包原子 RMW、每包系统调用、
跨 cache line 共享写——这五项是必改项，不修则其他杠杆收益被吸收。

## B5. 杠杆实施纪律

1. 单变量：每轮只改一个杠杆，`#ifdef` 隔离 A/B 双构建
2. 指令类杠杆先微验证：/tmp 写 5 行探针 objdump 确认指令映射（NEON/prfm 真的落地），改完再看热点函数反汇编防静默回退
3. 先正确性后性能：正确性口径全过才跑 A/B
4. A/B **交错多轮取中位**（不是先跑完 A 再跑 B），cv% 超阈值的轮次剔除
5. 收益 <2-3% 或中位差小于轮间波动 → 回滚 + 记负结果台账（手段/现象/结果/根因/启示）
6. 每轮采纳后重跑 B3，确认新瓶颈再选下一个杠杆（收益按"暴露下一层"估，不线性叠加）

**A/B 实测参照**（90.60，txonly+virtio_user，burst 32 vs 64 交错 3 轮、每轮 10s、
telemetry 取数，port0 TX pps）：

```
burst=32: 1129728 / 1128345 / 1127193  → 中位 1128345
burst=64: 1129024 / 1128083 / 1127808  → 中位 1128083
中位差 0.02% ≪ 2-3% 噪声带 → 无收益，回滚记账
```

判定理由：tap/vhost 后端反压主导（TX-dropped 大量堆积），burst 大小不是
binding constraint——这正确演示了"收益被环境瓶颈吸收"的判读方法。

## B6. 输出

按 skill 输出契约出报告：口径与预算 → 基线 → 瓶颈模型（bottleneck_class/binding_constraint/
closed_axes）→ 杠杆 ROI 表 → 每轮实施与验证 → 负结果台账 → 下一步。

---

## 两个 skill 的协作关系

```
应用没跑过基准？          → A4 建 testpmd/业务基线（kptools-dpdk-tuning M8）
配置层体检没过？          → A3 分层调优（先测后调，逐项回退）
热点确认在业务代码？      → 移交 B（dpdk-optimization），A 侧记录移交项
B 需要重启窗口做 A/B？    → 与业务方确认窗口；B 的阶段 0-2 可以在运行中进程上做
改码需求涉及锁/哈希表？   → B 联动 code-optimizer 的 hashmap-optimization skill
```

## 参考

- 完整方法论：`Plugins/app-tuner/skills/kptools-dpdk-tuning/`（M1-M10 + 诊断/顾问工作流）
- 代码杠杆库与瓶颈建模：`Plugins/code-optimizer/skills/dpdk-optimization/references/`
- 官方文档：doc.dpdk.org/guides（版本溯源见 app-tuner skill 的 UPSTREAM.md）
