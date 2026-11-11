# Changelog

所有重要变更记录于此文件。格式遵循 [Keep a Changelog](https://keepachangelog.com/)，版本号遵循 [SemVer](https://semver.org/)。

## [Unreleased]

### Added
- app-tuner 新增 `kptools-ascend-profiling-analysis-nostack` skill：无栈 profiling trace 的 step 识别（`STEP_TIME` → `kernel_details` → `step_trace_time` → `communication` → 纯 trace 降级）、每步时间、P1/P2/P3 近似阶段、模型结构与 Free 去向 HTML 报告；附 `detect_steps` 单测与触发评测。
- app-tuner 有栈 profiling 报告增强：首页/每步 Computing vs Free 重点框、按层类型聚类的模型结构（GDN/Attention/MoE/压缩层）、MTP/DSpark 外部模型结构，并去除代表实例框与包装层重复帧。
- code-optimizer 新增 `allocation-path-optimization` skill：分配路径优化（分配下限与尺寸类通胀审计、仅元数据对象精确申请、短生命周期对象抖动合并、成对释放、分配器与内核回收成本归因），源自 evbuffer 引用链元数据被 1024 B 下限放大的真实案例；附 `alloc_size_audit.py` / `alloc_probe.c`、七条分配路径不变量、tier-A 用例 `cases/size-class-audit`、tier-B 用例 `cases/metadata-floor`（自包含夹具，编两版比两版），以及只应用本 SKILL 补丁的真实 libevent 端到端用例 `cases/libevent-interfaces`
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
