# restore runtime 复用的实施与复现

## 诊断与约束

在 `hypervisor/vmm/src/device_manager.rs` 定位 `DeviceManager::restore_device_node`。基线按设备数量与 `MAX_WORKER_THREADS` 决定 worker 数，每次调用都 `build()` 一个多线程 runtime，再在 `block_on` 内按组恢复设备。先按 PID 与启动时间配对记录调用次数、实际 worker 数、构造时间和恢复时长，同时核对 `restore_devices` 调用上下文。若构造时间集中在无法复用的单次进程调用，就先处理实际热点。

复用将线程和 I/O 驱动的生命周期延长到进程。使用该 runtime 的调用需在允许阻塞的同步边界执行。若目标版本从 Tokio worker 内调用，应先确认并迁移到合适的阻塞边界，验证任务所需线程资源和栈深。记录新增长期线程、内存占用和并发 restore 时的排队。每次调用的设备数据、锁和快照仍属于本次调用。

## 实现策略

先确认目标树中 `restore_device_node` 的实现。若已采用 `scheme1-rpc.patch` 的 `OnceLock`，进程内后续恢复会沿用首次配置，首次建池使用 `build().unwrap()`。worker 数固定且能保证建池资源时可选该实现。需要在同一进程切换 worker 数或将建池失败传回调用方时，选择下述独立补丁。

两个补丁都修改 `hypervisor/vmm/src/device_manager.rs` 的同一处。若同时需要方案一的 CubeShim 改动，从干净的 v0.7.1 工作树开始，将 `SCHEME1_PATCH` 设为方案一补丁的绝对路径。在源码根目录先执行以下命令，再按下文应用独立补丁并构建。已应用完整方案一时，在单独的干净工作树按此顺序评估。

```bash
git apply --check --exclude='hypervisor/vmm/src/device_manager.rs' "$SCHEME1_PATCH"
git apply --exclude='hypervisor/vmm/src/device_manager.rs' "$SCHEME1_PATCH"
```

参考补丁基于 CubeSandbox `v0.7.1`（`31d911e430fdf8a8879bd062b8e78066c1a8e89d`）。进程级 `RwLock` 槽位一次保存一个设备恢复 runtime。匹配 worker 数的恢复在读锁内共享池。配置变化需要写锁，等待正在使用旧池的调用结束，先销毁旧池，再构建新池。不同配置的恢复因此可能等待，测量混合负载时单独记录切换等待。线程创建出错返回 `MigratableError::Restore`，槽位保持空闲，下一次恢复可按需要重试。该预算只覆盖设备恢复池，验收时还需计入进程和同一 cgroup 的其他线程。

共享 runtime 在某次调用失败后仍存活，因此恢复任务要由调用者负责收尾。补丁先收集并校验本设备组所需的快照，再提交组内任务。遇到恢复错误或 join 错误时，取消尚可取消的任务并等待余下任务结束，再返回错误。设备恢复函数可以同步执行，已经运行的同步工作可能需要自然结束，`abort` 无法中断它。一个组成功完成以后才进入下一个组。join 错误作为恢复错误向上传递。

在隔离源码工作树执行，`SOURCE_ROOT` 与 `SKILL_DIR` 使用本机绝对路径。

```bash
cd "$SOURCE_ROOT"
git rev-parse HEAD
git apply --stat "$SKILL_DIR/references/restore-runtime-reuse.patch"
git apply --check "$SKILL_DIR/references/restore-runtime-reuse.patch"
git apply "$SKILL_DIR/references/restore-runtime-reuse.patch"
(cd CubeShim && cargo build --release --locked)
```

以上构建适用于由 CubeShim 工作区链接 VMM 的版本。目标环境独立部署 VMM 时，按目标工作区构建并核对实际运行产物，同时核对 Rust 与 Tokio 多线程 runtime 的版本要求。

## 独立验证

使用 Python 3、Git 和 Rust/Cargo 执行包内回归。runner 应用随包补丁，用真实 Tokio 池执行实际恢复函数。测试夹具在构建入口施加三个 worker 的总预算，依次执行 2、2、3 worker 的恢复，确认两次构建且峰值为 3。随后压低预算，核对构建失败返回错误、旧池已释放、下一次小配置可以成功。离线运行可指定 `--vendor-dir /path/to/vendor`。

```bash
cd "$SKILL_DIR"
python3 cases/runtime-budget/run.py
```

Linux 目标验收使用已有隔离服务或测试 cgroup。先记录无恢复时的 `pids.current`、VMM 线程数和 `pids.max`，再用可容纳任一配置、无法容纳两池同时驻留的余量串行恢复两种配置。核对实际 worker 峰值和切换时间。再将预算降至不足以构建新池，确认错误可见且进程继续存活。放宽测试配额后验证下一次恢复。夹具预算覆盖构建和释放顺序，实际 OS 配额由这一步验证。

| 场景 | 验收点 |
|---|---|
| 同一进程连续多次相同 worker 数恢复 | 一次共享构造，后续成功恢复。记录首轮与后续构造/设备阶段耗时，线程和 FD 数稳定。 |
| 进程重启、空设备组、不同 worker 数 | 重启后独立初始化，空组不启动任务。切换配置前释放旧池，预算不足返回错误。 |
| 同组缺快照、设备恢复报错、任务 panic | 缺快照时本组未提交任务。已提交失败组的任务全部退出，下一组未开始。下一次恢复不受旧任务影响。 |
| 并发恢复、跨组设备依赖 | 同一调用内满足组间屏障，不同设备实例的数据互不污染。线程竞争下 p95 和失败率符合预算。 |

在相同模板、进程模式、工具链和恢复并发下比较 A（每次构造）与 B（可复用配置）。先单独记录冷启动，再为同进程复用场景做 A/B/B/A 交错采样。每次计量要求 100 次成功、0 错误。

v0.7.1 的 `RunWarmup` 使用同一运行模式，`create-only` 的内置 `-w 3` 会留下额外三个实例。两臂统一关闭内置预热。使用独占测试租户或环境，在每轮开始通过 List API 记录活跃 ID 全集和数量。若需三次预热，另开一次 `-c 1 -n 3 -w 0 -m create-only` 调用并单独保存结果。用调用前后的 List 差集记下预热 ID，逐个删除并确认活跃集合回到基线，保留被测 VMM 进程。然后执行计量，用同样的差集记录计量 ID 并逐个删除。两次清理和 List 均在计时窗口外；活跃集合或水位未恢复时暂停下一轮。若环境中有其他创建流量，改用隔离环境，避免将别人的实例纳入差集。

```bash
cube-bench -c 100 -n 100 -w 0 -m create-only --no-tui -o "$REPORT_JSON"
```

在目标 bench 核对参数，所有样本使用同一明确单位的 `summary.total_time_s` 或 `summary.total_time_ms`。按组分别取 A、B 中位数，用 `100 × (A − B) / A` 计算整批创建时间下降率。记录每组结果与波动；同时报告同进程命中率、构造次数、设备恢复时长和线程预算。使用基线产物回退并重新执行一次设备恢复，核对运行哈希与设备状态。
