# 前置依赖说明 / Prerequisites

## 目标

本说明统一列出 `server-application-optimization` 及其子 skill 依赖的工具、用途、是否必需，以及缺失时的标准降级策略。

核心原则：

- 不静默失败
- 不假装已经检查过缺失能力
- 缺失必需依赖时阻断对应子流程
- 缺失可选依赖时继续执行，但必须降级并降低结论置信度

## 依赖分级

### 1. 必需依赖

缺失后：

- 阻断对应子流程
- 输出 `blocked_by_missing_dependency`
- 明确提示用户需要安装什么

### 2. 推荐依赖

缺失后：

- 允许继续执行
- 输出 `fallback_notes`
- 显式说明哪些分析项被跳过
- 降低 `confidence`

## 主流程常见依赖

| 工具 | 用途 | 分级 | 缺失时处理 |
| --- | --- | --- | --- |
| `uname` | 基本系统识别 | 必需 | 阻断环境识别 |
| `ps` | 进程基础信息 | 推荐 | 降级进程归因能力 |
| `lscpu` | CPU 拓扑与架构 | 推荐 | 降级 NUMA / 架构判断 |
| `free` | 内存压力代理指标 | 推荐 | 降级内存判断 |
| `lspci` | PCIe、网卡、磁盘控制器、GPU/NPU 识别 | 推荐 | 降级硬件规格和加速卡识别 |
| `dmidecode` | BIOS、内存插槽、平台信息 | 推荐 | 降级 BIOS 与硬件容量判断 |
| `perf` | PMU、热点、topdown、火焰图和硬件事件采集 | 推荐/按采集阶段必需 | 环境诊断中标记 perf/PMU 能力降级或阻断 |
| root 或 `CAP_PERFMON`/`CAP_SYS_ADMIN` | 非 root perf 硬件事件、内核符号、容器内采集 | 推荐/按采集阶段必需 | 提前告知权限不足，建议宿主机采集或补 capability |
| PMU 虚拟化/容器映射 | VM/容器内硬件事件采集 | 推荐/按采集阶段必需 | 只能采软件事件或无法采集，topdown/cache/cycles 结论降级 |

## 子 Skill 依赖矩阵

### `database-workload-analysis`（由 `application-config-optimization` 按需引用）

| 工具/输入 | 用途 | 分级 | 缺失时处理 |
| --- | --- | --- | --- |
| `mysql` 客户端或等价输出 | 采集 InnoDB 状态 | 推荐 | 仅基于已有文本证据和规则输出建议 |
| `mysqld_cpu_pct` | AHI 决策输入 | 推荐 | 输出 `need_more_evidence` |
| `threads_per_core` | AHI 决策输入 | 推荐 | 输出 `need_more_evidence` |
| `buffer_pool_hit_rate` | AHI 决策输入 | 推荐 | 输出 `need_more_evidence` |

说明：

- 该专项不单独改写主流程候选列表生成规则
- 对外数据库型工作负载专项归属 `application-config-optimization`
- 缺失数据库专项输入时，应由 `application-config-optimization` 输出保守建议和降级说明

### `io-memory-network-bottleneck-analysis`

| 工具 | 用途 | 分级 | 缺失时处理 |
| --- | --- | --- | --- |
| `mpstat` | CPU 利用率测量（主工具），含 %irq/%soft | 推荐 | 记入 `mpstat_missing`，CPU 利用率判定降级 |
| `pidstat` | 进程级 usr/sys 构成分析（辅助） | 推荐 | 记入 `pidstat_missing`，进程级 CPU 分析降级 |
| `vmstat` | iowait | 推荐 | 记入 `vmstat_missing`，磁盘等待判定降级 |
| `iostat` | 磁盘利用率 | 推荐 | 记入 `iostat_missing`，磁盘带宽判定降级 |
| `ss` | 网络层基础线索 | 推荐 | 记入 `ss_missing`，网络判断降级 |
| `free` | 内存压力代理指标 | 推荐 | 记入 `free_missing`，内存判断降级 |

### `compiler-optimization`

| 工具/输入 | 用途 | 分级 | 缺失时处理 |
| --- | --- | --- | --- |
| `gcc` | 编译器版本识别 | 推荐 | 不输出版本感知建议 |
| `perf` | AutoFDO 证据路径 | 推荐 | 跳过 AutoFDO 路径，保守建议 LTO 或无 profile 路径 |
| AutoFDO 工具链 | `create_gcov` / `autofdo` | 推荐 | 不输出可执行 AutoFDO 步骤，只保留建议说明 |

### `cpu-affinity-optimization`

| 工具/输入 | 用途 | 分级 | 缺失时处理 |
| --- | --- | --- | --- |
| `ps` | 线程和 CPU 视图 | 推荐 | 降级线程分布与热点线程分析 |
| `taskset` | 亲和性读取与脚本生成参考 | 推荐 | 降级当前绑核状态识别 |
| `lscpu` | CPU 拓扑与 SMT 信息 | 推荐 | 降级拓扑与物理核映射分析 |
| `numactl` | NUMA 拓扑与亲和性 | 推荐 | 降级 NUMA 分析 |
| `perf` | 线程迁移监控 | 推荐 | 跳过迁移分析，仅保留静态分布判断 |
| `ref-skills/cpu-affinity-optimization` | 仓库内置 CPU 亲和性专项 skill | 推荐 | 回退到内部轻量规则路径 |

