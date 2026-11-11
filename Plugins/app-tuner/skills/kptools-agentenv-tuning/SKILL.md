---
name: kptools-agentenv-tuning
description: AgentENV 沙箱的 openEuler/ARM 内核适配与性能调优入口。当用户提到 AgentENV、ublk 功能使能、沙箱并发恢复、ARM Pvspinlock、CONFIG_HZ、DVMBM/TLBI 广播范围或 UnixBench 并发线性度时使用。收集 Host/Guest 环境证据，按问题选择已安装的专项 Skill，在项目实际负载下验证。
---

# AgentENV 内核适配与性能调优

本技能包属于 app-tuner，集中维护 AgentENV 场景知识。入口和已安装的专项 Skill
均可独立使用；已有环境证据、授权和测试用例可以直接继承。

通用实施与对照流程见 `references/optimization-process.md`，结果契约见
`references/validation.md`。资源规格、负载、并发、预热和采样参数均按当前项目确定。

## 输入

优先从用户描述、部署文档和现场只读检查中取得：

- 节点角色：Host、Guest、容器及它们之间的对应关系；WebIDE 所在环境不一定是 Host。
- 实际 OS、运行内核、CPU 架构与拓扑、AgentENV 版本及部署位置。
- 目标实例、vCPU/内存规格、并发数、后台负载和沙箱创建/恢复测试命令。
- 目标指标及方向，例如恢复耗时 P95（越低越好）、吞吐量（越高越好）。
- 可用内核源码、配置、补丁及 Hypervisor 实现。
- 已授权的操作范围，包括构建、部署、重启和远程执行。

缺少补丁或压测用例时继续完成诊断，列出缺失项；不要编造开关、SMC 编号、
重启命令或压测命令。

## 工作流程

### 1. 采集当前环境

读取 `references/validation.md`。在每个可访问的目标节点分别执行：

```bash
bash scripts/collect_environment.sh --output /absolute/path/to/new-evidence --role unknown
# 确认角色和目标进程后，可改用 --role host|guest|container，并增加 --pid PID。
```

路径相对于本 Skill 目录。输出目录必须尚不存在，避免覆盖已有证据。
脚本只读系统状态、写入新证据目录，不安装软件或改变内核参数。

将采集位置、节点角色和目标实例关联起来。`--role` 是调用者声明，
仍须结合虚拟化证据核实；容器内的 root 权限不代表可修改 Host 内核。
脚本退出 0 仅表示 ARM Linux 采集完成，不证明功能存在或性能优化成功。
使用 hostname、boot_id、运行内核及目标 PID 的 stat 关联本轮证据。
目标进程的 status 可读但 exe 不可读时，保留已有 CPU/cgroup 证据，
将实例身份标为待核实；不要换用另一个 PID 或自动提权。
发现旧版 AgentENV/uVM 仓库、二进制或 benchmark 时，核对版本、实际运行者和
权限要求，不自动复用其历史结果或执行包含 sudo 的 runner。

### 2. 检查功能前提

对于使用 ublk 存储路径的 AgentENV，若已安装 `agentenv-ublk-enablement`，
先读取该专项的 SKILL.md。若未安装，说明该专项不可用，
可依据已有的真实功能验证证据继续；缺少必要证据时记录阻塞项。
功能缺失时先修复，再通过真实的设备与沙箱功能检查，之后建立性能基线。
这部分记录为功能使能，不计算性能提升百分比。
运行配置未启用 in-tree ublk，但模块/设备已经存在时，先核对外置或回移驱动的
来源、srcversion、vermagic 和实际加载证据，不直接重编或替换正在使用的驱动。

### 3. 按证据选择专项 Skill

下表是候选路由。先检查 `skills/` 下的实际目录及其 SKILL.md，或检查安装后
以专项名称命名的独立入口；未安装的专项要明确报告为不可用，不能假定文件
存在或声称该专项已执行。

| 当前问题/证据 | 专项 Skill | 首选验证 |
|---|---|---|
| ublk 不可用、内核未启用 ublk | `agentenv-ublk-enablement` | 设备 I/O 和沙箱功能 |
| Guest 锁争用、锁持有者被抢占、vCPU 超分 | `agentenv-pvspinlock` | 同场景启用/禁用对照 |
| 恢复路径锁竞争、调度等待或尾延迟 | `agentenv-kernel-hz` | 当前与候选 HZ 配置对照 |
| DVMBM/TLBI 范围广播、进程创建负载并发扩展问题 | `agentenv-cluster-tlbi` | 核对开关、位图策略与布局 |

读取命中的专项文件后按其流程执行。仅出现关键词时先诊断，不直接实施。
安装器会暴露实际存在的专项 Skill；插件直接加载时可从其实际目录读取。
不要求为完成一次任务而遍历全部专项。

用户要求“每个 subskill 验证/消融”时，对已安装的专项按当前项目逐项建组：

- ublk：功能使能及真实 I/O；关闭后负载不能运行时不计算提速。
- PVspinlock：固定其他因素，在业务相关的资源与超分场景内比较机制开关。
- HZ：固定其余配置，比较当前配置与实际内核支持的候选配置。
- TLBI：核实 DVMBM 实现及策略，分别控制功能开关、CPU 布局和位图更新方式，
  避免将绑定效果计入机制收益；负载准备和线性度口径按已安装的 TLBI 专项文档执行。

单项对照只改变被测优化；全量减一项使用共同业务用例并固定其余已生效设置。
不能合并不同负载的百分比，也不能用其他调参代替被测机制。

### 4. 实施与验证

1. 确认 AgentENV 功能正常，固定规格、负载、并发数和指标口径。
2. 从专项 Skill 输出的候选动作中一次选择一个可独立验证的变量。
3. 在已有授权范围内实施；只有新增操作超出该范围时补充确认。
4. 保存实际实施与回退命令、内核配置差异和补丁标识。
5. 重启后重新识别节点、内核、目标实例和 CPU/cgroup 约束，再进行功能复测和性能对照。
6. 保留有证据支持的改进；出现功能失败或明确性能回退时恢复上一有效配置并复验。

内核与 Hypervisor 变更使用可恢复的测试节点/Guest 和启动项。
conda 克隆、普通容器或仅复制用户态程序不能隔离 Host 内核变更。
已有 `cpu-affinity-optimization` 可提供拓扑分析；TLBI 的实现能力仍由
`agentenv-cluster-tlbi` 单独确认，不能用绑核结果代替。

### 5. 输出

按 `references/validation.md` 的结果契约输出。区分：

- 已完成的只读诊断；
- 已验证的功能使能；
- 当前有实测数据的性能优化；
- 因补丁、权限或负载缺失而阻塞的候选动作。

仅报告当前测量支持的收益或退化，不预设收益幅度。
