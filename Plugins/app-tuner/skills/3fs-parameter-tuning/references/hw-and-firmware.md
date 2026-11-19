# 硬件与固件（BIOS / NVMe）

> **何时读我**：软件配置面已经排查完、带宽仍上不去，或要做跨集群/跨节点对比之前读本文 —— 先用 5 秒钟的 `nvme id-ctrl` 排除固件差异，再用 Redfish 核对 BIOS 属性；本文也给出"软件分析全做完才发现是硬件"的完整定界证据链模板。

> 深度根因分析方法（bpftrace 脚本写法、火焰图采集陷阱、分位数与直方图解读）见 skill `3fs-hotspot-code-optimization` 的 fdatasync 案例。本文只保留与"调参决策"直接相关的结论与证据。

节点角色与示例 IP 见 `SKILL.md`。

---

## 一、BIOS / 固件

### 1.1 读取方法（Redfish，唯一有效途径）

```bash
# BMC IP 发现（带内，免密 sudo 可用）
sudo ipmitool lan print 1 | grep 'IP Address'

# 密码用 read -s 注入, 不写进脚本/历史
read -rs BMC_PASS; export BMC_PASS
BMC_USER="${BMC_USER:-Administrator}"      # 鲲鹏 BMC 默认管理账号名
BMC_IP="<BMC IP>"

# 读全部 BIOS 属性
curl -k -u "${BMC_USER}:${BMC_PASS}" "https://${BMC_IP}/redfish/v1/Systems/1/Bios" | jq '.Attributes'

# 按需过滤
curl -k -u "${BMC_USER}:${BMC_PASS}" "https://${BMC_IP}/redfish/v1/Systems/1/Bios" \
  | jq -r '.Attributes | with_entries(select(.key|test("PowerPolicy|Smt|HiBoost|Uncore|Prefetch|Interleav|CacheMode";"i")))'

# 修改（重启后生效）
curl -k -u "${BMC_USER}:${BMC_PASS}" -X PATCH "https://${BMC_IP}/redfish/v1/Systems/1/Bios" \
  -H 'Content-Type: application/json' -d '{"Attributes":{"CPUPrefetchConfig":"Enabled"}}'
```

`dmidecode -t bios` **只给版本，不给设置项**。属性注册表为 `BiosAttributeRegistry.2.0.3`。

### 1.2 x86 调优文档的参数名在鲲鹏上不存在

以下 x86（Intel/Hygon）常见名字在 4 台机器上**全部查无此项**：`lstPowerPolicy`、`SMT2___lstSmt2`、`HiBoostlst`、`uncore boost`。

- 鲲鹏**无 SMT**（`lscpu` → `Thread(s) per core: 1`），所有 SMT/超线程类建议不适用
- 电源策略的等价项是 **`CustomPowerPolicy`**

**跨平台搬运调优清单前必须先用 Redfish 列出实际属性名。**

### 1.3 实测属性值

| 属性 | 值 | 备注 |
|---|---|---|
| `CustomPowerPolicy` | **Performance**（4 台全部） | 已是最优 |
| `CPUPrefetchConfig` | meta = **Disabled** ⚠；storage1/storage2/fuse-client-1 = Enabled | **唯一真实的调优候选** |
| `EnableSMMU` | meta = **Disabled** ⚠；其它 Enabled | 不一致 |
| `SmmuWorkAround` | storage1 = **Enabled** ⚠；其它 Disabled | 不一致 |
| `NUMAEn` / `OneNumaEnable` | Enabled / Disabled | |
| `DieInterleaving` / `SktInterleaving` | **Disabled / Disabled** | |
| `ChannelInterleaving` / `RankInterleaving` | Enabled / 4-way Interleave | |
| `CacheMode` | `in:partition out:share` | |
| `StreamWrite` / `StreamWriteValue` | `Allocate share LLC` / `threshold: 12 lines` | |
| `ProcessorFlexibleRatio`(+Override) | 26（=2.6 GHz）/ Override Disabled | |
| `DDRFreqLimit` / `CREnable` / `DemtMode` | Auto / Enabled / Disabled | |
| `SpeEn` / `EnableL2PMU` | meta: Enabled/Disabled；storage2: Disabled/Disabled；storage1/fuse-client-1: 无此项 | 影响 SPE 采集能力 |
| `CpuCoreNumber` | Disabled（= 全核） | |

