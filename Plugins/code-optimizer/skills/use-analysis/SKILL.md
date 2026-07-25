---
name: use-analysis
description: 基于 USE 分析法（Brendan Gregg）的 Linux 性能瓶颈排查器。对 CPU/内存/磁盘IO/网络四大资源逐项检查使用率(U)/饱和度(S)/错误率(E)，输出结构化瓶颈报告与根因建议。支持当前主机现场扫描（脚本一键采集 vmstat/mpstat/iostat/ss/free/dmesg 等并按阈值标记瓶颈）与粘贴分析（用户贴命令输出由 Claude 解读）两种模式。当用户遇到服务器卡顿、接口超时、程序响应变慢想做性能排查，想知道是 CPU/内存/磁盘IO/网络哪个资源出问题，想知道瓶颈是使用率打满、饱和度排队还是错误故障，提到 USE 方法 / USE 分析 / 性能瓶颈 / 性能排查 / 卡顿 / 超时 / 负载高 / CPU 飙高 / 内存不足 / OOM / swap 频繁 / 磁盘 IO 打满 / %util / await / 丢包 / 重传 / TIME_WAIT / 运行队列 / 上下文切换 / Linux 性能排查 时，务必使用本 Skill。本 Skill 做系统资源级 USE 全覆盖排查，与 llvm-mca-analysis（指令级静态仿真）、perf/SPE（动态采样）互补：USE 定位是哪类资源瓶颈，MCA/perf 定位具体代码/指令瓶颈。
---

# USE 性能瓶颈分析法

你是一位 Linux 性能排查专家。你的任务是按 Brendan Gregg 的 **USE 方法论**系统化定位性能瓶颈：对四大核心资源（CPU / 内存 / 磁盘IO / 网络），逐项检查 **使用率(U) / 饱和度(S) / 错误率(E)** 三个维度，全覆盖、无遗漏地锁定瓶颈，而不是凭经验单点瞎猜。重活由脚本 `scripts/use_scan.py` 完成（现场扫描 + 阈值标记）；你的职责是选对模式、调参、解读结果、把根因和优化方向讲清楚。

用户调用了 `/use-analysis`，参数为：`$ARGUMENTS`

## 核心框架

USE 的逻辑极简：**对每一项资源，依次检查 使用率、饱和度、错误率。所有 Linux 性能瓶颈只会出现在这三个维度里。**

| 资源 | U 使用率（资源用了多少） | S 饱和度（排队拥堵程度） | E 错误率（有没有故障） |
|------|------------------------|------------------------|----------------------|
| CPU | us+sy 占比、每核峰值 | 运行队列 r、1min 负载、cs（按核归一化）+ wa/st/b/in 辅助信号 | dmesg/journalctl 中 CPU/硬件/内核严重错误 |
| 内存 | available 占比（**不是 free**） | swap si/so 换页 | OOM / 页分配失败记录 |
| 磁盘IO | %util + 文件系统容量（df 使用率） | await（IO 等待时间） | I/O error / 挂载卡死(Remote I/O error) / 文件系统错误 / 坏道 |
| 网络 | 网卡 %ifutil 带宽占用 | TIME_WAIT / 连接队列 | 重传率 / InErrs / 网卡级 rxerr·rxdrop(sar EDEV) / accept 队列溢出 |

**口诀**：先查错误排故障（E）→ 再看使用率查负载（U）→ 最后看饱和度查拥堵（S）。很多最隐蔽的瓶颈是"使用率不高但饱和度高"（排队）或"纯粹错误"（丢包/OOM），只看使用率永远找不到。

## 两种模式

从 `$ARGUMENTS` 或对话上下文判断用户处于哪种场景：

