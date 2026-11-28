# Go 专题（微服务域）

Go 语言和 runtime 层面的通用优化能力。专题内不放 `subskills/`，每个 skill 直接平铺在 `go/` 下。

## 成员

| Skill | 职责 |
|---|---|
| [`go-runtime-patch-build`](go-runtime-patch-build/SKILL.md) | Go runtime 补丁、隔离构建根目录、工具链构建、服务重编译、二进制等价门控和回退 |
| [`go-syncpool-optimization`](go-syncpool-optimization/SKILL.md) | `sync.Pool` 的 `poolretain`/`poolprealloc` 参数决策、内存风险和拒绝条件 |
| [`go-cacheline-optimization`](go-cacheline-optimization/SKILL.md) | 缓存行争用的证据采集、差分诊断、字段归属、假设验证与优化点卡片；只识别优化点，不设计具体修复方案 |

## 域内路由

- 需要评估 `sync.Pool` 参数 → `go-syncpool-optimization`
- 需要判断 Go 服务是否存在缓存行争用、定位热点字段或评估优化价值 → `go-cacheline-optimization`
- 需要应用补丁、构建 Go 工具链、重编译服务或回退 → `go-runtime-patch-build`
- 需要 pprof 证据 → `code-optimizer` 侧 `microservice/go-pprof-analysis`
- 需要通用 A/B 方法 → `../microservice-ab-testing/`

## 依赖方向

```text
go-syncpool-optimization
  ├── go-runtime-patch-build
  ├── microservice-ab-testing
  └── go-pprof-analysis（按需）

go-cacheline-optimization
  ├── go-pprof-analysis
  ├── microservice-ab-testing（验证阶段按需）
  └── go-runtime-patch-build（执行阶段按需）
```

依赖只写 skill 名，不写跨目录相对路径；安装后按顶层 skill 查找。

## Review

- owners: []（待指派，填写 GitCode 用户名）
- 评审范围：`Plugins/app-tuner/skills/microservice/go/**`
