# app-tuner

应用级调优插件 — 服务器应用性能优化的端到端 Agent 工作流。

同时支持 **Claude Code** 和 **OpenCode** 两种 AI 编码工具。

## 概述

app-tuner 提供 1 个主编排 skill(`kpbot-app-tuner`)及其内部资源:

- **主 skill**: `skills/kpbot-app-tuner/` — 编排器,负责环境采集→瓶颈识别→动态路由→迭代验证→报告输出
- **分析型子 skill**(subskills,自研,13 个): BIOS/OS/网络/CPU 亲和性/编译/性能库/应用配置/加速卡/硬件升级/瓶颈预筛/数据库专项/vLLM-Ascend profiling/其他兜底
- **质量保障**: 对抗性审查、current_run 门控、subagent 独立上下文执行
- **AgentENV 专项技能包**: `skills/kptools-agentenv-tuning/` — 独立入口及按需安装的专项 Skill，共享环境采集和验证约定。
- **验证类 skill**: `skills/model-precision-analysis/` — 模型优化后的精度回归验证，输出准确率对比、回归题定位与达标判定。

## 安装

### OpenCode

```bash
# 克隆 KPBot 仓库
git clone https://atomgit.com/openeuler/KPBot.git

# 安装到你的项目
cd /path/to/your-app-project
/path/to/KPBot/install.sh project opencode

# 启动
opencode
```

安装后 `install.sh` 会自动:
1. 将 skill 拷贝到 `.opencode/skills/`
2. 生成 `AGENTS.md` 系统提示词(若仓库根有 CLAUDE.md)

验证安装:

```bash
opencode debug skill | grep app-tuner
```

### Claude Code

```bash
# 安装 marketplace
claude plugins marketplace add https://atomgit.com/openeuler/KPBot.git

# 或本地安装
claude plugins install --plugin-dir /path/to/KPBot/Plugins/app-tuner
```

## 技能组成

### 主 skill(编排器)

| 技能 | 描述 |
|------|------|
| `kpbot-app-tuner` | 服务器应用优化编排器 — 环境采集、瓶颈路由、subagent 迭代验证、报告输出 |
| `kptools-agentenv-tuning` | AgentENV 沙箱适配与调优入口 — Host/Guest 证据、功能前提、专项路由与验证 |

### AgentENV 专项技能包

专项 Skill 集中放在 `skills/kptools-agentenv-tuning/skills/`；安装器会将实际存在的
专项暴露为独立入口。

| 技能 | 描述 |
|------|------|
| `agentenv-pvspinlock` | ARM Guest/Host 半虚拟化锁等待与唤醒优化 |
| `agentenv-kernel-hz` | 并发恢复场景 HZ 调优及调度延迟验证 |
| `agentenv-snapshot-fs` | 快照写入路径与文件系统档位调优（ext4 慢路径识别、XFS 迁移） |
| `agentenv-cluster-tlbi` | cluster 布局与 TLBI 范围控制联合分析 |

安装、ARM 服务器只读采集和验证范围见 [AgentENV 技能包说明](skills/kptools-agentenv-tuning/README.md)。

### 独立调优 skill

| 技能 | 描述 |
|------|------|
| [`vllm-fastokens-segment-cache-migration`](skills/vllm-fastokens-segment-cache-migration/SKILL.md) | 评估 vLLM 切换到官方 fastokens 并启用段缓存的可行性，通过基线组和段缓存开启组验证 token 一致性、并发稳定性、分词性能、内存与回退准备 |

### 验证类 skill

| 技能 | 描述 |
|------|------|
| [`model-precision-analysis`](skills/model-precision-analysis/SKILL.md) | 优化后精度回归验证：读取精度评测工具 `opendatas/output` 下的合并结果，输出准确率对比、回归/改善题号与达标结论，精度不达标即阻止按性能放行 |

### CubeSandbox 时序剖析 Skills

