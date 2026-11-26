---
name: kitex-troubleshooting
description: 当 Kitex 服务出现慢、抖动、连接异常或内存上涨但原因不明时使用。交付症状到专项技能的分诊路径与最小补采清单。
---

# Kitex 症状分诊

本 skill 只做分诊与交接，不输出具体参数值。专项参数在对应 skill，验证统一走 `microservice-ab-testing`。若需要完整链路（基线 → 证据 → 分诊 → 调优 → 验证），按 `kitex/WORKFLOW.md` 执行。

## 何时触发

- 只有现象（慢、抖、连接多、GC 高），没有明确瓶颈类别。
- 已经做过一些改动但没有收益，需要重新定位。
- 需要判断"该采哪一份最小证据"再决定找哪个专项。

## 范围

- 覆盖：症状到技能的路由、最小补采清单、无法判定时的输出规范。
- 不覆盖：判据、参数值与收益承诺。

## 出处

- 症状与技能的对应关系来自本专题各 skill 的"何时触发"，不引入外部结论。

## 命中验证与越界验证

- 命中：问"服务变慢但不知道原因"，应答"症状 + 需补的三类证据 + 候选技能"。
- 越界：问"事件循环线程数设多少"，应答拒绝给值并转 `kitex-transport-tuning`。

## 分诊表

| 症状 | 首先看 | 可能结论 | 交给 |
|---|---|---|---|
| 加核不涨 QPS，CPU 未打满 | 系统调用占比、事件循环负载、连接获取锁 | 事件循环不足 / 连接池竞争 | `kitex-transport-tuning` |
| CPU 打满且系统调用占比明显偏高 | 系统调用分布、连接数 | 内核收发路径主导 | `kitex-transport-tuning`，必要时评估同机通信方案 |
| P99 长尾、P50 正常 | 锁/阻塞 profile、复用连接后是否恶化 | 队头阻塞 / 锁争用 / GC 停顿 | `kitex-transport-tuning` 或 `go-runtime-tuning` |
| 连接数、fd 暴涨 | 并发度、连接池参数、dial 次数 | 独占连接不合适 / 池参数过小 | `kitex-transport-tuning` |
| GC 次数高、RSS 增长 | 堆采样、每请求分配 | 分配热点 / GC 参数过紧 | `kitex-allocation-hotspot-tuning` → `go-runtime-tuning` |
| cache miss 高、跨核迁移或怀疑伪共享 | 缓存事件、字段归属、核读写分布 | 缓存行争用 / 真共享 / 容量 miss | `go-cacheline-optimization` |
| 编解码帧占比高 | CPU profile、IDL 形态 | 实现选择或 payload 形态 | `kitex-thrift-codec-selection` |
| 启用透传后变慢、分配上升 | header 数量与长度、协议 | 元信息开销 | `kitex-transport-protocol-selection` |
| 跨节点部署后整体变慢 | 服务端与压测端/上游的节点位置 | 跨 NUMA 访存 | 先按 `kitex-transport-tuning` 的 NUMA 结论调整，绑核移交系统层 |
| ARM64 上泛化调用明显慢、有降级告警 | 动态编解码库告警、是否用静态生成代码 | 加速路径缺失 | `kitex-transport-protocol-selection` |
| 升级后行为或性能变化 | 版本默认值、依赖版本 | 默认值漂移 | `kitex-version-upgrade-audit` |
| 埋点开启后怀疑变慢 | 同口径开/关对比 | 多数情况在噪声内 | `kitex-profiling` |

## 最小补采建议

无法判定时，按顺序补三项即可覆盖大部分情况：

1. CPU profile（10 秒）——判断计算/系统调用/调度。
2. 锁与阻塞 profile（需先开采样率）——判断争用位置。
3. 堆采样（分配空间 + 对象数）——判断分配来源。

同时记录：QPS/P50/P99、服务端 CPU、fd 数、GC 次数、客户端 CPU 使用率。

## 输出

- 能判定：给出"症状 → 证据 → 结论 → 下一 skill → 验证方式"。
- 不能判定：明确写"证据不足"，列出已排除项与最小补采清单，不得给参数建议。
- 证据冲突：列出冲突点，说明优先采信哪一份及原因（例如同口径、重复次数多、观测器已分离）。
- 不把历史案例结论直接当本轮结论。

## 与相邻技能的边界

- 只做路由：不输出参数值、阈值或收益承诺；判定方法与采集在 `kitex-profiling`，动作在对应专项 skill。
- 与完整流程的分工：本 skill 只提供"症状 → 技能"的查找表；阶段、门控与产出由 `kitex/WORKFLOW.md` 负责，两者可分别独立使用。
- 不跨轮次合并多个专项动作。
- 本 skill 可独立调用，不依赖 `WORKFLOW.md` 的中间产物。

## 参考

- 证据采集与判定：`kitex-profiling`
- 完整流程：`kitex/WORKFLOW.md`
