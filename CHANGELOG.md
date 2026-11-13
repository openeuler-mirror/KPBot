# Changelog

所有重要变更记录于此文件。格式遵循 [Keep a Changelog](https://keepachangelog.com/)，版本号遵循 [SemVer](https://semver.org/)。

## [Unreleased]

### Added
- app-tuner 新增 `kptools-ascend-profiling-analysis-nostack` skill：无栈 profiling trace 的 step 识别（`STEP_TIME` → `kernel_details` → `step_trace_time` → `communication` → 纯 trace 降级）、每步时间、P1/P2/P3 近似阶段、模型结构与 Free 去向 HTML 报告；附 `detect_steps` 单测与触发评测；分析前主动收集模型/版本/源码/with-stack，缺失时降级并标低置信度。
- app-tuner 有栈 profiling 报告增强：首页/每步 Computing vs Free 重点框、按层类型聚类的模型结构（GDN/Attention/MoE/压缩层）、MTP/DSpark 外部模型结构，并去除代表实例框与包装层重复帧。
- code-optimizer 新增 `allocation-path-optimization` skill：分配路径优化（分配下限与尺寸类通胀审计、仅元数据对象精确申请、短生命周期对象抖动合并、成对释放、分配器与内核回收成本归因），源自 evbuffer 引用链元数据被 1024 B 下限放大的真实案例；附 `alloc_size_audit.py` / `alloc_probe.c`、七条分配路径不变量、tier-A 用例 `cases/size-class-audit`、tier-B 用例 `cases/metadata-floor`（自包含夹具，编两版比两版），以及只应用本 SKILL 补丁的真实 libevent 端到端用例 `cases/libevent-interfaces`
- app-tuner 新增 `kptools-dpdk-tuning` skill：DPDK 应用性能调优双模式工作流（现象驱动的分层诊断排查树：PPS/时延/丢包/启动内存/CPU 异常；硬件+场景驱动的分层配置建议：BIOS/内核 GRUB/EAL/mbuf 内存池/PMD 网卡/功耗低时延），附 M1-M10 模块参考与官方文档溯源（UPSTREAM.md），面向鲲鹏 920 + hns3/Hi1822 主场景，不修改业务源码
- code-optimizer 新增 `dpdk-optimization` skill：DPDK 数据面热路径代码五阶段优化（口径确认与线速预算 → 基线冻结 → worker 核 PMU 瓶颈建模：compute/访存延迟/带宽/同步四类判定 → 分层代码杠杆库：热路径纪律/双循环预取/per-lcore 布局/查表解析/无锁统计/mbuf 复用 → 交错 A/B 验证与负结果记账），接入统一流水线场景路由（4 个入口文件），配置层联动 app-tuner 的 `kptools-dpdk-tuning`
- code-optimizer 新增 `hot-path-overhead-elimination` skill：热点路径机制开销消除（主导结果内联、叶子入口拆分、常量宽度拷贝替代通用例程、按实测值频率重排分派、无消费者记账跳过并维护不变量、拷贝后别名重载消除），源自事件缓冲区库 evbuffer 热路径的一线优化记录；附 `value_distribution.py` / `codegen_overhead_probe.py` 两个只读分析器、tier-A 用例 `cases/probe-contract`（脚本契约 + 反汇编对照 + frontmatter/路由泛化看护）、tier-B 用例 `cases/append-notify`（自包含三接口夹具，编两版比两版），以及只应用本 SKILL 补丁的真实 libevent 端到端用例 `cases/libevent-interfaces`（需外部 libevent 2.1.12 源码树，未提供时用例 SKIP；提供后 evbuffer 回归组通过、补丁不含分配路径改动；实测记录见用例 README）
- code-optimizer 新增 `openzl-compression-tuning` skill：OpenZL 压缩内核调优（熵解码根表/块恒等、循环携带补码移位、编码批次与展开约束、熵 codec 选型、无损正确性门禁、负结果与对抗审核档案），源自 KV Cache BF16 一线优化案例（Encode 2.16x / Decode 3.44x）；附 tier-A 用例 `cases/skill-integrity`（frontmatter/引用链接/方法论不变量/敏感内容看护，见 docs/case-spec.md）
- code-optimizer 新增 `openzl-bench-protocol` skill：OpenZL 基准测试工程协议（unitBench 场景注册与熵模式、openzl_benchmark 均值口径、KV 数据准备与分布统计、绑核与噪声控制、多机部署复测、topdown TMA 采集），附 `prepare_kvdata.py` / `bench_kv.sh` / `td_loop.sh` 三个脚本与 tier-A 用例 `cases/script-contract`（脚本契约 + mock unitBench 解析 + 分布统计数值回归）

## [1.0.0] - 2026-09-03

### Added
- npm 包发布流程（`package.json` + `bin/kpbot` CLI）
- `kpbot install` / `uninstall` 子命令
- `.gitignore`、`LICENSE`（Mulan PSL v2）、`CHANGELOG.md`

### Changed
- 首个正式版本发布（从 0.1.0 开发版升级）
- README 新增 npm 安装方式（推荐），git clone 降级为开发者途径
- marketplace.json / plugin.json version 对齐至 1.0.0

## [0.1.0] - 2026-07-21

### Added
- 初始开发版本（git clone + install.sh 分发）
- 2 个插件：code-optimizer、app-tuner