原 `sandbox-timeline-profile` 保留为一键 Workflow、路由和唯一共享实现；一期拆分的四个单点 Skill 可独立调用，也可由 Workflow 依次串联。

| 技能 | 单点职责与产物 |
|------|----------------|
| [`sandbox-profile-evidence-collect`](skills/sandbox-profile-evidence-collect/SKILL.md) | 围绕外部 argv workload 采集日志 delta/轮转 warning、Host/process/metrics 与可选 bpftrace，输出 evidence manifest；原 cube-bench 流仍由 Workflow 兼容 |
| [`sandbox-stage-latency-analyze`](skills/sandbox-stage-latency-analyze/SKILL.md) | 复用共享分析器生成 Cubelet/Shim/CubeMaster 与 CreateContainer 统计、拓扑 CSV、details JSONL 和摘要 |
| [`sandbox-timeline-svg-render`](skills/sandbox-timeline-svg-render/SKILL.md) | 直接读取 analysis JSON + 阶段 CSV 生成 timeline.svg 和渲染元数据，新路径不依赖 Excel |
| [`sandbox-profile-report`](skills/sandbox-profile-report/SKILL.md) | 只复用既有 analysis 生成七 sheet Excel、阶段/分批/Host PNG 和报告 manifest，不重新运行 analyzer |
| [`sandbox-timeline-profile`](skills/sandbox-timeline-profile/SKILL.md) | 四阶段一键 Workflow、旧 `run_c50_profile.sh` / `profile_dir` 调用兼容与共享脚本；不计入四个聚焦 Skill |

四个入口均为各 Skill 下的 `scripts/run.sh`，安装后通过平铺的 `sandbox-timeline-profile/scripts/` 解析共享库；缺共享库会明确 blocked。外部 Workflow 每轮要求新的 `startup-latency`，调用方必须在 workload 退出前交付同一 case 的 summary/benchmark JSON 对；失败 manifest、旧 benchmark 或路径重合都会阻止下游发布。进程快照不采集 argv，避免泄露主机命令行秘密。跨进程时钟、日志不可加性、阈值部分观测、轮转与 warmup 判读纪律统一保留在共享参考文档中。

### 分析型子 skill(subskills,自研)

| 技能 | 描述 |
|------|------|
| `bios-optimization` | BIOS 优化建议(SMT/C-State/NUMA/内存通道) |
| `os-optimization` | OS 层优化(governor/THP/HugePages/sysctl/ulimit) |
| `network-optimization` | 网络参数调优(网卡/IRQ/队列/RSS/RPS) |
| `cpu-affinity-optimization` | CPU 亲和性(绑核/绑内存/中断亲和) |
| `performance-library-selection` | 性能库选型(malloc/memcpy/压缩/加密) |
| `compiler-optimization` | 编译优化(版本/架构参数/LTO/PGO/向量化) |
| `application-config-optimization` | 应用配置(线程/队列/批量/缓存/连接) |
| `accelerator-optimization` | 密码加速卡优化: HPRE/KAE 密码卸载(UADK/ASF) |
| `hardware-upgrade-analysis` | 硬件规格升级建议 |
| `io-optimization` | 非 CPU 瓶颈预筛 + 磁盘/内存专项证据采集 |
| `database-workload-analysis` | 数据库专项分析(MySQL/InnoDB) |
| `other-optimization` | 其他优化方向兜底 |

### 工具辅助类 skill