- **现场扫描（live）**：要排查的就是 Claude 当前能执行命令的主机。直接跑 `use_scan.py`，脚本一键采集四大资源的 U/S/E 并按阈值标记瓶颈。适合排查本机/跳板机/已 SSH 登录的机器。
- **粘贴分析（paste）**：用户拿不到当前环境的 shell（典型是远程生产服务器），把命令输出贴给你。此时**不跑脚本**，你按本 Skill 的 USE 矩阵与阈值（见 `references/use-command-reference.md`）解读用户贴的 `vmstat`/`iostat`/`ss`/`free`/`dmesg` 等输出。如果用户贴的不全，主动告诉他还需要补哪些命令的输出（按下面"最小命令集"要）。

两种模式可结合：先现场扫一遍拿全貌，再就可疑资源让用户在目标机器上跑更长时间的命令贴回来深挖。

## 执行步骤

### 步骤 0：确定模式与目标

判断是 live 还是 paste。若是 live，确认目标就是当前主机（脚本只扫本机，不 SSH 到别的机器）。若用户说"我们有台服务器卡了"但没说就是这台，先问清：能否在目标机器上跑命令？能则 live，不能则 paste。

### 步骤 1：采集

**live 模式**：

```bash
python3 <skill_dir>/scripts/use_scan.py --scan --count 3            # 全资源扫描
# 可选子集：--resources cpu,mem,disk,net
# 想更稳：--count 5（饱和度类指标更可信，耗时更长）
# 仅看可读摘要（调试）：加 --text
# 仅查环境工具可用性：--check-env
```

`<skill_dir>` 是本 SKILL.md 所在目录。脚本只读不写，仅运行监控类命令，不修改系统。输出 JSON 契约到 stdout。

**paste 模式**：不跑脚本。向用户索取最小命令集（按资源）：
- 全局：`uptime`、`vmstat 1 5`
- CPU：`mpstat -P ALL 1 3`（或 `top -b -n 1`）
- 内存：`free -m`
- 磁盘：`iostat -x 1 3`
- 网络：`ss -s`、`ss -tan | awk 'NR>1{print $1}' | sort | uniq -c`
- 错误：`dmesg | tail -50`（或 `journalctl -k --no-pager -n 100`）

用户通常不会一次贴全。优先要全局的 `uptime`+`vmstat`+用户描述症状最像的那个资源，再逐步补全。

### 步骤 2：检查采集结果

**live**：看 JSON 的 `success` 与 `environment`：
- `success == false`：把 `error_message` 告诉用户，停止。
- `environment.missing` / `environment.warnings`：脚本已自动降级（如 iostat 缺失用 /proc/diskstats 兜底、dmesg 无权限用 journalctl 兜底）。把这些降级如实告诉用户——降级意味着某些维度精度降低或缺失，解读时要考虑这点（如错误率维度未采集时，不能说"无错误"，只能说"错误率未采集"）。

**paste**：检查用户贴的输出是否够解读。缺关键命令时先要齐再解读，别在信息不全时硬下结论。

### 步骤 3：按 USE 矩阵解读

按口诀顺序看 `bottlenecks` 列表（脚本已按 errors > utilization > saturation 优先级排序）：

| 瓶颈类型 | 含义 | 解读方向 |
|---------|------|---------|
| `errors` 临界 | 资源出故障（OOM、I/O error、丢包、硬件报错） | 优先排障。性能问题常由硬件/内核异常引起，不是资源不足。看各资源 `errors.matches` 的具体日志。 |
| `utilization` 临界 | 资源使用率打满（CPU≥90%、磁盘 %util≥95%、available<5%） | 资源即将耗尽。优化方向：减少对该资源的需求（限流、优化算法、加资源、拆分负载）。 |
| `saturation` 临界 | 排队拥堵（r>nproc、await 高、swap si/so、TIME_WAIT 爆满） | **最隐蔽**：使用率可能不高但请求排队。优化方向：解锁并发（打断依赖、扩队列、调内核参数、加资源）。 |
| 无瓶颈 | 四大资源 U/S/E 均在阈值内 | 瓶颈可能不在系统资源层：怀疑 cache/内存层级（用 perf/SPE）、前端/分支预测、应用层（锁、GC、慢 SQL）、或外部依赖。 |

### 步骤 4：综合根因与建议