**BIOS 版本不一致**：1.38（storage1）/ 4.03_DVM（storage2）/ 1.70（meta）/ 1.38（fuse-client-1），属性集随版本不同（如 `AdaptivePrefetch` 只在 4.03_DVM 上有）。属性数 201/386/249/201。**跨节点对比 BIOS 项前先核对版本**，否则会把"版本没有该项"误读成"该项被关了"。

机型：TaiShan 200/2280，Kunpeng 920-4826 @2.6 GHz。

### 1.4 建议（本次未执行，用户要求只读）

1. meta 节点 `CPUPrefetchConfig` Disabled → **Enabled**（计算密集型 CRC/压缩负载，且与其它 3 台不一致）
2. 统一 BIOS 版本
3. 对齐 `EnableSMMU` / `SmmuWorkAround`

> BIOS 改动属于"重启面最大、收益最不确定"的杠杆，排在 `lever-ranking.md` 的第 9 位。做之前先确认配置面与硬件固件面已经收敛。

### 1.5 OS 侧：本平台无 cpufreq

```bash
ls /sys/devices/system/cpu/cpufreq/policy0/
# scaling_driver, scaling_governor, ... 全部为空
```

**该平台没有 cpufreq 驱动** → governor / boost / EPP / `platform_profile` 全部不适用。不要按 x86 习惯去调 `scaling_governor=performance`。

UEFI varstore（`/sys/firmware/efi/efivars/Setup-*`）存在但未解析。C-states / turbo 数据未采集。

---

## 二、NVMe 固件导致的 fdatasync 长尾

**这是 B 集群 3 GB/s（vs A 集群 8 GB/s）的最终根因**，也是"软件分析全做完才发现是硬件"的典型案例。

### 2.1 5 秒钟的前置检查（应在所有软件分析之前做）

```bash
nvme id-ctrl /dev/nvmeXn1 | grep -E 'mn |fr |sn '
nvme get-feature /dev/nvme0n1 -f 0x06 -H    # Volatile Write Cache
```

| 集群 | 型号 `mn` | 固件 `fr` |
|---|---|---|
| A（8 GB/s） | `HWE62P443T2M002N` | **1059** |
| B（3 GB/s，初始） | 混有 `HWE62P443T2M00GN` | **8312 / 8318** |
| B（换盘后） | `HWE62P443T2M002N`（与 A 同型号） | **8312** ← 仍慢 |

**换同型号盘后 B 仍 3 GB/s → 固件是唯一剩余差异。**

### 2.2 定界证据链

**① bpftrace（3FS 负载下，5 秒窗口，storage_main）**：

| 指标 | A | B | B/A |
|---|---|---|---|
| fdatasync 均值 | **264 µs**（36073 次, 7214/s） | **1210 µs**（8963 次, 1792/s） | **4.6x** |
| fdatasync 直方图峰值 | [64K,128K) ns（12831） | **[1M,2M) ns**（5131） | ~10x 右移 |
| fdatasync 累计墙钟 / 占用核 | 9.524 s / 1.904 核 | 10.853 s / 2.170 核 | 1.14x |
| pwrite64 均值 | **373 µs**（51332 次, 10266/s） | **576 µs**（16372 次, 3274/s） | 1.54x |

updates/s 7214 → 1792（4.0x）与带宽 8→3 GB/s 吻合 → **B 是磁盘 flush 受限**。

**② 裸盘 python append+fdatasync（最可信，复现 WAL 的 4K-append-then-fdatasync 模式）**

`/tmp/append_sync.py`，4K append + 每次 fdatasync，30 秒，落 `/storage/data0/append-sync-test`：

| 节点 | avg | p50 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| A st1 | 62.2 µs | 59.6 | 81.3 | 108 | 1215 |
| A st2 | 58.4 µs | — | 75.8 | — | — |
| B st1（3 次） | 99.8~120.1 µs | **66.5~68.7（≈A）** | **1071~1241** | **1710~2013** | — |
| B st2 | 82.0~99.2 µs | — | 674~933 | — | — |

**B 的 p50 与 A 相当，p99 是 A 的 10~15 倍** —— 系统性长尾，不是均匀变慢。**均值会完全掩盖这一点，必须看分位数与直方图。**