| 技能 | 描述 |
|------|------|
| [`topdown-quality-diagnosis`](skills/topdown-quality-diagnosis/SKILL.md) | 鲲鹏 DevKit Top-down 采集质量审核：核对目标、窗口与事件口径，诊断异常数值和运行中断，提供有界复采方案与证据报告 |
| [`kptools-thread-affinity-analyzer`](skills/kptools-thread-affinity-analyzer/SKILL.md) | 线程绑核分析器 — 读取 kptools-thread-affinity-collector 输出目录做离线分析：拓扑富化(NUMA/NPU 归属)、角色识别规则外置 roles.json、8 类异常判定(A1 忙线程跨 NUMA 漂移/A1u 掩码不可读/A2 NPU 主机线程错位/A3 忙实例挤核/A4 掩码震荡/A5 绑核意图未覆盖按共同在场区间/A6 观测完整性/A7 线程翻动)，输出 HTML 报告(仅看异常过滤)+ summary.json(原子写入)；跨机采样包须带 topology_raw.txt |
| [`kptools-thread-affinity-collector`](skills/kptools-thread-affinity-collector/SKILL.md) | 运行期线程绑核采集器（只读，任意进程）— 双通道持续采样每线程 CPU 允许掩码与实际落点，产出三件套：samples.jsonl（机器格式时间序列）+ trace.log（人类可读事件日志：新线程/掩码变化/线程退出/心跳/终态名册/统计，cat 即懂）+ topology_raw.txt（拓扑快照）；支持 --auto/--tick 分步与长任务；采集与分析职责分离，目标机只装采集器、输出目录跨机取回 |
| [`kptools-cannlab-env`](skills/kptools-cannlab-env/SKILL.md) | cannlab 分设环境连接与远程调试 — 三级链路(本机→CPU dev容器→昇腾 NPU Pod)操作手册：SSH/引号纪律、scp 传输、remote_tool.sh 全子命令、NPU 开机预热 API、退出码契约(0-10)、rsync --delete 同步与文件生命周期纪律、长任务 nohup 轮询模式、NPU 环境配方(set_env/pyfix312 边界)、14 条实战踩坑(引号被吞/pip 陷阱/shm 与 AICPU 目录缺失/21008 根因分型等)；只管链路，执行内容路由具体任务 skill |
| [`kptools-cannlab-env`](skills/kptools-cannlab-env/SKILL.md) | cannlab 分设环境连接与远程调试 — 三级链路(本机→CPU dev容器→昇腾 NPU Pod)操作手册：SSH/引号纪律、scp 传输、remote_tool.sh 全子命令、NPU 开机预热 API、退出码契约(0-10)、rsync --delete 同步与文件生命周期纪律、长任务 PID/退出码文件模式、NPU 环境配方(set_env/pyfix312 边界)、15 条实战踩坑(引号被吞/pip 陷阱/shm 与 AICPU 目录缺失/21008 根因分型等)；只管链路，执行内容路由具体任务 skill |
| [`kptools-prompt-capture`](skills/kptools-prompt-capture/SKILL.md) | 采集 Python proxy 转发给 vLLM 的请求，分析请求分布和多轮会话，生成报告；支持已有采集数据的离线分析 |
| [`kptools-dplb-profiler`](skills/kptools-dplb-profiler/SKILL.md) | vLLM DP 负载均衡数据采集：现场轮询 /metrics（单池/PD 双池）与 Prometheus 时间段回采两种方式产出统一的 dp_load_metrics CSV（30 数据列 + queue_fixed） |
| [`kptools-dplb-report`](skills/kptools-dplb-report/SKILL.md) | vLLM DP 负载均衡分析报告：解析 dp_load_metrics CSV，产出单池与 A/B 策略对比 HTML 报告（CV/LIF/方差分解/TTFT 相关性）；数据由 kptools-dplb-profiler 采集或由用户提供 |
| [`kptools-arthas`](skills/kptools-arthas/SKILL.md) | Java 进程性能分析(Arthas 批处理只读采集：trace/watch/monitor/tt 方法级耗时分解与入参观察、thread 高 CPU/阻塞/锁竞争分析、memory/GC 状态、JVM 运行时性能参数、ognl 查询)，含性能分析剧本、采集纪律与 agent 级 with/without A/B 用例 |
| `kptools-ascend-pd-env` | vLLM-Ascend P/D 容器环境采集与对比(CANN/驱动/torch_npu/HCCL/cgroup/启动参数差异) |
| [`kptools-vllm-ttft`](skills/kptools-vllm-ttft/SKILL.md) | vLLM / vLLM-Ascend v0.27.1rc 首 token 延迟诊断，支持日志预检、按执行步和 TP 生成 HTML，以及按需规划与回滚 Python 打点 |
| `kptools-jvmjit` | JVM JIT 符号化 perf 采样分析(libperf-jvmti + perf inject --jit：方法级热点、JIT 代码指令级注解导出 annotate.txt 源码+汇编+逐指令周期占比、容器内采集与工具注入 toolpack)，含 dump 刷盘语义与 [JIT] 残留处理纪律 |
| `cubesandbox-deployment` | CubeSandbox 单机在线部署(x86_64/ARM64 资产选择、部署预检、SHA-256 校验、官方 one-click 安装与服务健康检查；不含模板或性能测试) |
| `cubesandbox-bench-protocol` | CubeSandbox 生命周期性能基准协议(创建/恢复吞吐与延迟的标准化场景、纪元纪律、多轮连跑防冷窗口、轮间清理清单、轮内分布形态判读) |
| [`cubesandbox-lifecycle-bench`](skills/cubesandbox-lifecycle-bench/SKILL.md) | CubeSandbox 核心生命周期一键 Workflow/路由与共享执行器(保留计划哈希、预检、准备、29 档全矩阵、冒烟、差集清理与证据归档) |
| [`kptools-openeuler-swegym-image-rebuilding`](skills/kptools-openeuler-swegym-image-rebuilding/SKILL.md) | SWE-Gym 任务镜像重建方法论(aarch64/x86 通用) — era-pin 四层锚定(git 时代锚点/freeze 清单/日期窗口/A/B 边界)、SCRIPT_FIXES 修复史、gold=1/empty=0 验收口径与判分/环境问题分流，含 agent 级 with/without A/B 用例(依赖漂移翻案) |
| [`kptools-cpuoffload-analyzer`](skills/kptools-cpuoffload-analyzer/SKILL.md) | torchtitan-npu CPU offload 内存分析器(bundle 离线三自检：node 映射自洽/L1 存活配对交叉核对/采样放大；nova 重建与骤降按共同 rank 对齐判定；bundle 只读，报告写 bundle 同级目录) |
| [`kptools-cpuoffload-collector`](skills/kptools-cpuoffload-collector/SKILL.md) | torchtitan-npu CPU offload pinned 内存采集器(L0 pagemap 快照：devmm_svm 段定位/present 页计数/NUMA node 归属/多时间锚点序列；可选 L1 sitecustomize 埋点；判读归姊妹 analyzer) |

