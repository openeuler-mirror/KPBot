# 原理与实施：rootfs 预派生与领养

## 成本迁移

原路径：`请求 → FICLONE → origin 目录 fsync → rootfs 可用`。共享目录下的高并发 fsync/元数据争用会形成尾部。

池路径：后台 `FICLONE + fsync → available`；前台 `reserve → rename → index update → resize/check → adopt`；目录持久化可合并。rename 为元数据操作，但依然有锁与 I/O 成本，不能称为零成本。

两种独立收益应分别验证：预派生省去前台 FICLONE，group commit 减少重复目录 fsync。后台工作没有消失。设消耗速率 λ、补池速率 μ、初始水位 P；持续 λ>μ 时大约 `P/(λ−μ)` 后耗尽，随后应保持正确回退。历史单 worker 每 25ms 一次还要加派生耗时，补池上限小于 40 个/秒，不能用满池一次突发代表持续 c100 性能。

## 修改位置与混合构建

| 文件 | 作用 |
|---|---|
| `Cubelet/storage/rootfs_pool.go` | 池注册表、构建、领养、flush、清池 |
| `storage/cubecow_volume_manager.go` | 创建入口先试池、模板删除钩子、引擎接口 |
| `pkg/cubecow/cubecow.go` / `cubecow_stub.go` | cgo 与非 native 构建接口 |
| `cubecow/include/cubecow.h`、`src/ffi.rs` | C ABI、错误码与 panic 边界 |
| `cubecow/src/engine/mod.rs`、`reflink.rs` | 能力接口、文件 rename、索引及目录 fsync |

Rust 静态库、C 头文件与 Go cgo 二进制必须来自同一改动。仅修改 Go wrapper 或仅重新编 Cubelet 会导致接口缺失或链接旧库；共同协议给出构建顺序。

## 状态、故障与持久化

成员状态的核心不变量：同一个名字同时最多被一个领养请求持有；从 available 移除到 rename 完成之间不能被补池器当作空槽重新创建；必要时保留 reserved 状态直到 rename 提交。引擎需在适当锁范围内校验 origin、目标冲突、文件 rename 与索引修改。

rename 成功后 resize 失败，不能无条件当作“池 miss”再派生。应按目标重试/清理契约处理已命名快照，确保下一次相同 sandbox/gen 可判定状态。历史补丁这类错误发生在 flush 信号前，迁移实现需保证已完成 rename 进入清理或持久化协议。

目录 fsync 失败不能只打一条日志就声称持久化完成。维护 pending generation，成功后才推进 durable generation；严格应答需要等待对应屏障。异步应答则报告易失窗口、重试上限和失败可观测性。不能仅凭 rename 原子性断言掉电后名字、内容和业务状态一定一致，需要目标文件系统/引擎恢复验证。

模板删除必须阻止新 reserve/fill，等待在途任务退出，再清理残留并删除模板。单纯 closed=true 加扫描不能阻止正在 I/O 的构建器在扫描之后重新创建成员。重启遇到 AlreadyExists 也不能无条件当干净成员：核对模板版本、快照 origin、是否被修改/领养和元数据一致性。

## 复现与消融

1. 原基线与候选采用相同编译参数、模板、文件系统、容量和日志级别。
2. 候选开池、候选关池分别测量；历史环境变量为 `CUBELET_ROOTFS_POOL_SIZE` 和 `CUBELET_ROOTFS_POOL_DISABLE`。历史实现对任意非空 DISABLE 值都会禁用，包括 `0`；启用时应 unset，不要设置为 `0`。
3. 满池实验先进行准备调用（历史实现首次使用才建池），清理实例，等就绪日志并记录水位；每个正式样本前按相同策略补满。首次填充时间单独报告，不计为创建快路径收益。
4. 冷池实验从无可用成员开始；持续负载实验运行到可观察耗尽/稳定状态，记录命中率、fallback、补池吞吐和后台 I/O。
5. 使用候选禁用池的结果核验是否存在其它改动贡献；池命中约 100 次只能证明机制触发，不能证明整体时间改善。

历史反例：多个热源轮换仍在前台派生，附件记录总时间恶化约 24.8%；并行突发补池也可能增加锁竞争。历史 480/25ms/20ms 为起点，必须由本次突发、资源预算和持久化契约确定。
