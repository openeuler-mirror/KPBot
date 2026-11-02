# Changelog

所有重要变更记录于此文件。格式遵循 [Keep a Changelog](https://keepachangelog.com/)，版本号遵循 [SemVer](https://semver.org/)。

## [Unreleased]

### Added
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
