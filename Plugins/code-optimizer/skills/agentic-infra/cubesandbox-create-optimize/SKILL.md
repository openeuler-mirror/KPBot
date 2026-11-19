---
name: cubesandbox-create-optimize
description: 编排 CubeSandbox 完整创建路径与多机制组合调优，核对 Guest 预连接、Master 解析缓存与异步 spec、rootfs 影子预派生池之间的交互，并做组合消融。适用于明确要求端到端归因、多个阶段联调或组合复现；仅诊断一个阶段或单一症状时不选择本技能。
---

# CubeSandbox 创建性能优化与组合验收

本技能独立包含三个方案的原理、实现映射、参考补丁、操作手册和组合验收流程，不需要加载其他 skill。所有百分比指**创建总时间下降率**：`100 × (T_base − T_candidate) / T_base`；不等于吞吐提升率。100 并发创建 100 个沙箱的 `total_time` 是整批墙钟时间，不能用请求平均值或阶段耗时相加替代。

## 输入与路由

从请求和工作区获取源码、目标机、模板、负载、已有 patch/结果及 `run_dir`。本入口处理完整路径归因、多个阶段联调和组合验收；下表用于在这项任务中选择候选机制，单项实验用于归因组合收益。仅要求处理预连接、缓存或存储池一个已定位问题时，不扩大为本组合流程。

先识别目标源码已有实现，再选择每个机制的一个最终版本。相同文件名或相同 diff 前像不代表补丁内容一致；同一路径的历史实现与修订实现是互斥变体，不能先后叠加。记录本包 commit 和选用补丁的 SHA256，保留已有生命周期修复；若已实现同等语义，记录 `already_present`。

| 证据 | 本包内实施资料 |
|---|---|
| restore 后连接等待、重复 monitor 建连或 runtime 构造 | [Guest 原理](references/agent-preconnect.md)、[Guest 操作](references/agent-runbook.md)、[补丁](references/scheme1-rpc.patch) |
| 同模板重复 SELECT、响应前 spec UPSERT | [Master 原理](references/master-create-path.md)、[Master 操作](references/master-runbook.md)、[补丁](references/scheme2-rsi-cache.patch) |
| 前台 rootfs 派生、共享 origin fsync/锁争用 | [存储池原理](references/rootfs-shadow-pool.md)、[存储池操作](references/pool-runbook.md)、[补丁](references/scheme3-pool.patch) |

## 执行步骤

1. **冻结实验**：读 [复现协议](references/reproduction.md)，记录环境、源码、构建、运行产物、bench 版本、模板和准确计时字段。输出 manifest，确认成功响应和持久化契约。
2. **基线归因**：按本包 [组合操作手册](references/workflow.md) 关联 API/Master/Cubelet/Shim 日志，使用本包采样协议和脚本。c1 健康检查与 c100 创建分开，不自动用吞吐场景替换用户目标。
3. **专项实施**：读取上表本地原理和操作章节，分别保留最小改动、测试和开关。参考 patch 是迁移依据，先 `git apply --check`，并审查版本边界。遇到同机制的不同版本时按语义合成一个最终 diff，不重复应用，也不以旧副本覆盖修订实现。
4. **单项与组合**：至少比较 base、1、2、3、123；要归因组合交互时增加 12、13、23。方案二内部还需 cache-only 与 async-only。每一条比较独立做交错配对，不把一批早期 base 重用于不同日期的候选。
5. **验收**：正确性先于性能；记录失败请求、后台欠账和池水位。使用本包脚本分析同负载、同受控环境下的 ABBA 原始报告。组合百分比独立测量，不相加单项收益，不因局部快或文件无冲突就认定可组合。
6. **交付**：写 `run_dir/REPORT.md`，包含补丁/哈希、原理、契约变化、复现命令、正确性、每臂原始结果、置信区间、消融和回退步骤。区分目标实测、历史报告和未验证假设。

## 结果分析

结果分析脚本仅用 Python 3.9+ 标准库，只读报告；下面的 tier-B 用例会编译并执行补丁派生代码。所有入口均不部署或清理沙箱实例：

```bash
python3 scripts/validate_package.py --static-only
python3 scripts/validate_package.py
python3 scripts/compare_create_time.py --manifest /path/to/run/comparison.json --output /path/to/run/verdict.json
python3 cases/comparison-contract/test_compare.py
python3 cases/preconnect-lifecycle/run.py
python3 cases/replica-schedulability/run.py
python3 cases/spec-write-order/run.py
```

输入与命令见 [复现协议](references/reproduction.md)。配对分析器仅需 Python；预连接生命周期用例还需 Rust/Cargo，Master 缓存与异步写用例还需 Git/Go。用例从本包实际 patch 提取或应用代码后编译执行。构建用例应运行在不含生产凭据的隔离测试环境；环境变量削减和离线依赖设置不等于文件系统或网络沙箱。有效数据下输出 `improved / regressed / inconclusive`；缺少目标机证据时工作流状态为 `awaiting_target_evidence`。分析脚本成功退出只表示分析完成，验收还需读 verdict，且不能替代正确性测试和环境审查。

## 验证范围

本包的 tier A 用例验证分析器；tier B 用例通过可控 VM/传输或 DB fixture 验证预连接生命周期、缓存失效代际及同 ID spec 写入顺序。fixture 不包含完整 CubeSandbox、真实 Guest 或数据库；进程间一致性、停机 drain、掉电恢复和目标机性能仍需集成验证，未执行时明确保留待验证状态。平台 CI 绿色也不能代替这些命令的实际执行日志。

## 分阶段交付

| 阶段 | 必需输入 | 执行动作 | 阶段产物 |
|---|---|---|---|
| 版本识别 | 源码、部署、模板 | 核对三个路径已有实现、工具链与运行哈希 | `inventory.json`、现有改动 diff |
| 场景冻结 | c100/n100、成功响应与持久化要求 | 记录配置、环境、计时字段及阈值 | `comparison.json` 计划、环境快照 |
| 机制实施 | 本地 patch 与原理章节 | 按 Master→存储池→Shim 顺序分开构建验证 | 各方案 patch、构建日志、正确性结果 |
| 组合集成 | 单项已完成、混合构建产物一致 | 核对部署、池就绪与 DB drain；执行 123 | 组合 diff、组件哈希表、运行记录 |
| 结果归因 | 各臂原始 JSON | 独立比较与消融，不混并不同场景 | 各比较 verdict、总报告、回退记录 |

组合的具体状态、2³ 实验矩阵、切换纪律、日志记录和报告模板见 [workflow.md](references/workflow.md)。它是本 skill 的执行手册，不是外部技能的路由入口。
