# 训练 rank 内存 NUMA 归属分析方法论

> 本文档是 kptools-train-mem-affinity 的核心资产：采集脚本只负责取数和粗筛（hints），
> **最终结论由人/agent 按本文判据矩阵做出**。每条判据都给了证据要求、阈值和
> "何时不应下结论"的反例——没有证据链的结论不算结论。

## 0. 口径与前提（先读，再采样）

### 观测的是"驻留"，不是"申请"

`/proc/PID/numa_maps` 统计的是**已驻留物理页（RSS）在各 NUMA node 的分布**。
malloc 了未写入的内存不占物理页，观测不到。因此本方法能回答的问题是：

> "这个 rank 的进程实际占用的物理内存分布在哪些 node 上"——这也正是
> 性能诊断需要的问题（远程访存只发生在驻留页上）。

"申请侧"的两个间接证据：`Mems_allowed_list`（允许从哪些 node 分配）+
驻留分布本身（分配策略的结果）。

### anon 与 file 必须分列

- **anon**（匿名页：heap、栈、mmap 匿名映射、PyTorch 的张量/缓冲）是进程
  私有分配，归属唯一，**亲和性判据以 anon 为主**
- **file**（文件页：权重文件 mmap、共享库）在 page cache 中物理只有一份，
  多个 rank mmap 同一权重文件时 numa_maps 会**各自计数**（物理共享却重复
  统计）——跨 rank 对比时 file 页的数字要打折看，不能直接当私有占用

### 快照的时点语义

内存分布不是静态的：内核 `numa_balancing=1` 时会自动迁移页；训练阶段切换
（warmup/稳态/eval）也会改变驻留。**每份快照必须带时间戳与阶段标注**，
对比分析（--diff）才有意义。numa_maps 读取对大模型进程耗时数秒，只能快照，
不能轮询。

### 覆盖率校验门（结论前置条件）

聚合驻留（numa_maps 各 node 之和）与 `VmRSS` 的比值应接近 1：

- `< 0.9`：有页没统计到（swap、权限、THP 统计竞态）→ 结论降级，先查观测
- `> 1.05`：几乎必然是**大页换算错了**（kernelpagesize_kB 未识别）→
  观测 bug，结论作废
- 正常区间内才允许引用 node 占比下结论

## 1. 阶段流程

```
阶段 0 确认环境与目标 → 阶段 1 快照采集 → 阶段 2 判据判定 → 阶段 3 结论路由
```

### 阶段 0：环境与目标

1. 拓扑：node 数、每 node 容量与水位、numa_balancing、THP（discover.sh）
2. rank→PID 映射：environ 里的 `RANK`/`LOCAL_RANK`/`WORLD_SIZE`
   （torchrun）或 `OMPI_COMM_WORLD_RANK`（mpirun）；`--pid` 手工指定兜底
3. 目标时段确认：稳态（warmup 完成后）快照价值最高；必要时在关键阶段各采一份
4. 可选联动：同期跑一次 kptools-thread-affinity-collector（线程侧落点），`--affinity-dir` 联合判读

### 阶段 1：快照采集

- `collect_rank_mem.py` 输出每 rank 的 node×{anon,file} 驻留分布、
  覆盖率、top VMA 归因（`--top-vmas`）
- 时机纪律：两次采样之间若发生重启（PID 变化），--diff 按 rank 号对齐，
  但要在结论里注明进程已重启
- 系统侧对照：node 水位 + `/sys/devices/system/node/nodeN/numastat` 的
  `local_node`/`other_node`（系统级远程访存率，佐证而非定位）

### 阶段 2：判据矩阵（C1-C6）

机器 hints 只是**粗筛提示**（脚本按下方机械条件打标），最终判定必须人工
核对证据要求与反例。

---

#### C1 内存-CPU 跨域（远程访存嫌疑）

- **定义**：进程 anon 主驻留 node 与 CPU 侧（允许掩码覆盖的 node，或
  thread-affinity 观测的线程实际落点 node）不一致
- **机器 hint**：`C1_REMOTE_SUSPECT`（anon 主 node ∉ Cpus_allowed 覆盖的
  node 集合，且 anon 主 node 占比 ≥ 60%）
- **证据要求**：anon 主 node 占比、各 node MB 数、CPU 掩码/线程落点；
  只有占比模糊（40-60%）时不打结论，加采或换时段
- **反例（不应下结论的情形）**：
  - `numa_balancing=1`：内核正在迁移中，当前分布不代表稳态；先关再测或
    连采两份看趋势
  - anon 驻留占比过低（<30%）：file 页噪音大，anon 的绝对量可能无意义
  - 快照恰逢内存申请高峰（如扩容 batch）：瞬态分布
