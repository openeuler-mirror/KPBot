# Changelog

本文件记录 server-application-optimization skill 的演进历史。

## [Unreleased]

### Changed

- 移除所有硬编码私有绝对路径（`/Users/a111/Desktop/...`），改用相对路径或 CLI 参数
- 更新 `.agents/skills` 兼容入口，与 `.claude/` 和 `.opencode/` 保持一致
- 精简主 SKILL.md，将客户端瓶颈排查和服务端压力饱和确认的详细逻辑下沉到 `network-optimization/SKILL.md`
- 精简主 SKILL.md 的工具依赖表、权限要求和容器约束章节，改为引用 `prerequisites.md`
- 重构 `input-contract.md`，将 50+ 字段拆分为主核心输入（~17 个）和子 skill 专属输入
- 去重 Operating Principles 和早停建议中的重复条目

### Added

- 在需求文档中正式纳入 `database-workload-analysis` 为第 8 个子 skill 并明确其定位
- 新增 `references/reading-guide.md`，说明文档间关系、阅读顺序和单点定义原则
- 新增 `CHANGELOG.md`

## [1.0.0] - 2026-04-10

### Added

- 主 skill 编排框架：9 步状态机（环境备份 → 报告输出）
- 7 个子 skill：bios-os、network、cpu-affinity、performance-library、compiler、application-config、io-memory-network-bottleneck
- 数据库型工作负载专项（MySQL/InnoDB + AHI 决策）
- 环境备份脚本 `backup_environment.sh`
- 瓶颈检测脚本 `detect_bottleneck.sh`
- CPU 均衡性检测脚本 `check_cpu_balance.sh`
- 网络优化辅助脚本 `optimize_network.sh`
- 收益汇总脚本 `summarize_improvement.py`
- 外部 library-replacement 和 network-io-performance 的检测与接入机制
- Claude Code、OpenCode、Agents 兼容入口
- 报告模板和 schema 定义
- 完整的 reference 文档集（workflow、input-contract、prerequisites、checklist、decision-tree 等）
