# spec 异步落库的实施与复现

## 适用条件与源码定位

`CubeMaster/pkg/templatecenter/store.go` 中的 `configureSandboxSpecHooks` 在创建成功后调用 spec 写。记录 hook 起止、行构造、SQL 等待和应答时刻，确认 UPSERT 位于主要等待路径。列出读方何时需要 spec、失败时如何补写、谁有权写同一 ID、允许的可见性延迟和崩溃后待写丢失范围。较早的应答意味着读方可能更早遇到缺失记录，验收要包含主要读方和删除操作。

默认走同步写。仅当已验证创建与其紧随的网络更新、克隆和快照读走同一 Master 及 `sandboxspec.Get`，且业务接受延迟落库和丢失窗口时，才在测试 Master 启动环境设置 `CUBEMASTER_ASYNC_SANDBOX_SPEC=1`。要求跨进程立即可见、必须保留立即快照的覆盖项且无法保证上述同进程读路径，或不能接受异步最终写失败时，维持同步写。环境变量在 hook 注册时读取，切换后重启测试进程并核对实际分支。

| 文件 | 作用 |
|---|---|
| `CubeMaster/pkg/templatecenter/store.go` | 支持将创建成功后的 MySQL spec hook 显式切换到 `PutAsync`，保留请求 clone 与注解过滤。 |
| `CubeMaster/pkg/sandboxspec/store.go` | 提供公用 record 构造、UPSERT 和解码，由 `Get` 先查同进程待写记录，保留同步 Put、Delete 入口。 |
| `CubeMaster/pkg/sandboxspec/async.go` | 有界队列、worker、重试和同 ID 写序，队列满时同步回退。 |

## 写入顺序与失败处理

在调用者内完成 canonical record 的构造与编码，队列项持有独立记录。默认队列 512、worker 2。worker 开始处理时开启 10 秒预算，覆盖等待同 ID 门闩、最多 3 次数据库尝试及其间的退避。任务在队列中的滞留时间另行计量。队列满立即转为同步写。请求仍可能等待同 ID 在途写释放门闩，等待服从请求 `ctx`。已接受入队的任务用后台预算继续执行。

通过每 ID 的门闩和代际序号协调入队、同步 Put、Delete 与 worker。新操作取得门闩后推进代际。worker 在真正执行 SQL 前检查代际，并持有门闩直到写操作结束，避免旧的在途提交晚于后续更新或删除。同 ID 的状态保留到最后一个等待者、队列项或在途操作释放引用。新意图推进代际后如果写入失败，旧排队任务仍过代；由调用方观察错误和重试最新意图。

同 Master 的 `Get` 通过同一门闩读取当前代待写记录，在解码时生成独立请求。worker 已持锁时，读方等待提交或按本次 `ctx` 返回错误。网络更新的原 `Get → 修改网络策略 → Put` 因此可以读到仍在队列中的创建输入，新 Put 推进代际，后续克隆读取更新后的策略。立即快照同样要核对读取路径使用 `Get`，并验证创建时的覆盖项进入快照输入。该可见性覆盖跟在已接受入队之后的同进程读取，进程退出、worker 终局失败和跨进程读取仍按准入契约处理。

进程级队列和门闩依赖所有写者通过上述入口。若同一 ID 可能被多 Master 或旁路写者写入，需单独建立数据库条件更新或持久化删除代际，再验证跨进程顺序。参考实现包含进程内队列，没有崩溃后重放和服务退出 drain 协议。将所需停机处理纳入目标部署生命周期，实测计划外退出后的读取与恢复结果。

## 应用与构建

本补丁与 `scheme2-rsi-cache.patch` 的异步 spec 实现互斥。需要默认同步、按需启用及同进程待写记录可见性时，选择本实现。方案二的组合补丁在初始化时启动 worker，创建 hook 直接调用 PutAsync。其 Get 读取数据库，缺少此版本的队列 trace。此版本用 CUBEMASTER_ASYNC_SANDBOX_SPEC=1 启用，首次 PutAsync 启动 worker，Get 可读取同进程待写记录。若当前树已有方案二，请从干净基线评估此补丁。需要同时保留方案二的解析缓存时，只迁入独立的缓存改动，保留一套 spec 写入、读取和 hook 实现，再验证立即快照、更新后克隆和同步回退。

