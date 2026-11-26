---
name: kptools-train-mem-affinity
description: >
  训练场景的内存亲和性分析：对 torchrun/mpirun/HCCL world 的每个 rank
  进程聚合 /proc numa_maps，输出"每个 rank 的驻留内存分布在哪些 NUMA
  node"并按方法论判据给出亲和性分析。当用户提到以下场景时触发：
  训练内存亲和性分析、内存亲和性、内存本地性/locality、每个 rank 内存
  分布在哪个 node、内存绑在哪个 node、验证 numactl --membind/绑内存
  是否生效、内存跨 NUMA/跨 node 远程访存排查、训练内存热点挤兑、HCCL
  通信缓冲与 NPU 所在 node 对齐、容器 mems_allowed 限制、numa_maps
  解读、内存被 numa_balancing 自动迁移。单进程原始 numa_maps 快照由
  kptools-proc-collector --numa 覆盖；NPU 设备侧 HBM/显存占用分析由
  kptools-ascend-smi 覆盖（本 skill 只管主机侧内存，不管设备显存）；
  绑内存动作由 cpu-affinity-optimization 执行；线程侧落点由
  kptools-thread-affinity-collector 覆盖（MR !247 评审中）；非训练进程（environ
  无 RANK 类变量）不在自动发现范围。
license: MulanPSL-2.0
compatibility: Linux（openEuler 验证基线）；/proc 与 /sys 只读，同 uid 免 root；python3 >= 3.6 纯 stdlib；非训练进程（environ 无 RANK 类变量）不在自动发现范围
metadata:
  skill_type: tool_assisted
  category: profiling
  domain: train_mem_affinity
  version: "1.1.1"
---

# kptools-train-mem-affinity — 训练场景内存亲和性分析

每个 rank 的内存"绑"在哪个 node，决定它的访存走本地还是跨 NUMA。本 skill
对训练进程组（torchrun/mpirun/HCCL world）做**快照式**采集：聚合每个 rank
的 numa_maps，输出 node×{anon,file} 驻留分布（anon/file 分列、大页换算、
top VMA 归因、覆盖率校验门）与亲和性判据粗筛（C1 内存-CPU 跨域 / C2
内存-NPU 错位 / C3 cpuset mems 强制分离 / C4 跨 rank 热点 / C6 覆盖率门），
支持双快照增量对比与线程落点联合判读，**最终结论按方法论
（references/methodology.md）由人/agent 判定**，不做 HTML 报告。
脚本参数（--diff/--affinity-dir/--top-vmas 等）见"参数速查"。

## 口径声明（先读）

- 观测的是**驻留内存（RSS）的 node 分布**，不是 malloc"申请"；"申请侧"
  的间接证据 = `Mems_allowed_list` + 驻留分布
- **anon/file 必须分列**：anon（heap/张量/缓冲）是私有分配，判据以它为准；
  file 页（权重/共享库）物理共享、跨 rank 重复计数，只做归因参考
- numa_maps 读取对大模型进程耗时数秒——**只做快照不做轮询**；每份快照带
  时间戳，对比用 `--diff`

## 工作流程

1. 环境自检：`bash scripts/discover.sh`（node 拓扑/水位、numa_balancing、THP）
2. 快照：`python3 scripts/collect_rank_mem.py --top-vmas 8`（自动扫 environ
   发现全部 rank；建议在 warmup 后稳态采）
3. 判读：按 `references/methodology.md` 判据矩阵 C1-C6 核对证据与反例，
   产出 markdown 结论（每 rank 一节：分布 + 判定 + 证据行 + 处置路由）
4. 需要线程侧对照时同期跑 `kptools-thread-affinity-collector`，`--affinity-dir` 联合判读

> 只读观测：不修改任何内存策略/绑核；采集本身只读 /proc 与 /sys。

## 采集命令

```bash
# 全量 rank 快照（自动发现 torchrun/mpirun 的 rank 进程），带 top VMA 归因
python3 scripts/collect_rank_mem.py --top-vmas 8 --out ./train_mem_affinity_20261121

# 只看指定 rank 进程（某个 rank 掉队时）
python3 scripts/collect_rank_mem.py --pid $(pgrep -f train.py | head -1)

# 训练中期二采，与上一份对比增量（哪个 rank 在哪个 node 涨）
python3 scripts/collect_rank_mem.py --out ./train_mem_affinity_2 --diff ./train_mem_affinity_1/train_mem_affinity.json

# 与 kptools-thread-affinity-collector 联合（内存 node × 线程落点 node × NPU node 同表）
python3 scripts/collect_rank_mem.py --affinity-dir ./af_20261121 --out ./train_mem_affinity_1
```

