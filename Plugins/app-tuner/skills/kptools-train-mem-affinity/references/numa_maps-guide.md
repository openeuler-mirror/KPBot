# numa_maps 字段手册与口径坑

> 采集脚本 `collect_rank_mem.py` 的解析依据。所有坑来自实测/内核文档，
> 涉及口径的部分在 methodology.md §0 有方法论级展开。

## numa_maps 行格式

每行一个 VMA（虚拟内存区）。**注意：numa_maps 没有 smaps 的
perms/offset/dev/inode 列**，真实格式是：

```
00400000 default file=/bin/cat mapped=1 mapmax=2 N0=25
55d0f2e00000 default heap N0=412 N1=88 anon=500 kernelpagesize_kB=4
7f3a30000000 bind:0 file=/dev/shm/hccl_123 mapped=64 N1=64 kernelpagesize_kB=2048
7fffce7e5000 default stack anon=6 N0=4 N1=2 kernelpagesize_kB=4
2b9d3b400000 interleave:0-3 anon=8575 dirty=673 N0=2157 N1=2171 kernelpagesize_kB=4
```

| 位置 | 含义 | 解析要点 |
|------|------|---------|
| 位置 1 | VMA 起始地址 | - |
| 位置 2 | **内存策略**（default/bind:N/interleave:0-3/prefer:N） | 是进程 mempolicy 的唯一可见线索 |
| `heap` / `stack` | 裸标记（无 `=`） | VMA 归因：主堆/栈 |
| `file=<path>` | 映射文件 | VMA 归因：权重/共享库/dev/shm 通信缓冲 |
| `N<x>=<n>` | **在 node x 驻留 n 个物理页** | 核心字段；单位是页不是 KB |
| `anon=<n>` | 匿名页数 | 换算 KB 乘 kernelpagesize |
| `kernelpagesize_kB=<kb>` | 该 VMA 页大小 | **缺省按 4**；大页 VMA 为 64/2048 等 |
| `mapped/mapmax/dirty/active/swapcache/...` | 页状态细分 | 本 skill 不用；`mapmax` 仅在同 VMA 页被多进程映射（mapcount_max>1）时由内核打印 |

解析规则（collect_rank_mem.py 的实现依据）：前 2 个 token 固定；其余
token 中 `heap`/`stack`/`file=` 提取为 VMA label，含 `=` 的按 key=value
（`N\d+=` 识别为 node 页数），其余裸 token（policy 参数）忽略。

> 与 smaps 的区别速记：smaps 有 perms/offset/dev/inode/Size/Rss/Pss，
> 但**没有 node 归属**；numa_maps 有 node 归属但没有 Pss/Rss。两者都读
> 才能做"私有驻留按 node"的精确口径——本 skill 取实用近似：anon 列
> 即私有驻留的 node 口径（见下）。

## 单位与换算（最容易错的地方）

- `N<x>=` 的计数单位是**物理页**，页大小由该 VMA 的
  `kernelpagesize_kB` 决定：4KB 页 VMA 的 `N0=100` = 400KB；
  2MB 大页 VMA 的 `N0=3` = 6144KB
- 聚合公式：`node驻留KB = Σ(N<x> × kernelpagesize_kB)`，
  `anonKB = Σ(anon × kernelpagesize_kB)`
- file 部分 = total − anon（`anon=` 只出现在有匿名页的 VMA；用差值最稳）
- **覆盖率门**：`Σnode驻留KB ÷ VmRSS` 应在 0.9~1.05；显著超出说明页大小
  换算错（见 methodology C6）

## anon/file 分摊到 node 的口径

单个 VMA 内 `anon=500` 无法区分这 500 页在哪个 node（`N0=412 N1=88` 只给
总数分布）。本 skill 按**页数比例分摊**：node0 分得 anon 500×412/500。
这是近似——对判据的影响：anon 主 node 判定在 VMA 内部分布均匀时准确，
极端失衡（同 VMA 内 anon 集中在某 node）会失真。VMA 粒度通常足够
（权重/heap/通信缓冲是不同 VMA）。

## file 页的共享计数坑

两个 rank mmap 同一权重文件：各自 numa_maps 都计 `N0=...`，物理上
page cache 只有一份。**跨 rank 对比与挤兑判断（C4）必须用 anon 列**，
file 列只做 VMA 归因参考。

## 读取代价与时机

- numa_maps 是内核逐 VMA 统计的生成文件：大模型进程几十万 VMA 时
  **一次读取数秒**、且有内核锁开销——只做快照，禁止轮询；采样时机选择
  见 methodology §4
- 读取时进程在继续跑：数字是读取过程中的近似瞬时值，VMA 顺序扫描
  （先后有毫秒级差异）——对结论无影响，但两份快照对比时增量要考虑
  自然涨落
- 同 uid 免 root；跨 uid 读 numa_maps/status 需 root（与 /proc 其他
  接口一致）

## environ 的 rank 字段

| 场景 | rank | local_rank | world_size |
|------|------|-----------|------------|
| torchrun / torch elastic | `RANK` | `LOCAL_RANK` | `WORLD_SIZE` |
| mpirun (OpenMPI) | `OMPI_COMM_WORLD_RANK` | `OMPI_COMM_WORLD_LOCAL_RANK` | `OMPI_COMM_WORLD_SIZE` |
| SLURM srun | `SLURM_PROCID` | `SLURM_LOCALID` | `SLURM_NPROCS` |

- environ 为 NUL 分隔，同 uid 可读；读不到（跨 uid/权限）的进程会被
  列进 stderr 警告并跳过——**发现数 < WORLD_SIZE 时必须排查**（校验门）
- `--pid` 指定目标时不做全扫，但仍读其 environ 取 rank 信息；无 rank
  环境变量的 PID 以 `rank=?` 呈现（该 skill 定位为训练场景，非 rank
  进程不在自动发现范围）

## 系统侧对照数据

| 数据 | 路径 | 用途 |
|------|------|------|
| node 水位 | `/sys/devices/system/node/nodeN/meminfo` | C4 佐证（挤兑/空闲） |
| 远程访存率 | `/sys/devices/system/node/nodeN/numastat`（`local_node` vs `other_node`） | 系统级佐证 |
| 自动迁移开关 | `/proc/sys/kernel/numa_balancing` | C5 |
| 透明大页 | `/sys/kernel/mm/transparent_hugepage/enabled` | C6 排查（THP 统计竞态） |
| NPU 归属 | `/sys/bus/pci/devices/*/vendor`==`0x19e5` **且 class==`0x120000`**（或 Ascend device id d801/d802）→ 同目录 `numa_node` | C2（-1 = 未知，不判；单信 vendor 会把 Hi1822 网卡计入 NPU 集合） |

以上全部走 sysfs/procfs 原始文件，不依赖 numactl/numastat 命令
（两者缺失不影响本 skill；numastat -m 作为人工复核手段可在指南外使用）。