- **处置路由**：绑核/绑内存动作 → `cpu-affinity-optimization`（其已声明
  "绑核、绑内存"；hostbound 场景注意其宽松池纪律）

#### C2 内存-NPU 设备错位（Ascend 场景）

- **定义**：rank 进程的 anon 主驻留 node（尤其通信缓冲、H2D 阶段缓冲）
  不在任一 NPU 设备所在 node → 主机↔设备搬运跨 NUMA。NPU 设备识别 =
  PCI vendor `0x19e5` **加** class 0x120000（Processing accelerators）
  或 Ascend device id（d801/d802）双校验——华为全家桶（hns3/Hi1822
  网卡、iBMA、加密卡、PCI bridge）同为 0x19e5，单信 vendor 会把 NIC
  的 node 混入 NPU 集合令 C2 双向失真（误报+漏报）
- **机器 hint**：`C2_NPU_MISALIGN`（anon 主 node ∉ NPU node 集合，
  与 C1 同用"anon 主 node 占比 ≥ 60%"门槛——占比模糊时不判）
- **证据要求**：anon 主 node + NPU PCI `numa_node` + **top VMA 归因**
  （定位是哪些映射占了主驻留——[heap]/权重文件/通信缓冲/共享内存）
- **反例**：
  - NPU `numa_node=-1`（VM/容器 sysfs 常见）→ **不可判定，跳过**，
    绝不输出"错位"结论（没有归属数据时下结论属于编造）
  - 多 NPU 分布多 node：与任一设备同 node 即算对齐
  - HCCL 通信路径大量走设备 HBM / RDMA 网卡（RoCE）时，主机侧驻留只反映
    staging 部分，结论要结合 HCCL 拓扑（kptools-ascend-hccn）看
- **处置路由**：`cpu-affinity-optimization` 的 NPU 设备对齐流程；
  通信侧深查 → `kptools-ascend-hccn` / `kptools-ascend-profiling-analysis`

#### C3 cpuset 内存约束强制分离

- **定义**：`Mems_allowed_list` 允许的 node 集合与 CPU 掩码所在 node 无交集
  → 结构性强制远程（不是调优问题，是配置问题）
- **机器 hint**：`C3_FORCE_REMOTE`（两集合交集为空）
- **口径**：`Mems_allowed_list` 的数字**就是 NUMA node 号**（非 CPU 号，
  不经 cpu→node 映射）；`Cpus_allowed_list` 才做 cpu→node 映射——两者
  口径弄反会在多 node 机器上双向失真（内存允许 node1 却被当 cpu1 归
  node0 漏报；同域配置被误报）。解析结果记入 rank 的 `mems_nodes`/
  `cpu_nodes` 字段供复核
- **证据要求**：Mems_allowed_list + Cpus_allowed_list 的 node 交集；
  这是**确定性问题**（无需阈值），但必须同时确认 Cpus_allowed 不是全量
  （全量 CPU 时交集必非空，hint 不会触发）
- **反例**：交集非空但仅一个 node——属于"窄约束"，报告里注明即可，
  不算强制分离
- **处置路由**：容器/系统层 cpuset 配置（systemd slice、K8s CPU Manager、
  容器 mems）→ `os-optimization` / 平台配置，**不是应用层绑核能解的**

#### C4 跨 rank 内存热点

- **定义**：同机多个 rank 的 anon 主驻留堆在同一 node，挤兑该 node 带宽
  与容量
- **机器 hint**：`C4_NODE_HOTSPOT`（≥2 个 rank anon 主 node 相同，且存在
  至少一个其他 node 有充足空闲——否则是单 node 机器/约束，无热点可言）
- **证据要求**：rank×node 对比表 + node 水位（目标 node free 显著低于
  其他 node 佐证）+ 系统侧 numastat 的 `other_node` 比例升高佐证
- **反例**：
  - 单 node 机器或 mems_allowed 限制为单 node：所有 rank 必然堆一处，
    这是 C3 的范畴，不是 C4
  - file 页重复计数（见 §0）：必须用 anon 判，不用 total 判
- **处置路由**：per-rank membind / interleave → `cpu-affinity-optimization`；
  容量不足 → `hardware-upgrade-analysis` / 减小驻留（应用配置）

#### C5 numa_balancing 干扰

- **定义**：`kernel.numa_balancing=1` 时内核周期性扫描并迁移页，静态快照
  的分布是"瞬时状态"，且会破坏 membind 意图
