# Guest 预连接方案操作手册

## 1. 建立本次输入与产物

本方案适用于 CubeShim 负责启动/恢复 VM、Guest agent 经 vsock/ttrpc 连接的创建路径。历史改动只涉及 `CubeShim/shim/src/sandbox/sb.rs` 与 `hypervisor/vmm/src/device_manager.rs`，但目标版本可能调整 module 布局。以函数和行为定位，不依赖历史行号。

从任务获取 `SOURCE_ROOT`、本 skill 的绝对路径 `SKILL_DIR`、独立且不在源码构建输出中的 `RUN_DIR`、READY 模板和 bench 连接配置。以下命令在 Linux 目标机执行；参数使用实际绝对路径，技能不会自动选择机器或凭证。

```bash
set -euo pipefail
cd "$SOURCE_ROOT"
mkdir -p "$RUN_DIR/source" "$RUN_DIR/build" "$RUN_DIR/reports"
git rev-parse HEAD > "$RUN_DIR/source/commit.txt"
git diff --binary > "$RUN_DIR/source/preexisting.patch"
rg -n 'async fn create_sandbox|connect_agent|monitor_vm|new_multi_thread|block_on' CubeShim hypervisor
git apply --stat "$SKILL_DIR/references/scheme1-rpc.patch"
git apply --check "$SKILL_DIR/references/scheme1-rpc.patch"
```

`--check` 成功只说明上下文可应用。核对代码已有连接复用、VM 失败清理、runtime 作用域后再实施；已有同类改动时保留，不重复 spawn。若目标曾使用同名历史补丁，先按内容哈希和最终语义识别版本，不再次叠加；本包的 JoinSet、shutdown/drain 与有界退避是同一修订实现的一部分。若不适用，按 agent-preconnect.md 的函数映射迁移，不用 `--reject` 留半应用代码后直接构建。

## 2. 从代码重建调用链

沿 `create_sandbox → start_vm → restore → connect_agent → Guest 初始化 → monitor_vm` 阅读。为每个实例记录以下单调时钟时间：restore 起止、vsock 可连接、CONNECT 完成、agent 安装、Guest 初始化完成、health 首次成功。关联 sandbox ID 与本次 generation，禁止把另一实例的时间拼入同一条链。

| 成本 | 当前实现要回答的问题 | 修改后的观测 |
|---|---|---|
| 预连接 | 连接任务最早何时能真正成功？是否必须等 Guest vCPU？ | CONNECT 与 restore 有实际重叠，汇合等待减少 |
| monitor 建连 | 业务连接仍有效时是否第二次建连？ | 第二次握手减少，health 与业务均正常 |
| runtime | 每 restore 都构造吗？一个进程能复用几次？ | 构造次数下降，线程/FD 稳定 |

不要用三个 RPC 的总时长推算预连接收益：SetGuestDateTime、RNG 和 CreateSandbox 在历史 patch 中没有一起并行化。

## 3. 分三步实施

**连接安装**：抽出 `install_agent_conn(Client)`，统一设置 `self.conn` 与 `self.client`；同步路径仍调用原连接函数后安装。避免 client 已替换而 conn 仍指向旧实例的半更新状态。

**预连接**：在 `start_vm` 前创建局部 `JoinSet` 并启动一次有界任务，不能用裸 `JoinHandle`。本包参考补丁已修正：恢复失败执行 `shutdown().await` 后返回原错误；连接超时/失败则先 shutdown 并等待，再走原串行回退。后续 `?` 和请求取消会 Drop 集合并 abort 子任务；Drop 无法同步 await，运行时在下一次调度完成回收。连接已成功但恢复失败时，集合中的未消费连接也应释放。

汇合代码先执行 `let joined = timeout_at(join_deadline, preconnect.join_next()).await;`，再 `match joined`；将等待结果与分支处理分成两条语句，失败分支继续在串行建连前执行 `shutdown().await`。修改后运行下述回归用例，确认从补丁抽取的同一段代码编译成功，并通过全部 12 项生命周期测试。

