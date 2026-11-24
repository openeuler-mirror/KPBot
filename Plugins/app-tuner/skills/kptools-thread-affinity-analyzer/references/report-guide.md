# 报告判读指南 — 字段、异常模式与处置路由

## 产物清单

| 文件 | 内容 | 消费方式 |
|---|---|---|
| `绑核分析报告.html` | 数据质量卡 + 异常区（带证据链）+ 线程画像表 + 掩码时间线 + 口径说明 | 人读；"仅看异常线程"过滤 |
| `summary.json` | 结构化结论（见下） | agent 直接消费 |
| `samples.jsonl` | 原始时间序列 | 复现/深查/二次分析 |
| `topology_raw.txt` | 采样时刻拓扑快照 | 解释归属；跨机解析必带 |

## summary.json 字段

```
role_config   source（生效的 roles.json 路径或 fallback）/ rules 条数
target        pid / comm / interval_s / started / end_reason / ticks_total
environment   host / kernel / clk_tck / topology_source / numa_nodes /
              l3_domains / npu_devices[{bdf,node}] / npu_nodes /
              target_cpuset / thread_env_vars
threads       instances（线程实例数）/ max_concurrent（峰值并发）/ busy_threads
findings[]    id / severity / title / detail / threads / evidence
data_quality  data_ticks / absent_ticks / malformed_lines /
              psr_outside_allowed{violations,checked,ratio} /
              numa_mapping{bool} / topology_gap
profiles[]    每线程实例：tid / epoch / comm / role / busy_pct / allowed_masks /
              mask_changes / active_psr_top / nodes_touched /
              migrations_lower_bound / lifetime_seq / flags
```

线程实例说明：Linux 的 tid 会回收复用。聚合时若同一 tid 的 utime 计数回退，
视为新实例（`epoch` 递增），报告里显示为 `tid N·eK`；`lifetime_seq` 给出
该实例在场 tick 区间。

## 异常模式判读

### A1 忙线程未绑核跨 NUMA 漂移（high）
忙% ≥ 50 且允许掩码跨多 NUMA 且活跃样本实际跨多 node。跨 node 迁移破坏
缓存/TLB 局部性，鲲鹏多 node 场景的通用性能问题。证据含 nodes_touched、
掩码、迁移下界。掩码**不可读**（`?`）的跨 node 忙线程不进 A1（无法证实
"未绑核"），单列为 A1u（info）——只报告观测到的跨节点运行。
**处置**：证据交 `cpu-affinity-optimization` 出绑核候选
（注意其 hostbound 警示：算子下发主线程要宽松池，禁止单核独占）。

### A2 NPU 主机线程与设备 NUMA 错位（high）
角色 ∈ host 侧下发/拷贝/通信/释放集合，且忙% ≥ 阈值（默认 50，与 A1 同），
且主要落点 node（**按 NUMA 节点汇总活跃样本后取主要节点**——防止单核
计数压过节点总量）不在任一 NPU 设备（PCI vendor 0x19e5 + class 0x120000
双校验识别）所在 node。H2D/D2H 与控制通路跨 NUMA 会掉带宽。
多 NPU 分布多 node 时，与任一设备同 node 即算对齐。**不可判定即跳过**：sysfs
报 `numa_node=-1`（VM/容器场景实测常见）或非昇腾环境时 A2 不判定，报告 NPU
表显示 unknown——没有设备归属数据时输出"错位"结论属于编造。**处置**：路由
`cpu-affinity-optimization` 的 NPU 设备对齐流程；其判据（NPU Free 高 +
host task-clock 低）可进一步确认 hostbound。

### A3 忙线程挤核（medium）
同一 tick 内 ≥3 个**达到忙阈值的线程实例**报同一核，出现比例 ≥5%。psr
快照口径下这是时间分片共享核的证据，实际争抢只会更重；只按计数会把
"三个各 ~25% 的分时线程"误判成挤核，故必须逐实例核过忙阈值并只标记
参与该次挤核的忙实例。**处置**：核对 OMP_NUM_THREADS 等
线程数配置（路由 `application-config-optimization`）与绑核分组。

### A4 掩码震荡（medium）
单线程掩码变更 ≥3 次。若非应用主动调用 sched_setaffinity，常见于
`OMP_PROC_BIND=spread/close` 的并行区动态绑核（进入并行区收紧、离开放宽）。
**处置**：确认 OMP_PROC_BIND / OMP_PLACES 意图；若震荡非预期，考虑
`OMP_PROC_BIND=false` 对比测试。

### A5 绑核意图未覆盖（medium）
在主线程与目标线程**同时在场的 tick** 比较：主线程掩码已收紧，而部分
线程掩码更宽——典型原因是 HCCL/CANN/OMP 库线程自行重置亲和性，绕过了
`taskset`/容器启动绑核。比较取共同在场区间末端（线程先于主线程收紧
退出时，拿各自"最终掩码"硬比会误报）。**处置**：对清单中的线程
由 `cpu-affinity-optimization` 决定是否显式补绑（注意回退命令）。

### A6 观测完整性（info，>5% 升 high）
psr 越出允许掩码的样本比例。同一 tick 内 psr（stat）与掩码（status）
两个文件先后读取，**掩码恰在两读之间被收紧**（绑定/解绑瞬间）会产生
极少量假阳性（预期 <0.5%）——普通迁移只发生在掩码内部，不会令 psr
越界。比例显著偏高提示采样异常（如掩码解析 bug），应先修观测再信结论。

### A7 线程翻动（info）
线程实例数 ≥3× 峰值并发。短命线程反复创建销毁（thread-per-task 反模式或
池配置问题）；短于采样间隔的线程不可见，实际翻动只会更高。**处置**：
识别创建者（c++:pthread/async、python:ThreadPool 误用等），路由应用配置
或代码优化。

## 阈值依据

| 阈值 | 值 | 依据 |
|---|---|---|
| 忙线程判定 | 50%（--busy 可调） | 高于该值线程的迁移/挤核才有调度侧收益 |
| 活跃样本 | 增量/时窗 ≥ 20% | 过滤瞬时唤醒，保留持续占用 CPU 的样本 |
| 挤核并发 | ≥3 忙实例同核 | 2 个属正常时间分片；3+ 才是配置问题信号 |
| 掩码震荡 | ≥3 次变更 | 1-2 次可能是初始化/setup，≥3 是周期性模式 |
| 线程翻动 | ≥3× 峰值并发 | 对齐 proc-collector 迁移阈值的量级直觉（其真源为 ">100 high / >10 medium"，本表取 medium 档的量级） |

## 常见问题

- **迁移数为 0 但性能抖动**：psr 快照口径下界为 0；用 `-i 0.1` 加密采样或
  换 `perf sched`（后续版本）
- **全部线程掩码都是全量**：进程未绑核；A1 会按漂移判定，这是预期行为
- **容器内运行**：掩码即容器 cpuset 边界；宿主机视角需在宿主机采样
  （`docker top` 换算 PID）
- **跨机解析采样包**：必须带上 topology_raw.txt，否则报告退出码 3