- **机器 hint**：无（脚本记录开关状态进 JSON，由判读者引用）
- **判读规则**：
  - 已做 membind/绑内存的进程 + numa_balancing=1 → 每份快照都要注明
    "可能被自动迁移污染"
  - 需要确定性结论时：先 `sysctl kernel.numa_balancing=0`（os-optimization
    的范畴）再采
- **处置路由**：`os-optimization`

#### C6 覆盖率（观测自检）

- **定义与阈值**：见 §0 覆盖率校验门；hint `C6_LOW_COVERAGE`（<0.9）、
  `C6_OVERCOUNT`（>1.05，页大小换算错误嫌疑）
- **处置路由**：先修观测（大页识别、权限、swap 排查），结论全部挂起

---

### 阶段 3：结论输出与路由

结论输出为 markdown 分析（无 HTML），每 rank 一节：

```
rank N (pid X, LOCAL_RANK m)
  anon 驻留: node0 12.3GB(82%) / node1 2.7GB(18%)   [覆盖率 99.2% ✓]
  top VMA: [heap] 8.1GB(n0 90%) / weight.safetensors 2.4GB(file,n0) / ...
  判定: C2_NPU_MISALIGN 成立 —— 证据：anon 主 node0 ∉ NPU node{2}；
        通信缓冲(shm VMA)驻留 node0，与设备 node2 跨域
  处置建议: 路由 cpu-affinity-optimization（per-rank numactl --membind=2 重启）；
        建议先关闭 numa_balancing 再验证
```

路由总表：

| 发现 | 动作归属 |
|------|---------|
| C1/C2/C4 需要绑核/绑内存 | `cpu-affinity-optimization` |
| C3 容器/系统 cpuset | `os-optimization` / 平台配置 |
| C5 numa_balancing/THP | `os-optimization` |
| 通信路径深查 | `kptools-ascend-hccn`、`kptools-ascend-profiling-analysis` |
| 线程侧落点复核 | `kptools-thread-affinity-collector`（本 skill 只管内存侧） |

## 2. 场景走查

### 场景 A：单机 8 卡训练某 rank 掉队

稳态快照发现 rank3 的 anon 82% 驻留 node1，其余 rank 全部主驻留 node0
（NPU 也在 node0）。→ C1（线程落点 node0，内存跨域）+ C2（设备 node0）。
top VMA 归因：rank3 的 [heap] 几乎全在 node1——排查启动脚本发现 rank3
曾被 `numactl --membind=1` 误启动。处置：去掉 membind 重启，或统一
per-rank 绑定；验证方式：重采快照对比 + thread-affinity 联合表。

### 场景 B：容器训练 allreduce 带宽抖动

C3 成立：容器 `Mems_allowed_list: 0` 而 `Cpus_allowed_list: 32-63`
（node1）→ 强制远程，任何应用层绑核无解。处置：改容器 cpuset
（mems+cpus 同 node）或放宽 mems；改完重采验证 C3 消失、C1 不再触发。

### 场景 C：多 rank 同机 node0 挤兑

C4：8 rank anon 全堆 node0（各自 20GB+），node0 free 见底而 node1 空闲
过半；系统 numastat `other_node` 占比高。处置：按 rank 分片 membind
（rank0-3→node0，rank4-7→node1，与各自 NPU/CPU 对齐）或 interleave
缓解带宽争抢；注意 file 页（共享权重）不计入挤兑判断。

## 3. 与 kptools-thread-affinity-collector 联合分析

`--affinity-dir` 读入其输出目录后生成联合表：

```
rank | anon 主node | 线程主落点node(busy加权) | NPU nodes | 联合判定
 0   | 0 (82%)     | 0                        | 2         | C1✗ C2✗（内存/线程/node0，设备 node2）
 1   | 2 (91%)     | 2                        | 2         | 干净
```

- 线程主落点来自其 summary.json 的 profiles（busy 线程的 active_psr_top
  加权到 node）；按 PID 对齐，对不上时降级为配置面判定（Cpus_allowed）
- 判据优先级：**实际落点 > 允许掩码**——掩码允许不代表线程真的在那跑
  （thread-affinity 的核心发现）
- 两份观测的时间窗应重叠，否则联合结论要注明时差

## 4. 快照时机速查

| 时点 | 价值 | 注意 |
|------|------|------|
| warmup 后稳态 | 主用例：驻留分布稳定可判 | 确认训练已过 warmup（loss 曲线进入平台） |
| 启动后立即 | 看 H2D 阶段缓冲去向 | 瞬态分布，只作 C2 佐证 |
| 训练中期二采 | --diff 看增量方向 | PID 未变才可比；变了按 rank 对齐并注明 |
| OOM/掉队现场 | 看哪个 rank 在哪个 node 涨 | 配合 node 水位看挤兑 |
