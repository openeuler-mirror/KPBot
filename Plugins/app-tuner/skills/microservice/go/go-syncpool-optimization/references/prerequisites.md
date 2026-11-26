# 依赖与降级

本 skill 只做参数决策，实际补丁构建和服务验证依赖以下 skill 与输入。缺失时按“缺失时处理”降级或阻塞。

| 依赖 | 用途 | 分级 | 缺失时处理 |
| --- | --- | --- | --- |
| `go-runtime-patch-build` skill | 补丁校验、隔离构建、工具链构建、服务重编译、回退 | 必需 | 输出 `blocked_dependency`，只输出参数建议，不进入执行 |
| `microservice-ab-testing` skill | 固定负载、单变量 A/B、指标、正确性、判定 | 必需 | 输出 `blocked_dependency`，标记 `verified=false` |
| `go-pprof-analysis` skill | CPU/heap profile 采集与判读 | 推荐 | 降级为 `gctrace` 与 `runtime.MemStats` |
| `go` 工具链 | 读取 `VERSION`、检查二进制和运行 `gctrace` | 必需 | 输出 `blocked_go_baseline` |
| `GOROOT` 源码与外部私有补丁 | 交给 `go-runtime-patch-build` 构建 | 必需 | 输出 `blocked_patch_source` |
| 压测客户端与固定负载 | A/B 与内存水位验证 | 推荐 | 标记 `verified=false`，只输出候选动作 |
| 重编译与重启授权 | 构建工具链、替换二进制 | 必需 | 输出 `blocked_authorization` |

## 安装后的路径约定

本 skill 会由 `install.sh` 从 `skills/microservice/go/go-syncpool-optimization/` 平铺安装为
`<CONFIG_ROOT>/skills/go-syncpool-optimization/`。本文件位于 skill 自身的 `references/` 下，
随 skill 一起复制，安装后仍可通过 `references/prerequisites.md` 访问。

不要在本 skill 内使用跨 skill 的上级相对路径；域目录只在源码树中存在，安装后会被展平。
依赖 skill 只写 skill 名，由调用方按安装后的顶层 skill 查找。
