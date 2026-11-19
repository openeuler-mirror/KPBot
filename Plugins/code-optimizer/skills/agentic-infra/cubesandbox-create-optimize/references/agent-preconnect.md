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

本包 `scheme1-rpc.patch` 使用请求局部的 `JoinSet` 持有预连接任务。相比原始实现中的裸 `JoinHandle`，退出创建 future 时会自动取消集合内的任务，避免请求结束后继续重连。流程为：

```text
preconnect = request_local_JoinSet()
deadline = monotonic_now + 30s
preconnect.spawn(timeout_at(deadline, connect_with_capped_backoff))
snapshot = restore_vm()
if restore failed:
    preconnect.shutdown().await     # abort + drain，再返回原错误
    return original_error
finish_required_restore_steps()?    # 早退时 JoinSet 的 Drop 取消子任务
joined = timeout_at(min(deadline, monotonic_now + 10s), preconnect.join_next()).await
if joined contains a successful connection:
    install(connection_from(joined))
else:
    preconnect.shutdown().await     # 原串行回退前确保旧尝试退出
    conn = serial_connect().await?
    install(conn)
# 父请求在任一 await 被取消：局部 JoinSet 被 Drop，自动 abort
```

汇合时先将 `timeout_at(...).await` 的结果绑定到 `joined`，再 `match joined` 处理成功或回退，使等待操作与结果处理的边界明确。成功分支安装取得的连接；其他结果进入显式 shutdown/drain 后的串行回退。包内回归用例直接抽取这段参考补丁编译执行。

迁移时核对最终源码，不以补丁同名或相同 `index` 前像判定版本。本包修订实现包含 JoinSet 所有权、失败与回退的 shutdown/drain、等待结果先绑定再分支和 1→50ms 退避；目标已有这些性质时无需再应用。若目标仍为裸 JoinHandle 版本，应生成替换该实现的一个最终 diff，避免叠加第二个预连接任务或用旧变体覆盖已修复代码。

取消覆盖所有 return/`?` 路径。恢复失败和连接回退是可异步清理的路径，显式 `shutdown().await` 确认任务及其已产出连接被回收；其他早退及外部取消依靠 `JoinSet::drop` 发出 abort，由运行时在任务下一次调度/让出点完成回收。Rust 的同步析构不能 await，不能声称“父 future 被 Drop 时已经同步等待全部子任务退出”。裸 JoinHandle 的 Drop 会脱离任务，不能替代 JoinSet。

当前重试退避为 1、2、4、8、16、32、50ms，随后最多每 50ms 一次；保留首次短等待，避免一直以 1ms 重试。30s 是预连接本地上限，`timeout_at` 包住整个重试 future，因此一次挂起的 connect 也受约束；restore 后的 10s 汇合宽限不能延长起始的绝对 deadline。

0.7.1 的 `create_sandbox(&mut self)` 没有请求 deadline 入参，本补丁不伪造调用链不存在的预算接口。父请求取消会通过 Drop 停止预连接；目标版本若已有显式请求 deadline，应让本地 deadline 取请求剩余预算与 30s 上限的较早者，串行回退也遵守该预算。30s 不表示 VM 恢复或整个创建操作必须在 30s 内结束。前提是底层 connect future 可取消且不在内部另行遗留无主任务；真实传输仍需集成验证。

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

## 包内取消回归

在本 skill 目录执行 `python3 cases/preconnect-lifecycle/run.py`（Rust/Cargo 与 Python 3.9+）。runner 从本包实际 patch 提取 setup/join 两段代码，嵌入可控 VM/传输 fixture 后在真实 Tokio runtime 上执行 12 项测试，而不是另写一份修复实现。用虚拟时钟覆盖恢复失败、成功连接后恢复失败、连接/退避/后续阶段取消、父请求更短的 timeout、后续 `?`、挂起连接 deadline、汇合预算、串行回退、正常成功及 c100 失败突发。此用例不替代完整 CubeShim 编译和真实 Guest/FD 集成测试。

该命令会执行从参考补丁提取的代码，使用无生产凭据的隔离测试环境。Cargo.lock 由 Cargo 维护，不手工添加未启用特性的包；依赖解析与离线复验方法见本包 [reproduction.md](reproduction.md)。
