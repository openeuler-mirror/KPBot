# 优化决策树参考 / Optimization Decision Tree

本文是 `candidate-skill-list.md` 的执行补充，描述候选 skill 进入分析/执行后如何选择动作和变更模式。

## 总原则

- 先判断瓶颈并采集性能信号，再生成候选 skill 列表。
- 先执行低风险、可快速回退的动作。
- 高风险动作必须有用户确认、验证计划和回退计划。
- 单个 skill 连续 5 轮收益均小于 1% 时停止该 skill。
- 未识别瓶颈时停止调参，输出报告和下一步采集建议。

## 瓶颈分支

### 网络瓶颈

优先候选：`network-optimization`

检查顺序：

1. 丢包、重传、socket backlog。
2. 网卡队列、RSS/RPS/RFS/XPS。
3. IRQ 亲和性和软中断 CPU 分布。
4. 防火墙、TCP/sysctl、中断聚合、ring buffer。
5. 若软件优化空间不足，进入 `hardware-upgrade-analysis`。

### 磁盘瓶颈

优先候选：`application-config-optimization` 或 `os-optimization`

检查顺序：

1. iowait、磁盘 util、队列深度。
2. 应用刷盘、日志、缓存、异步/同步策略。
3. 文件系统、I/O scheduler。
4. 若设备规格不足，进入 `hardware-upgrade-analysis`。

### 内存容量瓶颈

优先候选：`application-config-optimization`

检查顺序：

1. RSS、swap、OOM、容器 memory limit。
2. 应用缓存和 buffer pool。
3. OS THP/HugePages。
4. 若容量不足，进入 `hardware-upgrade-analysis`。

### 内存带宽瓶颈

优先候选：`cpu-affinity-optimization`

检查顺序：

1. NUMA 拓扑和跨节点访问。
2. 线程/内存亲和性。
3. 批量、缓存和数据布局。
4. 若带宽规格不足，进入 `hardware-upgrade-analysis`。

### CPU 瓶颈

按证据加入候选：

- 线程偏斜、迁移、NUMA、IRQ 冲突 → `cpu-affinity-optimization`
- 线程、队列、缓存、数据库状态 → `application-config-optimization`
- malloc/memcpy/压缩/加密/CRC 热点 → `performance-library-selection`
- 编译选项、topdown、fallback 热点 → `compiler-optimization`
- governor、THP、numa_balancing、irqbalance、sysctl → `os-optimization`
- Power Profile、SMT、C-State、BIOS NUMA → `bios-optimization`
- 未归类 CPU 热点 → `other-optimization`

### GPU/NPU 瓶颈

优先候选：`accelerator-optimization`

检查顺序：

1. 设备利用率、显存、带宽、host-device 拷贝。
2. batch size、并发流、算子 fallback。
3. CPU feed 能力是否限制加速器。
4. 若设备规格不足，进入 `hardware-upgrade-analysis`。

### 硬件规格限制

优先候选：`hardware-upgrade-analysis`

触发条件：

- 目标硬件某项资源长期接近饱和，软件侧候选收益低。
- 当前规格无法满足目标指标。
- 性能问题主要由核数、内存容量、内存带宽、网卡带宽、磁盘 IOPS、GPU/NPU 规格解释。

## 变更模式分类

| 模式 | 含义 | 示例 |
|---|---|---|
| `analysis_only` | 只分析，不执行 | 缺权限或生产环境 |
| `dry_run` | 输出命令和影响，不执行 | 高风险动作预审 |
| `online` | 在线生效 | sysctl、部分应用参数 |
| `restart_required` | 需要服务重启 | 应用配置、运行库 |
| `system_reboot` | 需要系统重启 | BIOS、SMT |
| `rebuild_required` | 需要重编译或替换二进制 | 编译优化、源码补丁 |
| `hardware_advice` | 只输出硬件建议 | 更高规格硬件 |

## 收益归因

- 默认采用串行叠加收益。
- 每轮记录阶段收益和累计收益。
- 不得把多个并发变更后的混合结果拆分为单项收益。
- query mix 或 workload 变化必须标记为诊断发现，不能包装成服务器配置收益。