#### CubeSandbox 单项基准入口（10 个）

以下入口共享 `cubesandbox-lifecycle-bench/scripts/` 和 `default-plan.json`，不是十份执行器副本；
八个场景入口固定各自 section，preflight/prepare 分别固定只读检查和独立准备。

| 技能 | 单点能力与成功证据 |
|------|------|
| [`cubesandbox-bench-preflight`](skills/cubesandbox-bench-preflight/SKILL.md) | 只读 check；输出 `preflight.json`，FAIL 即受阻，不自动修复或压测 |
| [`cubesandbox-bench-prepare`](skills/cubesandbox-bench-prepare/SKILL.md) | 独立 NUMA/TAP 准备；输出 `prepare.json`、readiness 和复验结果，不运行基准 |
| [`cubesandbox-template-create-bench`](skills/cubesandbox-template-create-bench/SKILL.md) | Template Create；复用 bench-protocol 生成 `create-verdict.json` |
| [`cubesandbox-density-bench`](skills/cubesandbox-density-bench/SKILL.md) | Density；输出内存/实际存活数据并完成本章节差集排空 |
| [`cubesandbox-snapshot-bench`](skills/cubesandbox-snapshot-bench/SKILL.md) | 独立沙箱的 Snapshot 制作并发 |
| [`cubesandbox-dirty-page-bench`](skills/cubesandbox-dirty-page-bench/SKILL.md) | 指定 dirty MiB 的 Snapshot/恢复矩阵 |
| [`cubesandbox-snapshot-create-bench`](skills/cubesandbox-snapshot-create-bench/SKILL.md) | 同一 Snapshot 的并发创建/恢复 |
| [`cubesandbox-rollback-bench`](skills/cubesandbox-rollback-bench/SKILL.md) | 各沙箱回滚到自身 checkpoint 的并发 Rollback |
| [`cubesandbox-clone-bench`](skills/cubesandbox-clone-bench/SKILL.md) | 运行中源沙箱的并发 Clone，区分 num 与 concurrency |
| [`cubesandbox-pause-resume-bench`](skills/cubesandbox-pause-resume-bench/SKILL.md) | 并发 Pause 后 Resume |