输出目录里 `train_mem_affinity.json` 为机器可读快照（含系统侧 node 水位、
numastat、numa_balancing、THP），可留档供 --diff 与复查。

## 判据速查（详见 methodology.md，含阈值/反例/路由）

| ID | 判据 | 机器 hint（粗筛） |
|----|------|-------------------|
| C1 | anon 主驻留 node 与 CPU 掩码/线程落点跨域（远程访存） | C1_REMOTE_SUSPECT |
| C2 | anon 主驻留 node 不在任一 NPU 设备 node（Ascend） | C2_NPU_MISALIGN（numa_node=-1 不判） |
| C3 | Mems_allowed 与 Cpus_allowed 的 node 无交集（强制分离） | C3_FORCE_REMOTE |
| C4 | ≥2 rank 的 anon 堆同一 node 且存在空闲 node（热点） | C4_NODE_HOTSPOT |
| C5 | numa_balancing=1 污染静态快照与 membind 意图 | （记录状态，判读打折） |
| C6 | 覆盖率校验门：聚合驻留/VmRSS ∉ [0.9,1.05] | C6_LOW_COVERAGE / C6_OVERCOUNT |

hints 只是粗筛：每条判据的**证据要求与"不应下结论"的反例**在方法论中
逐条列出（如 numa_balancing=1 时 C1 需二采看趋势、file 页共享使 C4 必须
用 anon 判），核对后才允许写入结论。

## 输出契约

- stdout：markdown —— rank 发现行（发现数/LOCAL_WORLD_SIZE 本机口径校验）+ 每 rank 的
  node 分布表（MB/%，anon/file 分列）+ 覆盖率行 + top VMA 归因表 +
  系统侧 node 水位；`--diff` 追加增量表；`--affinity-dir` 追加联合表
- `<out>/train_mem_affinity.json`：完整快照（meta/system/ranks[]，含 hints）
- stderr：诊断（environ 不可读的 PID、rank 数与 WORLD_SIZE 不符、权限降级）
- 退出码：`0` 成功（含 hints）；`1` 参数错误；`2` /proc 不可读；
  `3` 未发现任何 rank 进程；`4` numa_maps 全部不可读