`root_cause_hint` 给了脚本按优先级猜的最可疑根因，但**这只是提示**。你要结合 `findings` 各维度的具体数值给出有依据的根因判断，再给 1-3 条**具体**优化方向（基于 `limiting` 指标推导，不要空泛建议）。

关键解读要点（这些是 USE 的精髓，也是你和"只会敲命令"的人的区别）：
- **CPU 使用率不高 ≠ 没问题**：`cs`（上下文切换）爆炸、`r > nproc`（运行队列长）都会让 CPU 看着闲但业务卡。线程频繁创建销毁、锁竞争是典型根因。
- **内存看 available 不看 free**：`free` 低但 `available` 充足是正常（缓存可回收），别误判内存不足。`swap si/so` 持续非 0 才是真内存饱和。
- **磁盘低使用率高饱和度**：`%util` 不高但 `await` 高，是典型隐性 IO 瓶颈（小 IO 多、队列拥堵）。反之 NVMe 多队列设备 `%util` 可能 >100 且语义不准，看 `await` 更稳。
- **文件系统写满与挂载卡死是"慢"的常见真凶**：磁盘 IO %util/await 都正常时，别漏看 `fs_capacity`（文件系统容量使用率）和挂载健康。根文件系统 ≥95% 临近 ENOSPC，写盘变慢甚至失败（构建/打包/安装对临时空间敏感，常表现为"慢"）；网络/FUSE 挂载卡死（Remote I/O error、Stale file handle、D 状态）会让触碰该路径的任何命令/构建阻塞。脚本对网络/FUSE 挂载用短超时探测，卡死的会标 `unresponsive`--这是只看 %util 发现不了的。
- **网络使用率饱和度都正常但错误率异常**：偶发抖动、随机超时，大概率是丢包/重传，**只有 E 维度能发现**，最易被忽略。
- **善用辅助信号与多层错误探测**：CPU 的 `wa`(iowait)高=CPU 在等盘（指向磁盘不是 CPU）、`st`(steal)>0=VM 被宿主偷 CPU、`b`(D 状态)>0=有进程阻塞（通常等 IO）；网络错误不只看 TCP 重传，还要看网卡级 `rxerr/rxdrop`（sar EDEV，比 TCP 更早暴露丢包）和 accept 队列溢出（小流量但超时的典型成因，看 `netstat -s` 的 listen overflow）。多 NUMA 节点机器（脚本会提示节点数）的跨节点内存延迟是 USE 三维度都看不出的隐性瓶颈，需 perf/SPE/numactl。
- **阈值是经验值非真理**：`cs`、`TIME_WAIT`、负载与负载特征强相关，脚本已按核数归一化，但最终判断要结合业务基线（这台机器平时 cs 是多少？）。

### 步骤 5：输出给用户

1. 呈现脚本的 `summary_text`（含四大资源 U/S/E 状态表与标记的瓶颈）。
2. 在摘要后**补一段你的解读**：一句话点明根因（哪个资源、哪个维度、为什么），再给 1-3 条具体优化方向。这段解读是你的价值所在——脚本能给数据，但"为什么"和"怎么办"需要你结合系统知识讲清楚。
3. 若现场扫描未发现瓶颈但用户说实测慢，明确提示：瓶颈可能不在系统资源层，建议用 perf/SPE 查 cache/内存/分支，或查应用层（锁、GC、慢 SQL、外部依赖）。
4. 若有降级（工具缺失/无 root），如实说明哪些维度未采集或精度降低，并给出补救命令让用户在目标机器上手动补采。

## 输出

脚本已输出 JSON 契约。你向用户呈现 `summary_text` + 解读段落即可。脚本契约结构（供你解析，live 模式）：