每个入口都有 `scripts/run.sh` 和 Tier-A `cases/skill-contract/`。场景可先 `--dry-run`、再用
`--show-plan` 生成固定范围的批准哈希；正式运行沿用统一锁、冒烟、清理和 `results.json.ok` 判据。

### 场景优化类 skill

| 技能 | 描述 |
|------|------|
| `nginx-optimization` | Nginx 场景专属调优(http/https × 长/短连接四场景、L1 环境/L2 配置/L3 鲲鹏加速分层、IRQ-worker 布局、KAL OpenSSL/KAE 加速、火焰图热点对策与压测验证纪律)，含 evals 评测集与 agent 级 with/without A/B 用例集(场景决策、调优循环纪律) |
| [`redis-optimize`](skills/redis-optimize/SKILL.md) | Redis 场景专属调优(实例发现、网卡 NUMA 定位、进程绑核、多队列中断绑核、ntuple 按端口分流、redis.conf/OS 参数与 redis-benchmark 验证纪律)，含 evals 评测集与 tier-A skill-integrity 完整性用例 |
| [`kptools-dpdk-tuning`](skills/kptools-dpdk-tuning/SKILL.md) | DPDK 应用性能调优(诊断+顾问双模式) — 现象驱动的分层排查与硬件+场景驱动的分层配置建议，覆盖 BIOS/内核/EAL/mbuf 内存池/PMD 网卡/功耗低时延，不修改业务源码；需要改码的优化移交 code-optimizer 的 `dpdk-optimization` |

### 环境部署 skill

| 技能 | 描述 |
|------|------|
| [`redis-deploy`](skills/redis-deploy/SKILL.md) | Redis 环境部署(源码构建、默认 7.0.15、sha256 校验、systemd 托管、多实例、离线部署、升级/回滚与验收；构建目录 root 私有 0700、路径拒绝 . 与 .. 段、不自动修改内核参数、不执行远程脚本、卸载需人工确认)，含 evals 评测集与 tier-A skill-integrity 完整性用例 |

### 独立诊断 skill

| 技能 | 描述 |
|------|------|
| [`kptools-torchtitan-deploy`](skills/kptools-torchtitan-deploy/SKILL.md) | torchtitan-npu 部署手册与踩坑指导 — 版本配套表(以上游 installation.md 为准的快照)、昇腾软件栈六件套安装(NNAL source 顺序)、三步源码安装与 torchao-npu、新配置系统(--module/--config，弃用 TOML)、run_train.sh 单机/多机环境变量逐项、单卡 debugmodel 冒烟、affinity 扩展配置(MR 1100 已合入 master)；踩坑实录 A 组以仓内补丁为可溯源证据(附真实路径)，B 组为实战环境坑(配置系统重构/版本错位/910B3 sync-start 地板/CPU_AFFINITY_CONF 主线程静默失效) |
| `rocksdb-perf-diagnostics` | RocksDB 性能诊断与优化 — 用 LOG / properties / Statistics / perf_context / EventListener 定位写停顿、compaction 积压、读写/空间放大,并在授权下执行单变量复测 |

