# 数据契约 — collector 产物与 analyzer 输入/输出

> collector（kptools-thread-affinity-collector）与 analyzer
> （kptools-thread-affinity-analyzer）之间是**文件接口**——两者可分机
> 部署、独立使用。本文明确接口 schema，分析侧对格式的假设在此校验。
> 对应 collector 版本 1.1.0。

## 输入：collector 输出目录

```
<out>/
├── samples.jsonl     # 必需：机器格式时间序列
├── topology_raw.txt  # 必需：采样机拓扑快照（缺失时分析器退出码 3；
│                     #  仅 --allow-sys-fallback 显式指定才用本机 /sys 补采）
└── trace.log         # 可选：人类可读事件日志（分析侧不消费）
```

### samples.jsonl

首行 meta、中间每行一个 tick、run 模式末行 end（JSONL）：

```json
{"meta":{"skill":"...","version":"1.1.0","pid":123,"interval":0.5,
         "clk_tck":100,"host":"...","kernel":"...","started":1795405522.37,
         "proc_root":"/proc","auto":false,"target_comm":"python3.11",
         "starttime":1000}}
{"ts":1795405522.84,"seq":1,"threads":[
  {"tid":123,"comm":"mainThread","state":"S","psr":4,"utime":0,"stime":0,"allowed":"0-127"},
  ...]}
{"ts":...,"seq":2,"absent_proc":1}          ← 目标不可见的 tick（无 threads 键）
{"end":{"ts":...,"reason":"timeout","ticks":600}}   ← run 模式末行
```

| 字段 | 语义 | 解析规则 |
|------|------|---------|
| `meta.starttime` | 目标进程启动时间（stat 第 22 字段） | --tick 会话身份校验用；0 = 采集时未知 |
| `threads[].tid` | 线程 ID | int；utime 计数回退 → 判为新实例（epoch 递增，tid 复用安全） |
| `threads[].comm` | 线程名 | 采集侧已清洗控制字符/引号/反斜杠 |
| `threads[].psr` | 最后运行所在核（processor，原 stat 字段 39） | 快照口径：迁移数是下界估计 |
| `threads[].utime/stime` | CPU 时钟 tick 累计（CLK_TCK 粒度） | 相邻在场 tick 增量/时窗 ≥20% → 活跃样本 |
| `threads[].allowed` | `Cpus_allowed_list`（如 `0-7`，`?` = 读取失败） | parse_cpulist 解析；`?` 样本掩码状态记未知（A1u 口径，不判"未绑核"） |
| `absent_proc` | 目标进程不可见的 tick | 不产生线程样本 |

### topology_raw.txt

`=== <section>` 分节（collector 采样机快照）：

| 分节 | 行格式 | 用途 |
|------|--------|------|
| `host` | **两行**：`uname -s -r` + os-release 的 PRETTY_NAME | 报告 host（优先发行版名）/kernel（从首行提取） |
| `clk_tck` | 单数字 | utime/stime 换算（缺省 100） |
| `node_cpulist` | `<node>|<cpulist>` | cpu→node 映射（A1/A2 判据） |
| `l3_domains` | 每行一个 shared_cpu_list | 环境级 L3 域列表（无按线程的 L3 归属列） |
| `npu_pci` | `<bdf>|<node>`（vendor 0x19e5 + class 0x120000 双校验；node<0 = 未知） | NPU node 集合（A2；-1 → 不可判定跳过） |
| `npu_smi_info_l` | `npu-smi info -l` 原样或 `(absent)` | NPU 拓扑旁证（分析侧当前不消费，保留现场） |
| `target_view` | `cpuset=` / `Cpus_allowed_list:` | 上下文 |
| `target_env` | `<k>=<v>`（线程库变量白名单，路径值已脱敏为 basename） | 绑核相关环境变量 |

跨机分析必须保留 topology_raw.txt；缺失时退出码 3（默认不用分析机
拓扑解释采样机数据——那会伪造归属结论）。

## 输出：分析产物

| 文件 | 内容 |
|------|------|
| `绑核分析报告.html` | 数据质量卡（含拓扑缺口披露）+ 异常发现（证据链）+ 线程画像表 + 掩码时间线 + NPU 归属 + 口径说明；临时文件 + 原子写入 |
| `summary.json` | 结构化结论（字段见 report-guide.md §summary） |

summary.json 关键段：

```
meta/skill + role_config{source,rules}
target      {pid,comm,interval_s,started,end_reason,ticks_total}
environment {clk_tck,topology_source,numa_nodes,l3_domains,
             npu_devices,npu_nodes,target_cpuset,thread_env_vars,...}
threads     {instances,max_concurrent,busy_threads}
findings[]  {id,severity,title,detail,threads,evidence}
data_quality {data_ticks,absent_ticks,malformed_lines,
              psr_outside_allowed{violations,checked,ratio},
              numa_mapping,topology_gap}
profiles[]  每线程实例（tid/epoch/comm/role/busy_pct/allowed_masks/
            mask_changes/active_psr_top/nodes_touched/
            migrations_lower_bound/lifetime_seq/flags）
```

线程实例语义：tid 复用（utime 回退）→ 新实例（epoch 递增）；显示为
`tid N·eK`；`lifetime_seq` 为在场 tick 区间。

## 已知边界

- psr 快照口径：迁移数为下界估计
- 短于采样间隔的线程不可见（A7 翻动指标提示）
- 掩码 `?`（不可读）的线程：绑核状态未知，跨 node 运行单列 A1u
  （info），不参与 A1"未绑核"结论
- 数据质量与观测完整性断言（psr ∉ 允许掩码比例）由分析侧自检并写入
  报告的数据质量区，异常偏高时结论降级
