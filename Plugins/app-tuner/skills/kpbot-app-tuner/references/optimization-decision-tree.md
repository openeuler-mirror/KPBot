# 优化决策树参考 / Optimization Decision Tree

本文是 `candidate-skill-list.md` 的执行补充，描述候选 skill 进入分析/执行后如何选择动作和变更模式。

## 总原则

- 先判断瓶颈并采集性能信号，再生成候选 skill 列表。
- 先执行低风险、可快速回退的动作。
- 高风险动作必须有用户确认、验证计划和回退计划。
- 单个 skill 需验证完全 subskill 给出的所有推荐。
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

### NPU 推理瓶颈（Ascend/CANN/vLLM）

优先候选：`cpu-affinity-optimization`（强制首位）→ `application-config-optimization`

检查顺序：

1. NPU 利用率（`npu-smi info -t utilization`）
   - util `< 60%` → CPU feed 瓶颈 → `cpu-affinity-optimization` + `application-config-optimization`（OMP/线程数）
   - util `> 90%` → NPU 计算瓶颈 → 算子优化 / `compiler-optimization`
2. HBM 带宽利用率
   - 高 → 模型太大/批量太小 → `application-config-optimization`（batch size）
3. host-device 拷贝
   - 高 → 减少小 tensor 传输 → `application-config-optimization`（合并传输/异步拷贝）
4. 算子 fallback
   - 检测到 fallback 日志 → `compiler-optimization`（CANN 版本升级/自定义算子注册）
5. malloc 热点
   - perf 热点含 `malloc`/`free` → `performance-library-selection`（tcmalloc）
6. KV cache 碎片
   - OOM 或 throughput 抖动 → `application-config-optimization`（`gc_threshold`/`max_split_size`）
7. 编译优化空间
   - `libtorch_cpu.so`/`libtorch_npu.so` 热点 → `compiler-optimization`（PGO/LTO）
8. 若设备规格不足 → `hardware-upgrade-analysis`

实战案例参考：Ascend910 + vLLM qwen2.5-1.5b，S0→C7 累计 +94.5%。优化路径为 C5 绑核 → C8 应用配置 → S5 tcmalloc → C6 Python PGO → C7 全 PGO。CPU 侧 feed 能力释放后，框架 PGO 才能发挥收益；顺序错误会导致 PGO 收益被 CPU 瓶颈掩盖。

### GPU 推理瓶颈（NVIDIA/CUDA）

优先候选：`cpu-affinity-optimization`（强制首位）；GPU/NPU 等计算卡证据命中时追加 `accelerator-optimization`

检查顺序：

1. 设备利用率、显存、带宽、host-device 拷贝（`nvidia-smi dmon`/`dcgmi`）。
2. batch size、并发流（CUDA streams）、算子 fallback。
3. CPU feed 能力是否限制加速器（PyTorch DataLoader、CPU 预处理）。
4. 算子热点 → `compiler-optimization`（CUDA kernel 融合/TensorRT）。
5. malloc 热点 → `performance-library-selection`（tcmalloc/PyTorch caching allocator）。
6. 若设备规格不足 → `hardware-upgrade-analysis`。

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

## AI 推理多轮动态决策

AI 推理场景的瓶颈常在多轮优化后发生转移（如 CPU 瓶颈→NPU 瓶颈→内存带宽瓶颈）。决策树不是一次性选择，而是根据上一轮收益动态调整下一轮动作的循环。

### 决策规则

| 上一轮收益 | 下一轮动作 | 说明 |
|---|---|---|
| `> 10%` | 继续当前分支深挖 | 当前瓶颈类型仍有空间，进入更激进参数或组合优化 |
| `1% - 10%` | 切换下一候选分支 | 边际收益递减，预算转移到更高优先级候选 |
| `< 1%` | 当前分支推荐已验证完毕 | 标记完成，进入下一未完成分支 |
| 瓶颈重分类 | 重新采集证据，重建决策路径 | 上一轮变更后瓶颈类型转移，旧决策树失效 |

### 瓶颈重分类触发条件

- NPU util 从 `< 60%` 升到 `> 90%`：CPU feed 瓶颈已解决，转为 NPU 计算瓶颈，决策路径从 cpu-affinity 分支切换到 compiler-optimization 分支。
- malloc 热点消失但 throughput 未提升：内存分配已优化，瓶颈转移，需重新 perf 采集定位新热点。
- TTFT 下降但 throughput 仍低：首 Token 延迟已优化，转为吞吐量瓶颈，进入 KV cache/batch size 分支。

### 首轮探索→后续细化

- **首轮探索**：横向扫描所有证据命中分支，用保守配置快速建立收益量级排序。
- **后续细化**：对收益 `> 10%` 的分支深挖，逐步收紧参数。深挖期间其他分支暂停，避免收益归因混淆。
- **切换门控**：当前分支推荐已全部验证完毕，或瓶颈已重分类，才从深挖切回扫描。

### Ascend910 + vLLM 案例的决策路径

> **历史参考声明**：以下路径是**历史案例的实际执行记录**，不是预设路径模板。每个新的优化轮次必须由 subagent 基于现场采集证据独立判断优化路径和候选动作。subagent 不得直接照搬此路径作为执行计划。此案例仅用于说明"每轮收益信号驱动下一轮分支选择"的动态决策机制。

```
S0 基线 → 候选 [cpu-affinity, application-config, performance-library, compiler]
  │
  ├─ C5 绑核 → +8.52%（<10%，边际未达深挖线）→ 切换 application-config
  │     └─ NPU util 仍<60%，CPU 仍高 → 应用配置
  ├─ C8 应用配置(OMP/线程) → +35.9%（累计 +47.5%）→ 深挖 application-config
  │     └─ CPU 高缓解，malloc 热点显现 → 切换 performance-library
  ├─ S5 tcmalloc → +9.3%（1-10%）→ 切换 compiler
  ├─ C6 Python PGO → +6.5%（1-10%）→ 切换 compiler 内下一候选
  └─ C7 全 PGO(libtorch_npu.so) → +13.3%（>10%）→ 累计 +94.5%
```

关键洞察：每轮收益信号驱动下一轮分支选择。CPU 侧优化（绑核+应用配置+tcmalloc）必须先于框架编译优化（PGO），否则 PGO 收益被 CPU feed 瓶颈掩盖。

> **subagent 独立判断要求**：当 performance-library-selection subskill 被调度时，subagent 必须独立执行其 SKILL.md 的完整流程（全类别热点采集 → 库类型识别+规则匹配 → 验证流程设计）。历史案例中 S5 只使能了 tcmalloc，但完整的热点采集可能发现 memory_operations 热点（memcpy/memset ≥ 0.5%）从而同时推荐 bisheng-stringlib。subagent 不得以历史案例路径作为跳过采集或缩小推荐范围的理由。
