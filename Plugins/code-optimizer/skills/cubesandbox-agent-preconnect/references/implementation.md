# 原理与实施：连接预热、连接与 runtime 复用

## 机制与收益上界

串行路径为 `restore → CONNECT → Guest 初始化`。预连接提前等待 vsock 可用，使握手的一部分与 restore 尾部重叠；Guest 未运行前必须依赖 Guest 的往返无法凭空消除。若恢复耗时 R、握手 H，理想无依赖情况的节省上界为 `min(R,H)`；实际还受 socket 出现时点、Guest 调度和汇合开销约束，通常远小于这个上界。

历史分段记录：CONNECT 约 3.96ms、SetGuestDateTime 3.79ms、RNG 0.76ms、CreateSandbox 0.91ms；这些是来源案例的局部数据，不可全部计为本方案可消除时间。历史案例实际重叠窗口仅 1–2ms。

monitor 重复握手、每次 restore 新建 Tokio runtime 是另外两种成本。历史记录中 monitor 建连 max 23.5ms、runtime 构造 max 8.1ms，提示长尾机会，但局部最大值不等于同一轮最慢请求的可消除时间。

## 实现定位

| 位置 | 修改意图 | 必须保持 |
|---|---|---|
| `SandBox::create_sandbox` | restore 前启动受控预连接，之后汇合 | 只在 restore 和必需初始化成功后发布 READY |
| `connect_agent` | 分离安装连接与建立连接 | client 与 conn 对应同一实例、失败不污染状态 |
| `monitor_vm` | 复用现有连接 | 传输支持并发、monitor 不关闭业务仍在使用的连接 |
| `DeviceManager` restore | 合适作用域内复用 runtime | 调度约束、线程配置、关闭语义与所有权明确 |

请求拥有子任务的伪代码：

```text
preconnect = start_bounded_connect(instance_identity, request_deadline)
try:
    snapshot = restore_vm()
    finish_required_restore_steps(snapshot)
    conn = join(preconnect, remaining_deadline)
    if preconnect_failed: conn = serial_connect(remaining_deadline)
    install(conn)
finally:
    cancel_if_running(preconnect)
    await_termination(preconnect)
```

取消任务必须覆盖所有 return/`?` 路径。不要只在 timeout 分支 abort；Rust 丢弃 JoinHandle 会让任务继续运行。重试间隔与上限按目标负载和 deadline 决定，历史补丁的 1ms/30s/10s 是案例参数，不是普适默认。注意 connect 本身是否可取消并受超时约束。

`OnceLock<Runtime>` 按进程首次初始化固定线程数，后续参数变化不会生效。先确认一个 shim/VMM 进程实际负责几个实例和几次 restore，再决定是否有复用空间。不能把不同进程之间的 runtime 当成共享。

## 可复现验证矩阵

| 用例 | 注入/负载 | 观察与判据 |
|---|---|---|
| 成功 | 同模板 c1 与 c100/n100 | 创建/查询/删除通过，连接身份正确 |
| 恢复失败 | vsock 出现前后分别失败 | 预连接退出，任务/FD 数在清理后回落 |
| 取消 | 重试中、连接成功后、汇合时取消 | deadline 有界、无孤立重试 |
| monitor 并发 | health 与 CreateSandbox 同时请求 | 无协议串扰或业务连接被关闭 |
| 重复恢复 | 连续多轮及实例 ID 重用 | 不接到旧 Guest；runtime 线程数不持续增长 |
| 消融 | base、preconnect-only、reuse-only、combined | 分别报告阶段时间、总时间、长尾 |

构建走共同协议的 CubeShim 链；不要因为主指标无显著提升而改测量指标或挑最好一轮。需要验证长尾时保留逐请求数据，并说明它是辅助指标。