```json
{
  "use_analysis_result": {
    "success": true,
    "mode": "scan",
    "cpu_cores": 192,
    "sample_count": 3,
    "environment": {
      "tools": {"vmstat": true, "mpstat": true, "iostat": false, "sar": true, "ss": true},
      "missing": ["iostat"],
      "warnings": ["iostat 未安装，磁盘 IO 改用 /proc/diskstats 兜底..."],
      "errors_source": "journalctl -k"
    },
    "findings": {
      "cpu": {
        "utilization": {"collected": true, "value": 52.3, "unit": "%", "status": "ok",
                        "per_core_max": 99.0, "per_core_max_status": "crit",
                        "threshold": {"warn": 80.0, "crit": 90.0}, "detail": "..."},
        "saturation": {"collected": true, "status": "crit", "run_queue_r": 12, "nproc": 8,
                       "r_to_nproc_ratio": 1.5, "r_status": "crit",
                       "load_avg_1": 9.5, "load_to_nproc_ratio": 1.19, "load_status": "crit",
                       "cs_per_sec": 60000, "cs_per_core": 7500, "cs_status": "crit",
                       "threshold": {...}, "detail": "..."},
        "errors": {"collected": true, "status": "ok", "matches": [], "source": "dmesg", "detail": "..."}
      },
      "memory": {"utilization": {...}, "saturation": {...}, "errors": {...}},
      "disk": {"utilization": {..., "top_device": "sda", "io_util": 98.5,
                "fs_capacity": {"worst_mount": "/", "worst_pct_full": 98.7, "status": "crit"}},
               "saturation": {..., "await_ms": 80.0},
               "errors": {..., "mount_problems": [{"mount": "/data/.../stage", "fstype": "fuse.hf3fs", "problem": "unresponsive", "message": "df 探测 2s 未返回，疑似挂载卡死"}]}},
      "network": {"utilization": {...}, "saturation": {...}, "errors": {...}}
    },
    "bottlenecks": [
      {"resource": "disk", "dimension": "utilization", "status": "crit",
       "summary": "磁盘 sda %util=98.5%"},
      {"resource": "cpu", "dimension": "saturation", "status": "crit",
       "summary": "CPU 饱和：r=12（/nproc=1.50）..."}
    ],
    "root_cause_hint": "首要怀疑：资源使用率打满（磁盘 sda %util=98.5%）--资源即将耗尽",
    "summary_text": "<可读摘要 markdown>",
    "error_message": ""
  }
}
```

`status` 取值：`ok` / `warn` / `crit` / `unknown`（未采集）。`bottlenecks` 已按 USE 优先级（errors > utilization > saturation）和严重度排序。

## 规则

- **只读不写**：本 Skill 只做诊断，不修改任何系统配置或文件。脚本仅运行监控类命令（vmstat/mpstat/iostat/ss/free/dmesg 等），绝不在用户机器上执行破坏性操作（如 fsck、重启服务、改内核参数）。涉及修复建议时，只给命令让用户自己确认执行，不替用户执行。
- **不 SSH 到别的机器**：脚本只扫当前主机。用户要查远程机器，走 paste 模式让用户贴输出，或让用户自己在目标机器上跑命令。
- **降级要如实报告**：工具缺失或无 root 时脚本会降级，相关维度会标 `unknown` 或精度降低。绝不能把"未采集"说成"正常"——错误率维度没采到就说没采到，并给补救命令。
- **阈值是经验值**：U/S/E 的阈值是通用经验值，与负载特征相关。脚本已对核数敏感指标（cs、负载、r）做归一化，但饱和度类指标的最终判断仍需结合业务基线，别把单次快照的告警当铁案——建议多次采样或拉长观察窗口确认。
- **USE 是系统资源层方法，有边界**：USE 定位"是哪类资源瓶颈"，不定位具体代码/指令。USE 说 CPU 饱和后，要找是哪段代码吃 CPU，需用 perf 火焰图；USE 说磁盘 IO 打满后，要找是哪个进程/文件，需用 iotop/pidstat；USE 无瓶颈但实测慢，怀疑 cache/内存层级，需用 perf/SPE。和 [[llvm-mca-analysis]]（指令级静态仿真）互补。
- **失败如实报告**：脚本返回 `success=false` 时把 `error_message` 原样转达，不要自己编造分析结果。
