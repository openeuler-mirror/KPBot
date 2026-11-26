# 示例：从 cache miss 现象到优化点卡片

本示例为合成场景，只演示识别流程，不包含真实机型、版本或测量数据，也不给具体修复方案。

## 场景

某 Go 微服务在压测中出现：

- cache miss 事件集中在 runtime 分配/调度路径。
- 多个 worker 高频更新同一个请求统计结构。
- 单核运行时相关事件消失，多核运行后恢复。
- 容量、带宽、锁和 NUMA 远端访问没有显示主导。

## Step 0：目标

回答三个问题：是否存在缓存行争用、在哪个结构/字段、是否值得优化。

## Step 1：证据采集

| 证据 | 观察 | 说明 |
|---|---|---|
| 硬件缓存事件 | cache miss 与跨核迁移集中在同一函数 | 支持共享/布局假设 |
| profile | 热点在统计更新函数，不在编解码 | 排除分配/编解码主导 |
| 结构归属 | 两个计数字段位于同一缓存行，分别由不同 worker 写 | 支持伪共享假设 |
| 受控实验 | 单核时事件消失；隔离副本后事件下降 | 支持共享行假设 |

反例：容量 miss 随工作集上升、锁等待、NUMA 远端访问均未出现。

## Step 2：差分诊断

- 真共享：两个字段逻辑独立，不是同一字段。
- 容量 miss：工作集未变，miss 不随数据规模上升。
- 伪共享：不同字段跨核写，落在同一缓存行。
- 结论：伪共享假设，置信度 `medium`（两类证据一致，实验只做了一次）。

## Step 3：字段归属

```text
structure: requestStats
fields:
  counterA: worker-0 读写
  counterB: worker-1 读写
line: 样本对象地址 + 现场缓存行大小核验后同一行
constraints: 无序列化/反射/unsafe 暴露
```

## Step 4：假设验证

- 单核 vs 多核：单核事件消失，多核恢复。
- 保持访问者不变，只改变两个字段是否同处一条缓存行：同线版本事件上升，分行版本下降。
- 不使用独立副本/隔离共享写，因为它会同时消除真共享，无法单独归因伪共享。
- 反例：容量、锁、NUMA 未解释。
- 置信度：`medium`；要达到 `high`，需要第二组独立实验或重复验证。

## Step 5：优化点卡片

```text
finding_id: F-001
structure: requestStats
fields: counterA, counterB
contention_type: false_sharing
evidence:
  - 硬件事件：cache miss 与跨核迁移集中在统计更新
  - profile：统计更新是热点
  - 字段归属：两个计数字段同一缓存行（样本地址 + 缓存行大小核验）
  - 受控实验：只改变行归属，同线上升、分行下降
confidence: medium
status: inconclusive
counterevidence: 容量/锁/NUMA 未显示主导
impact_scope: request stats update path
validation_plan: 重复行归属实验；复核容量、锁和 NUMA
remediation_direction: 缓存行隔离/热冷分离方向；具体布局由 code-optimizer 设计
recommended_next_skill: code-optimizer（业务布局实现）或 go-runtime-patch-build（运行时布局补丁）
not_a_finding_reason: n/a
```

## Step 6：交回

- 结论：存在缓存行伪共享假设，置信度 `medium`，状态 `inconclusive`；需重复实验或第二组独立来源后才能 `confirmed`。
- 方向：缓存行隔离/热冷分离；具体布局、参数和补丁由下一跳设计。
- 下一跳：业务结构布局 → `code-optimizer`；运行时布局补丁 → `go-runtime-patch-build`；验证 → `microservice-ab-testing`。
- 本示例只给方向，不给出隔离、重排或补丁的具体实现。