参考补丁基于 CubeSandbox `v0.7.1`（`31d911e430fdf8a8879bd062b8e78066c1a8e89d`）。在隔离源码工作树执行，`SOURCE_ROOT` 与 `SKILL_DIR` 使用本机绝对路径。

```bash
cd "$SOURCE_ROOT"
git rev-parse HEAD
git apply --stat "$SKILL_DIR/references/async-spec-write.patch"
git apply --check "$SKILL_DIR/references/async-spec-write.patch"
git apply "$SKILL_DIR/references/async-spec-write.patch"
(cd CubeMaster && CGO_ENABLED=0 make build)
```

完整构建使用目标 `go.mod` 对应版本。v0.7.1 的 CubeMaster 声明 Go 1.25.7。变更后核对同步 Put、Delete、后台和回填写者最终调用的入口，逐一确认顺序或外部并发契约。通过目标实验环境已有发布方式切换 Master，核对运行产物哈希及模板 READY。

## 独立验证与回退

本包在受控 DB fixture 中执行参考补丁里的 writer 和同步入口，需要 Python 3、Git 与 Go 1.20+。在无业务凭据的测试环境执行。

```bash
cd "$SKILL_DIR"
python3 cases/spec-write-order/run.py
```

该用例通过屏障制造旧写与新 Put/Delete 的交错，覆盖过代丢弃、同步回退、请求取消及重试失败。新增夹具执行实际 `Get`、`Put` 和 worker，验证排队时的网络读改写、worker 结束后的策略重放、立即快照读取独立覆盖项以及在途读取消。真实克隆、快照服务、MySQL 与停机行为在目标环境验收。

| 场景 | 验收点 |
|---|---|
| 正常创建、读取、删除 | 对照应答后的可见性，等待后台完成后按本轮 ID 逐条核对最终内容与删除结果。 |
| 旧任务排队或执行中，收到更新或删除 | 最新操作的结果不会被旧写覆盖，删除不会被旧任务复活。 |
| 创建应答后立即更新网络，再创建克隆 | 暂缓该 ID 的创建写，确认更新能获取原输入。放开写队列并等完成后，从实际克隆输入和节点规则核对新策略，确认旧策略未被重放。 |
| 带显式覆盖项创建，应答后立即生成快照 | 暂缓该 ID 的创建写，核对实际快照输入含覆盖项。立即读与异步失败的允许结果对不上业务契约时停用异步模式。 |
| 持续负载、队列满、DB 超时 | 同步回退、失败和重试可观测。记录队列深度、最老任务年龄、回退次数、后台提交数和积压清空时间。 |
| 停机与重启 | 核验退出时待写处理、允许的丢失范围、重启后的补写与业务读方行为。 |

另做开启 DEBUG 的诊断轮，从 Master 日志提取 `async sandbox spec event=`。用 `sandbox`、`generation` 和 `queued_at_ns` 配对 `enqueued` 与 `dequeued`。按 `queued_at_ns` 到出队事件的 `at_ns` 还原排队区间，日志的打印顺序可能不同于入队顺序。采样时尚未出队的条目数是队列深度，最早 `queued_at_ns` 与采样时刻之差为最老队列任务年龄。单独累计 `sync_fallback`、`worker_committed`、`superseded` 和 `worker_failed`，对照最终 DB 记录及出队后仍在途的提交。DEBUG 仅用于诊断，正式 A/B 使用双方相同的常规日志等级。

保留相同模板、数据库拓扑和 bench 版本，以 A（同步）和 B（异步）做 A/B/B/A 交错采样。每次测量结束后，等本轮后台写完成并核对记录，再清理实例和开始下一轮，防止上一轮欠账影响结果。使用独占测试实例集并关闭内置预热。可从下列命令开始，先核对目标 bench 的 `--help`。

```bash
cube-bench -c 100 -n 100 -w 0 -m create-only --no-tui -o "$REPORT_JSON"
```

要求每份报告 100 次成功、0 错误，记录同一单位的 `summary.total_time_s` 或 `summary.total_time_ms`。按组分别取 A、B 中位数，整批创建时间下降率为 `100 × (A − B) / A`。同时报告应答后待写数、队列清空时间与最终记录核对结果。将创建快路径改善与后台完成成本分别报告。

回退前先停止本轮新建流量，处理仍在队列和在途的写入，并按 ID 核对已完成与待补写记录。恢复已校验的同步基线产物，确认运行哈希、读方可见性及创建后删除契约。