重试从 1ms 指数退避到最多 50ms；整个连接 future 使用起始时刻的 30s 绝对 deadline，覆盖单次挂起 connect。restore 后等待使用原 deadline 与“当前时刻 +10s”的较早值。当前接口没有显式请求 deadline，依靠父 future 的取消绑定生命周期；迁移到提供请求预算的接口时，接入更短的请求 deadline，并让串行回退使用同一预算。

**连接/runtime 复用**：monitor 优先 clone 已安装的可并发传输，缺失时保留原路径。runtime 按目标进程复用，线程数首次初始化规则写清楚；不要在已经运行的 Tokio executor 内同步等待同一 executor。三个机制分别保存补丁或临时开关，便于回退与归因。

## 4. 构建、发布与就绪

```bash
set -euo pipefail
cd "$SOURCE_ROOT/CubeShim"
cargo build --release --locked > "$RUN_DIR/build/shim.log" 2>&1
```

只有退出码为 0 才继续。通过 workspace 配置确认内嵌 VMM 被重建；记录实际 shim 产物 SHA256。由 operator 从目标部署配置取得 `SHIM_BINARY_PATH`，通过运行时环境变量或发布参数提供实际绝对路径，按已有发布流程备份并在停止测试创建入口后替换。不要杀掉正在承载非测试实例的旧 shim；新建实例是否使用新 shim 需单独检查 `/proc/<new-pid>/exe`。

启动后先运行一次 c1 创建，确认新进程哈希及 agent ready/health；再做 20 次 create/list/delete。性能测试按本地 [reproduction.md](reproduction.md) 执行。只改磁盘文件但继续测旧 shim 进程，结果无效。

## 5. 验证顺序与故障定位

| 步骤 | 操作 | 通过条件 | 失败后优先查 |
|---|---|---|---|
| 正常路径 | c1 与 20 次生命周期 | Guest 可执行、查询存在、删除后资源释放 | client/conn 安装、实例身份 |
| restore 失败 | 在连接成功前后各注入一次 VM 失败 | 原错误被保留，任务/连接已回收 | JoinSet 所有权与显式 shutdown |
| 取消 | retry/CONNECT/join 时取消请求 | 不超整体 deadline，无持续 retry | connect 是否可取消、join 清理 |
| monitor | health 与创建 RPC 并发 | 无连接被意外关闭或协议串扰 | Client clone 的传输语义 |
| runtime | 连续 restore 后观察线程/FD | 数量不随请求持续增加 | runtime 实际作用域与关闭 |
| 性能 | base/preconnect-only/reuse-only/all | 分别记录总时间、阶段与长尾 | 可重叠窗口、调度和计时边界 |

阶段日志本身过密也会影响 c100。基线与候选使用相同日志等级，必要时另设诊断轮采细节，正式轮保持一致。

迁移前先在本 skill 目录执行 `python3 cases/preconnect-lifecycle/run.py`：该用例直接从 patch 提取实际预连接代码，在锁定 Tokio 依赖与虚拟时钟下验证失败、取消、deadline 和 c100 失败突发。它会编译并执行补丁代码，应在无生产凭据的隔离测试环境运行；首次取包按受控 registry 配置完成，准备齐依赖后再离线复验。执行时不要把 mock 连接/VM 的通过结果当成真实 Guest 或完整 Shim 编译已通过；目标侧继续做上表集成验证。

## 6. 结果与回退

报告写明三种机制分别是否启用、CONNECT 重叠窗口、runtime 构造次数、创建总时间下降率及长尾变化。若总时间无显著变化但长尾改善，分别报告，不改变主指标。

失败时回退本次最小补丁或备份产物，经新实例确认旧哈希与健康恢复。不要用 `git reset --hard` 覆盖用户已有工作。统计用本包 `scripts/plan_comparison.py` 和 `scripts/compare_create_time.py`；本包测试 `python3 cases/comparison-contract/test_compare.py` 不需要其他 skill。
