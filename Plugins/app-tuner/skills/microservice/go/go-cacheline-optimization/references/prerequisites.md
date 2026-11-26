# 依赖与降级

本 skill 只做缓存行争用识别和优化点判定；证据采集依赖以下 skill、工具和输入。缺失时按“缺失时处理”降级或阻塞，不把猜测当结论。目标运行端为 Linux；Arm64 的 `perf c2c` 依赖 SPE。

| 依赖 | 用途 | 分级 | 缺失时处理 |
| --- | --- | --- | --- |
| `perf` 或硬件缓存事件采集 | cache miss、跨核缓存行迁移、true/false sharing 证据；命令与权限见 `cacheline-contention-playbook.md` §2 | 必需 | 输出 `blocked_evidence`，只保留假设和最小补采命令 |
| `go-pprof-analysis` skill | CPU/heap profile 和热点调用链采集 | 推荐 | 现场采集等价 profile 时按 playbook §6 正常分级；仅 `runtime.MemStats` 时置信度 `low` 并保留补证分支 |
| `go` 工具链 | 读取 `VERSION`、检查二进制身份 | 必需 | 输出 `blocked_go_baseline` |
| 结构定义或调试符号 | 字段归属、缓存行分组、核归属映射 | 推荐 | 无法落到字段时输出 `undetermined`，不标记为伪共享 |
| 压测客户端与固定负载 | 可复现的证据和对照实验 | 推荐 | 标记 `degraded`，只输出假设和最小补采计划 |
| `microservice-ab-testing` skill | 仅在验证候选变更时使用 | 按需（验证阶段） | `blocked_dependency`，只输出优化点卡片，不进入 A/B |
| `go-runtime-patch-build` skill | 仅在优化点需要运行时补丁时使用 | 按需（执行阶段） | `blocked_dependency`，只输出移交建议 |
| `code-optimizer` | 业务结构布局、字段重排等代码实现 | 按需（执行阶段） | 只输出移交建议，标记 `analysis_only` |
| 重编译与重启授权 | 执行阶段构建工具链、替换二进制 | 按需（执行阶段） | 输出 `blocked_authorization`，不影响识别阶段 |

`perf` 采集需要 `perf_event_paranoid` 允许或 root / `CAP_PERFMON`；无权限且拿不到缓存事件时输出 `blocked_evidence`。Arm64 上 `perf c2c` 依赖 SPE；无 SPE 但有 `perf stat` cache 事件时降级为粗粒度计数并标记 `degraded`，完全没有缓存事件时输出 `blocked_evidence`。补丁来源和运行补丁由 `go-runtime-patch-build` 校验，本 skill 不定义补丁来源状态。

状态口径：缺缓存事件 → `blocked_evidence`；缓存事件可用但单一来源或缺压测 → `degraded`；`medium`/`low` → `inconclusive`；仅 `high` → `confirmed`。

## 安装后的路径约定

本 skill 会由 `install.sh` 从 `skills/microservice/go/go-cacheline-optimization/` 平铺安装为
`<CONFIG_ROOT>/skills/go-cacheline-optimization/`。本文件位于 skill 自身的 `references/` 下，
随 skill 一起复制，安装后仍可通过 `references/prerequisites.md` 访问。

不要在本 skill 内使用跨 skill 的上级相对路径；域目录只在源码树中存在，安装后会被展平。
依赖 skill 只写 skill 名，由调用方按安装后的顶层 skill 查找。