### `network-optimization`

| 工具/输入 | 用途 | 分级 | 缺失时处理 |
| --- | --- | --- | --- |
| `ip` | 网络接口发现 | 推荐 | 降级接口识别能力 |
| `sar` | 活跃接口与利用率观察 | 推荐 | 降级流量活跃度判断 |
| `netstat` | 丢包与错误统计 | 推荐 | 降级丢包分析 |
| `ethtool` | 队列与 NIC 统计 | 推荐 | 降级队列平衡分析 |
| `irqtop` | IRQ 负载热点分析 | 推荐 | 跳过中断负载热点识别 |
| 根目录 `ref-skills/network-io-performance` | 仓库内置网络专项 skill | 推荐 | 继续检查兼容 fallback 或回退内部路径 |
| 外部 `network-io-performance` 路径 | 兼容 fallback 网络专项 skill | 推荐 | 回退到内部通用网络路径 |

### `os-optimization`

| 工具/输入 | 用途 | 分级 | 缺失时处理 |
| --- | --- | --- | --- |
| `sysctl` | Kernel 参数读取和建议生成 | 推荐 | 仅输出人工检查项 |
| `systemctl` | irqbalance、tuned、cpupower 等服务状态 | 推荐 | 降级服务状态判断 |
| `cat /sys/kernel/mm/transparent_hugepage/*` | THP 状态 | 推荐 | THP 建议降级为人工核验 |
| `cpupower` 或 governor sysfs | CPU governor 状态 | 推荐 | 降级功耗策略判断 |
| `numastat` | NUMA 自动平衡与远程访问证据 | 推荐 | NUMA 相关 OS 建议降级 |

### `bios-optimization`

| 工具/输入 | 用途 | 分级 | 缺失时处理 |
| --- | --- | --- | --- |
| `dmidecode` | BIOS 版本、平台、内存通道信息 | 推荐 | 降低 BIOS 建议置信度 |
| Redfish/BMC 只读导出 | BIOS 配置读取 | 推荐 | 只输出人工检查矩阵 |
| 厂商平台文档 | BIOS 参数语义确认 | 推荐 | 禁止输出高风险自动变更 |

### `accelerator-optimization`

| 工具/输入 | 用途 | 分级 | 缺失时处理 |
| --- | --- | --- | --- |
| `nvidia-smi` / `rocm-smi` / 厂商 NPU 工具 | GPU/NPU 利用率和错误状态 | 推荐 | 标记 `accelerator_tool_missing` |
| `lspci` | 识别加速卡和 PCIe 链路 | 推荐 | 降级设备存在性判断 |
| 应用 batch、队列、拷贝路径证据 | 加速卡调度分析 | 推荐 | 只输出需要补采的证据 |

### `hardware-upgrade-analysis`

| 工具/输入 | 用途 | 分级 | 缺失时处理 |
| --- | --- | --- | --- |
| 当前硬件规格 | 判断 CPU、内存、磁盘、网络、GPU/NPU 容量上限 | 推荐 | 输出 `need_hardware_inventory` |
| 业务目标指标 | 判断是否需要更高规格硬件 | 推荐 | 只输出容量风险，不输出采购建议 |
| 历史案例或目标规格 | 做升级收益推断 | 推荐 | 标记推断置信度为 low |

### `other-optimization`

| 工具/输入 | 用途 | 分级 | 缺失时处理 |
| --- | --- | --- | --- |
| 未归类瓶颈证据 | 记录人工专项分析方向 | 推荐 | 输出 `need_more_evidence` |
| 人工约束和不可变更项 | 避免生成不可执行建议 | 推荐 | 将建议限制为 analysis_only |

### `performance-library-selection`

| 工具/输入 | 用途 | 分级 | 缺失时处理 |
| --- | --- | --- | --- |
| `perf` | 外部热点采样 | 推荐 | 外部库替换路径降级 |
| `lsof` | 库依赖识别 | 推荐 | 仅保留有限静态判断 |
| `readelf` | 库/符号补充识别 | 推荐 | 降低识别覆盖率 |
| `ref-skills/library-replacement` | 仓库内置 ARM 库替换专项 skill | 推荐 | 继续检查兼容 fallback 或回退内部路径 |
| `optimization_kb.json` | `library-replacement` 知识库 | 必需 | 外部路径阻断，回退内部路径 |

## 标准降级策略

### 必需依赖缺失

- 停止该子流程
- 输出：
  - 缺失依赖名
  - 受影响能力
  - 推荐安装方式或下一步

### 推荐依赖缺失

- 继续执行
- 输出：
  - `fallback_notes`
  - `confidence=low` 或降低后的置信度
  - 被跳过的能力说明

## 用户感知要求

用户必须能在两个地方看到依赖问题：

### 1. 执行过程中

需要显式提示：

- 缺了什么
- 因此少做了什么
- 当前结果可信度是否下降

### 2. 最终报告中

报告必须包含：

- 依赖状态总览
- 缺失工具列表
- 受影响分析项
- 当前是否已采用降级路径
- 当前使用的是 `repo_local_subskill` 还是 `external_fallback`