## 参数速查

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--out <dir>` | 输出目录 | `train_mem_affinity_<UTC时间>` |
| `--pid <PID[,PID]>` | 指定 rank 进程（跳过全扫） | - |
| `--top-vmas <N>` | 每 rank 打印前 N 大 VMA 归因 | 0（不打） |
| `--diff <old.json>` | 与旧快照对比增量（按 rank 号对齐；同 rank 多
  PID 已按主进程去重，PID 变化即真重启） | - |
| `--affinity-dir <dir>` | 联合 thread-affinity 采集输出目录
  （MR !247 评审中；数据契约见 references/affinity-trace-contract.md，
  缺文件时 WARN 降级不静默） | - |
| `--rank-env <KEY>` | 追加自定义 rank 环境变量名 | RANK 等（见 guide） |
| `--proc-root` / `--sys-root` | procfs/sysfs 根（测试注入） | /proc、/sys |

## 约束

- 只读：不改内存策略、不绑核、不写目标进程任何状态
- rank 发现依赖 environ 可读（同 uid；跨 uid 需 root）；读不到的进程
  列入 stderr 警告；**发现数校验用本机口径**——有 `LOCAL_WORLD_SIZE`
  （torchrun/openmpi/slurm 族自带）时按本机期望数校验（双向：少=有
  rank 没发现，多=环境异常重复声明）；无本机字段（pmi 族/老启动器）
  时按 WORLD_SIZE 校验并注明"跨主机总数，多机训练本机发现部分 rank
  属正常"
- rank 发现按**作业分组**（TORCHELASTIC_RUN_ID，缺失时按进程树根）：
  本机并行多个训练作业（各自都有 rank 0）时报 `MULTI_JOB` 退出码 1
  并列出各作业 pids，用 `--pid` 指定目标——不会把两个作业拼成一份报告
- 同一 rank 多 PID（DataLoader worker 继承 RANK/LOCAL_RANK/WORLD_SIZE
  属常态）：取 PID 最小者为主条目，其余记入 `duplicate_pids` 不参与
  校验门/C4/diff 对齐（**--pid 手工指定同样适用该不变量**）
- numa_maps 快照耗时与 VMA 数成正比（大模型进程数秒），多 rank 顺序采集，
  各 rank 快照存在秒级时差——--diff 与联合判读时注意
- 单 NUMA node 机器上 C1/C2/C4 无跨域可言，脚本照常输出分布但相关判据
  自然不触发
- NPU 设备按 PCI vendor `0x19e5` + **class 0x120000（Processing
  accelerators）/ Ascend device id（d801/d802）双校验**识别——华为全家桶
  （hns3/Hi1822 网卡、iBMA 等）同为 0x19e5，单信 vendor 会误入 NPU 集合
  令 C2 双向失真；`numa_node=-1`（VM/容器 sysfs 未暴露归属，实测常见）时
  该设备不参与判定；无可信 NPU 信号时 C2 输出 unknown 而非猜测
- 联合表（--affinity-dir）的 NPU 列与 C2 判定**优先用本地双校验集合**
  （thread-affinity 采集侧的 npu_pci 为 vendor-only 全列，可能含网卡；
  本地无 NPU 集合时才沿用快照集合并 WARN 标注未双校验）

### 和同类 skill 的分界

- 单进程原始 numa_maps/numastat 快照由 `kptools-proc-collector --numa`
  覆盖；本 skill 在其上做 **rank 级聚合 + anon/file 分列 + 方法论判读**
- 绑核/绑内存动作与策略由 `cpu-affinity-optimization` 覆盖；本 skill 只
  发现证据并路由
- numa_balancing/THP/cpuset 系统配置变更由 `os-optimization` 覆盖
- 线程侧落点观测由 `kptools-thread-affinity-collector` 覆盖（MR !247
  评审中，合入前联合判读按 references/affinity-trace-contract.md 的
  数据契约自行构造采样目录）；本 skill 是内存侧，`--affinity-dir` 是
  两者数据的交联而非职责重叠

## 错误处理

| 现象 | 原因与处理 |
|------|-----------|
| 退出码 3 + `NO_RANK_FOUND` | environ 无 RANK 类变量：确认用 torchrun/mpirun 启动，或 `--pid` 指定；跨 uid 用 root |
| 退出码 1 + `参数错误: --pid 需为数字 PID` | 笔误（如 `--pid 1OO00`）——改正参数，不要去查进程存活 |
| 退出码 3 + `TARGET_MISSING pid=<数字>` | 指定的数字 PID 不存在：确认进程存活/environ 权限 |
| 退出码 1 + `MULTI_JOB` | 本机并行多个训练作业（各有 rank 0）——stderr 列了各作业 pids，用 `--pid` 指定目标 |
| stderr `environ unreadable: pid ...` | 跨 uid 或进程退出中；同 uid/root 重试 |
| stderr `ranks found (N) != LOCAL_WORLD_SIZE (M)` | 本机有 rank 没发现：查进程是否活着、environ 权限；结论注明缺口 |
| stderr `ranks found (N) != WORLD_SIZE (M)（跨主机总数...）` | 无本机期望字段（pmi 族）：多机训练本机只发现部分 rank 属正常，单机训练则提示缺口 |
| coverage 显著 < 0.9 | swap/权限/THP 统计竞态；按 C6 挂起结论先修观测 |
| coverage > 1.05 | 大页换算错误嫌疑（kernelpagesize_kB 未识别）——提 bug，结论作废 |
| 采集耗时 > 10s | 正常（几十万 VMA）；不要并发多进程读同目标 |
| `--diff` 后 PID 变化 | 进程重启过，按 rank 对齐仍可比，结论注明"跨重启" |

## 参考资料

- [分析方法论](references/methodology.md) — 判据矩阵 C1-C6（证据/阈值/反例/路由）、场景走查、快照时机
- [numa_maps 字段手册](references/numa_maps-guide.md) — 行格式、大页换算、anon/file 口径坑、rank 环境变量表
