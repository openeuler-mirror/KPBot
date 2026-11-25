# Sonic Go 组件专题（微服务域）

`github.com/bytedance/sonic` JSON 编解码组件专属的微服务优化能力。专题内不放 `subskills/`，skill 直接平铺在 `sonic-go/` 下。

## 成员

| Skill | 职责 |
|---|---|
| [`sonic-go-pool-optimization`](sonic-go-pool-optimization/SKILL.md) | Sonic encode/decode 池清单、热点定位、buffer 复用和通用池参数落地 |

## 域内路由

- Sonic JSON 编解码池/热点 → `sonic-go-pool-optimization`
- 通用 `sync.Pool` 参数 → `../go/go-syncpool-optimization/`
- pprof 证据 → `code-optimizer` 侧 `microservice/go-pprof-analysis`
- 通用 A/B 方法 → `../microservice-ab-testing/`

## Review

- owners: []（待指派，填写 GitCode 用户名）
- 评审范围：`Plugins/app-tuner/skills/microservice/sonic-go/**`