**③ A 集群 `direct=1 + fdatasync`**：sync 均值 **2.3 µs**、p99 40~42 µs → A 的 FLUSH 极快。

> 这条决定了 `sync_when_write=false` 的收益上限：FLUSH 本身快的集群，关掉 sync 涨不了 26.8%（见 `config-mechanics.md` §3.1）。

### 2.3 逐项排除清单

| 候选原因 | 排除依据 |
|---|---|
| XFS 参数 | `xfs_info /storage/data0` 逐字节相同：`isize=512 agcount=4 agsize=195351062 bsize=4096 crc=1 finobt=1 sparse=1 rmapbt=1 reflink=1 bigtime=1 inobtcount=1 nrext64=1, log internal blocks=381545` |
| 挂载选项 | 两边均 `rw,noatime,nodiratime,attr2,inode64,logbufs=8,logbsize=32k,noquota`（B 多一个 `seclabel`） |
| 内核版本 | 均 `6.6.0-145.3.13.143.oe2403sp3.aarch64` |
| XFS 块分配路径 | `--fallocate=posix` 预分配后 randwrite 4K + fdatasync，B st1 仍 548.5 µs / p99 1631（A 对照 24.5 µs / p99 64）→ **预分配也慢** |
| 3FS 软件 | 裸盘测试已绕开 3FS |
| 磁盘型号 | 换成与 A 相同的 `HWE62P443T2M002N` 后仍慢 |
| **NVMe 固件** | A `fr=1059` vs B `fr=8312` → **唯一剩余差异** |

**决定性验证（待做）**：把 B 的一块盘升到 1059 固件，重跑 python append 测试，预期 p99 回到 ~80 µs。

**这张表的价值在于"排除法要逐项留证据"**：每一行都是一次实测，而不是一句"应该不是它"。

### 2.4 因果链

```
8312 固件 FLUSH 长尾（裸盘 p99 0.7~1.6 ms）
  → WAL fdatasync 毫秒级
  → 3FS 并发下放大到均值 1.2 ms
  → 每次 update 多阻塞 ~1 ms（meta_store.rs:348-355 硬编码 sync=true，不可配置）
  → updates/s ÷4
  → 8 → 3 GB/s
```

**软件侧无化解空间**（update 阶段 sync 硬编码），只能统一硬件固件。

### 2.5 其它硬件/OS 基线（未调，供对照）

- **NUMA**：4 节点（node0 CPU 0-23、node1 24-47、node2 48-71、node3 72-95）；ConnectX-5（81:00.0/81:00.1）在 **NUMA node 2**；`ibp129s0f0`/`enp129s0f1np1` 也在 node 2；NVMe `numa_node: N/A`
- **L3**：storage1 与 fuse 客户端 192 MiB（4 实例）；**meta 与 storage2 只有 96 MiB** —— 跨节点对比时这是干扰因素
- **sysctl 基线**：`net.core.rmem_max=wmem_max=229376`、`netdev_max_backlog=1000`、`tcp_congestion_control=cubic`、`tcp_rmem=4096 131072 6291456`、`tcp_wmem=4096 16384 4194304`、`vm.dirty_ratio=60`、`vm.dirty_background_ratio=10`、`vm.dirty_expire_centisecs=3000`、`vm.dirty_writeback_centisecs=500`、`vm.swappiness=10`、`vm.nr_hugepages=4096`（Hugepagesize 2048 kB；AnonHugePages 10764288 kB → THP 生效）
- **服务**：`irqbalance.service` enabled/active；NVMe 调度器 `[none] mq-deadline kyber bfq`（none 生效）
- **时间同步**：meta 跑 ntpd，一台客户端跑 chronyd（均已同步）；storage3 曾慢 34 秒（见 `topology-and-placement.md` §五）
- **裸盘带宽基线**（A，5 次均值，Direct）：单盘顺序读 5324~5545 MB/s、顺序写 4519~4622 MB/s
- **未做任何 sysctl / THP / NUMA balancing / IRQ affinity 改动**；CPU 绑核只用在测试脚手架（`taskset -c 12`，`scripts/fio_bench.sh` 用 `TASKSET_CPU=` 暴露）