## 目录结构

```
app-tuner/
├── .claude-plugin/plugin.json         # 插件清单
├── README.md                          # 本文件
├── docs/                              # 设计文档与使用指南
│   ├── architecture-4plus1.md
│   ├── usage-guide.md
│   ├── report-template.md
│   └── ...
├── examples/                          # 示例运行产物
│   └── mysql-test/
├── skills/                            # 技能入口及嵌套技能包
│   ├── kptools-agentenv-tuning/       # AgentENV 专项技能包
│   │   ├── SKILL.md                   # 场景入口
│   │   ├── references/                # 共享证据与验证约定
│   │   ├── scripts/                   # 只读环境采集
│   │   └── skills/                    # 可独立调用的专项 Skill
│   └── kpbot-app-tuner/               # 主 skill
│       ├── SKILL.md                   # 唯一规范源
│       ├── agents/                    # 平台 agent 配置
│       ├── references/                # 参考文档(22 个)
│       ├── scripts/                   # 脚本(22 个)
│       └── subskills/                 # 自研分析型子 skill(13 个，含 scripts/ 和 references/)
└── opencode/                          # OpenCode 覆盖层(差异文件)
```

## 使用指南

### 快速上手

安装后直接在 Claude Code 或 OpenCode 中用自然语言描述你的优化需求:

```
帮我优化这个 MySQL 服务的性能,目标是提升 sysbench read-only QPS
```

```
分析这个应用的瓶颈,环境信息已经采集在 /path/to/env-backup
```

### 主要入口

| 场景 | 告诉 AI | 底层技能 |
|------|---------|---------|
| 完整优化流程 | "帮我优化这个服务器应用的性能" | `kpbot-app-tuner` |
| AgentENV 沙箱 | "检查 AgentENV 的 ublk 或并发恢复性能" | `kptools-agentenv-tuning` |
| 环境备份 | "备份当前环境信息" | `scripts/backup_environment.sh` |
| 报告生成 | "生成优化报告" | `scripts/generate_report.py` |
| RocksDB 性能问题 | "RocksDB 写停顿/读慢/compaction 抖动怎么优化" | `rocksdb-perf-diagnostics` |
| Redis 环境部署 | "部署一个 Redis 环境（默认 7.0.15）" | `redis-deploy` |

### 优化工作流

```
环境采集 → 基线测试 → 瓶颈识别 → 动态路由 subskill → subagent 迭代验证 → 报告输出 → 环境还原
```

## 文档

- [架构设计(4+1)](docs/architecture-4plus1.md) — 主 skill 编排、子 skill 关系、运行闭环
- [使用指南](docs/usage-guide.md) — 不同平台如何加载和使用
- [报告模板](docs/report-template.md) — 首版优化报告模板
| [`kptools-vllm-ttft-workflow`](skills/kptools-vllm-ttft-workflow/SKILL.md) | 按已有产物选择 TTFT 阶段，协调双侧计划、人工授权和采集交接 |

## 脚本说明

脚本位于 `skills/kpbot-app-tuner/scripts/`,主要包括:

| 脚本 | 作用 |
|------|------|
| `backup_environment.sh` | 环境信息采集 |
| `collect_evidence_snapshot.sh` | 动态证据快照采集 |
| `detect_bottleneck.sh` | 瓶颈识别 |
| `create_subagent_tasks.py` | 生成 subagent 任务包 |
| `merge_subagent_results.py` | 合并候选池 |
| `create_execution_task.py` | 生成执行验证任务 |
| `apply_optimization_action.sh` | 受控执行调优动作 |
| `generate_report.py` | 生成最终报告 |
| `dynamic_workflow_manager.js` | 工作流状态管理 |
| `record_timing.py` | 耗时记录 |
| `summarize_improvement.py` | 收益汇总 |

## License

Apache-2.0
