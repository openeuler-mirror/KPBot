# Kitex 专题（微服务域）

CloudWeGo/Kitex 是 Go 微服务 RPC 框架。本专题只放 **Kitex 组件专属** 能力：传输与连接、编解码与协议、证据解读、分诊、版本治理、框架静态分析。

通用能力不放这里：

- 单变量 A/B 的测量与判定口径 → `microservice-ab-testing`
- GOMAXPROCS、容器配额、GC 参数 → `go-runtime-tuning`
- 通用 Go profiling 方法 → `code-optimizer` 侧 `microservice/go-pprof-analysis`
- 通用运行时补丁与重编译 → `go-runtime-patch-build`
- Go 缓存行争用识别（cache miss、跨核迁移、伪共享/真共享）→ `go-cacheline-optimization`
- 绑核、IRQ 亲和、内核参数 → 由 app-tuner 主流程的 `cpu-affinity-optimization`、`network-optimization`、`os-optimization` 承接

## 推荐入口

- 需要从零做一次组件级分析与诊断 → 先读 [`WORKFLOW.md`](WORKFLOW.md)，按"识别 → 基线 → 证据 → 分诊 → 单变量调优 → 验证/移交"执行。
- 只是单点问题（选 codec、选协议、要不要调事件循环）→ 直接调用对应 skill，不必走完整流程。
- WORKFLOW 是可选编排，不改变各 skill 的独立调用方式、门控与回退。

## 成员

| Skill | 职责 |
|---|---|
| [`kitex-transport-tuning`](kitex-transport-tuning/SKILL.md) | 事件循环线程数、连接形态（独占/复用）、连接池参数、与网卡中断的 NUMA 对齐 |
| [`kitex-allocation-hotspot-tuning`](kitex-allocation-hotspot-tuning/SKILL.md) | 请求路径高频分配（字符串解码、上下文链、透传元信息、请求/响应结构）的定位与削减 |
| [`kitex-thrift-codec-selection`](kitex-thrift-codec-selection/SKILL.md) | fastcodec / frugal / Apache Thrift 选型与 IDL 形态影响 |
| [`kitex-transport-protocol-selection`](kitex-transport-protocol-selection/SKILL.md) | Framed / TTHeader / gRPC 选型、元信息开销、ARM64 泛化调用可用性 |
| [`kitex-profiling`](kitex-profiling/SKILL.md) | Kitex 证据采集、特征帧解读与证据充分性判定 |
| [`kitex-troubleshooting`](kitex-troubleshooting/SKILL.md) | 症状驱动的分诊路径与最小补采建议 |
| [`kitex-version-upgrade-audit`](kitex-version-upgrade-audit/SKILL.md) | 版本默认值变更、依赖耦合、ARM64 缺口与升级对照 |
| [`kitex-hot-path-analysis`](kitex-hot-path-analysis/SKILL.md) | 框架静态热路径分析与改造候选，只输出分析不改代码 |

## 共享资产

- [`assets/`](assets/README.md)：最小 echo demo（IDL、回环 handler、server/bench 模板、一键运行器），供本专题各 skill 复用。
- [`references/kitex-demo-and-benchmark.md`](references/kitex-demo-and-benchmark.md)：demo 搭建与 Kitex 专属测量注意事项。

验证资产按需放置：`evals/eval_queries.json` 只覆盖近名易混对（transport / protocol / codec、allocation / runtime），其余 skill 以 SKILL.md 内的「命中验证与越界验证」作为验收，不强行补齐。

## 域内路由

- 加核不涨、系统调用占比高、连接数/fd 异常、复用连接后长尾变差 → `kitex-transport-tuning`
- 每请求分配高、GC 频繁且堆增长快 → 先 `kitex-allocation-hotspot-tuning`，参数层再去 `go-runtime-tuning`
- cache miss 高、跨核缓存行迁移或怀疑伪共享/真共享 → `go-cacheline-optimization`（先识别优化点，再决定改代码或打补丁）
- 判断该用哪种编解码 → `kitex-thrift-codec-selection`；判断该用哪种传输协议/是否付元信息成本 → `kitex-transport-protocol-selection`
- 不知道该采什么证据、或火焰图看不懂 Kitex 帧 → `kitex-profiling`
- 只有现象没有方向 → `kitex-troubleshooting`
- 计划升级或升级后回归 → `kitex-version-upgrade-audit`
- 配置层已到瓶颈、想评估是否值得改框架 → `kitex-hot-path-analysis`（产出候选清单，落地由 code-optimizer 承接）
- 需要验证收益 → 统一走 `microservice-ab-testing`

## 依赖方向

```text
kitex-troubleshooting
  ├── kitex-profiling
  ├── kitex-transport-tuning
  ├── kitex-allocation-hotspot-tuning
  ├── kitex-thrift-codec-selection
  └── kitex-transport-protocol-selection
        └── 统一验证：microservice-ab-testing
参数层：go-runtime-tuning
缓存行识别：go-cacheline-optimization
候选落地：code-optimizer（由 kitex-hot-path-analysis 输出候选）
```

## Review

- owners: []（待指派，填写 GitCode 用户名）
- 评审范围：`Plugins/app-tuner/skills/microservice/kitex/**`
