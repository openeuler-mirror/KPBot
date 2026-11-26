# rootfs 影子池操作手册

## 1. 确认存储对象与能力

本方案要求不可变模板 rootfs、可领养的独立快照对象、支持原子 rename 的同文件系统布局，以及引擎索引与持久化协作。确认当前使用 reflink/XFS 后端；其他后端没有实现新接口时必须回退，不能只添加 Go 方法名。

```bash
set -euo pipefail
cd "$SOURCE_ROOT"
mkdir -p "$RUN_DIR/source" "$RUN_DIR/build" "$RUN_DIR/reports"
git rev-parse HEAD > "$RUN_DIR/source/commit.txt"
git diff --binary > "$RUN_DIR/source/preexisting.patch"
rg -n 'CreateSandboxRootfsFromTemplate|CreateSnapshotFromVolume|ensureSnapshotOrigin|DeleteByKind' Cubelet
rg -n 'rename_snapshot|fsync_snapshot_dir|trait Engine|fsync_dir' cubecow
git apply --stat "$SKILL_DIR/references/scheme3-pool.patch"
git apply --check "$SKILL_DIR/references/scheme3-pool.patch"
```

`SOURCE_ROOT`、`SKILL_DIR`、`RUN_DIR` 使用实际绝对路径。先备份 gitignored 的 cubecow 头/库，保留当前源码修改。读 implementation.md 的状态与故障说明后再应用或迁移。

## 2. 按完整调用链实施

| 层 | 修改和检查 |
|---|---|
| `rootfs_pool.go` | 每模板池、成员预留、单 worker 构建、flush 队列、关闭与清理 |
| `cubecow_volume_manager.go` | 原派生前试领养；创建 miss 回退；模板删除清池 |
| `pkg/cubecow` | Go 方法、C 参数/字符串释放、native 与 stub 接口一致 |
| `cubecow.h` / `ffi.rs` | 两个新 C ABI 导出、空指针/错误码/panic 处理 |
| `Engine` / `ReflinkEngine` | 默认不支持回退、rename 冲突检查、文件与索引原子协议、目录 fsync |

前台路径是 `reserve member → rename → 检查/扩容 → 发布 rootfs`。reserved 成员不能提前被补池器当成空槽；rename 后失败需进入清理/恢复协议，不能直接再次派生覆盖目标。

后台 fill 只能从与池身份匹配的不可变模板派生。模板删除先关闭池、阻止新任务并等待在途任务，然后扫残留；重启时仅在验证 origin、模板版本和未使用状态后回收 AlreadyExists 成员。

flush 的“每窗口一次目录 fsync”是合并持久化，不是免持久化。严格成功应答等到 durable generation 覆盖本次 rename；异步应答必须声明崩溃窗口，失败重试与告警保持可观测。

## 3. 配置和容量

| 历史配置 | 值与含义 | 当前部署应确认 |
|---|---|---|
| `CUBELET_ROOTFS_POOL_SIZE` | 默认 480，正整数按模板维护；历史实现对非正数或非法值回落默认值 | 核对解析后的有效容量，不能用 `SIZE=0` 表示禁用 |
| `CUBELET_ROOTFS_POOL_DISABLE` | 非空即禁用，包括字符串 `0` | 启用时 unset，禁用时设 `1` |
| build stagger | 25ms，一次一个成员 | 派生耗时、锁竞争、补池吞吐 |
| flush delay | 20ms 合并窗口 | 持久化契约、fsync 时长、失败处理 |
| sweep limit | 默认 2048，目标更大时按实现扩展 | 历史水位和孤儿扫描范围 |

480 只能覆盖一定突发；每 25ms 一次加上派生耗时的补池速率低于 40/s。持续创建速率高于补池时会耗尽，必须正确回退。多模板可使后台任务相乘，资源预算不能仅按单池估算。

只有前两项是参考补丁读取的环境变量；build stagger、flush delay 和 sweep limit 是代码常量，调整它们需要形成新的源码/构建版本。环境变量必须进入实际 Cubelet 服务进程的启动配置，在交付记录中保留有效值；只在另一个终端 export 不会更新已运行的进程。既有池对象的目标水位也不能默认随配置变化自动扩缩。

## 4. 混合构建与发布

```bash
set -euo pipefail
cd "$SOURCE_ROOT"
(cd cubecow && cargo build --release --locked) > "$RUN_DIR/build/cubecow.log" 2>&1
mkdir -p Cubelet/third_party/cubecow/lib Cubelet/third_party/cubecow/include
cp cubecow/target/release/libcubecow.a Cubelet/third_party/cubecow/lib/
cp cubecow/include/cubecow.h Cubelet/third_party/cubecow/include/
sha256sum cubecow/target/release/libcubecow.a Cubelet/third_party/cubecow/lib/libcubecow.a > "$RUN_DIR/build/library-hashes.txt"
(cd Cubelet && CGO_ENABLED=1 go build ./... && CGO_ENABLED=1 go build -o cubelet ./cmd/cubelet) > "$RUN_DIR/build/cubelet.log" 2>&1
```

逐条确认成功后继续，库的两个哈希应相同。记录头文件与最终 Cubelet 哈希。部署前由 operator 从已核验配置提供 `CUBELET_BIN=<cubelet-binary-path>`；使用 systemd 时还需提供 `CUBELET_UNIT=<cubelet-systemd-unit>`，并以环境变量传入当前会话。占位符仅说明参数含义，不能原样用于操作；不提供猜测的默认路径或服务名。输入校验和只读核对命令见本地 [reproduction.md](reproduction.md)。

按以下顺序使用目标已有发布机制：核对服务 ExecStart 与 `CUBELET_BIN` → 将当前文件与哈希备份到本次受控 `RUN_DIR` → 停止对应服务/新请求 → 安装完整新产物 → 启动并等待就绪 → 从进程管理器取得新 PID，核对 `/proc/<pid>/exe` 与 `CUBELET_BIN` 的 SHA256。非 systemd 部署采用其实际进程管理器的等价操作。真实部署值只留在本次本地记录，公开分享时用变量名或占位符。undefined symbol/undefined API 优先检查是否重新生成并复制 Rust 静态库和头，而不是绕过 cgo 检查。

首次准备请求会触发历史实现建池。完成后清理测试实例，等待 `rootfs pool ready`，记录每模板水位及就绪时间。启动日志不等于池已满；首次填充成本单列。

把首次准备与正式采样分开记录：准备调用可能先走原派生，随后才有后台填充。每轮正式调用前重新确认同一模板与目标水位，不能沿用上一轮的 ready 日志。若目标版本没有直接查询水位的接口，可结合领养、补池事件重建计数，并注明估算方式；水位无法确认时记录为未知，不归入满池样本。

## 5. 正确性与故障矩阵

| 场景 | 验证内容 |
|---|---|
| 正常领养 | 创建、挂载读写、尺寸正确、查询和删除 |
| 池空/禁用 | 原派生可用，无新增错误，目标命名和返回语义一致 |
| 并发/重复请求 | 一个成员仅领养一次；同 sandbox/gen 重试按原契约处理 |
| rename 前失败 | 原对象仍可判定，回退不覆盖其他快照 |
| rename 后扩容失败 | 已命名对象被清理/登记恢复，不泄漏或重复发布 |
| fsync 失败 | 不错误宣称 durable，pending 状态可重试且可观测 |
| 模板删除与 fill/adopt 交错 | 关闭后无新成员，所有任务退出，无清理后再创建 |
| 重启/故障恢复 | 重建索引、孤儿验证和已领养对象归属正确 |

先对引擎/API 层用可控 mock 注入失败，再在目标文件系统验证重启和所需崩溃语义。进程重启不等于断电恢复，报告分别标注。

## 6. 三类性能与消融

1. **满池突发**：每次独立 c100/n100 调用前恢复同一水位；记录约 100 次领养是否发生、derive/FICLONE/fsync 次数与整批时间。
2. **冷池/耗尽**：从空池开始，直到命中转回退，记录错误率和延迟；不能从报告中删掉回退请求。
3. **持续负载**：运行到补池与消耗稳定或明确耗尽，报告创建和补池速率、I/O、CPU 与空间。

| 对照条件 | 调用前需要对齐 | 同轮记录 |
|---|---|---|
| 基线 / 候选关池 / 候选开池 | 模板内容、文件系统、二进制身份与实际池开关 | 成功/失败数、创建 `total_time`、领养与原派生次数 |
| 满池重复样本 | 相同初始水位、准备与清理规则 | 消耗成员数、补池工作及返回水位所需时间 |
| 冷池或持续负载 | 明确的初始水位与一致的运行时长/请求设计 | 命中转回退的时刻、消耗与补充速率、后台 CPU/I/O |

候选开池与关池使用同一二进制，池设置通过已核验的服务启动配置生效；与原基线比较时另外记录代码/构建差异。每次 c100/n100 调用都保留全部 100 个请求的成功和失败，按本地协议采集，不把回退请求从样本中剔除。`total_time` 下降率为 `100 × (base − candidate) / base`；准备填池、后台补池及持久化完成情况分别报告。

主比较为原基线 vs 候选开池，归因为同一候选二进制关池 vs 开池。候选关池若也快于基线，检查是否存在其他实现/构建差异，不能全归池。完整 ABBA 和工具命令在本地 [reproduction.md](reproduction.md)。

## 7. 回退

先禁用池，观察原派生恢复；停服前按持久化策略 drain，再通过同一已核验的 `CUBELET_BIN`、`CUBELET_UNIT`（或实际进程管理器配置）回退匹配的 Cubelet/静态库/头文件，重新取得 PID 并校验运行哈希。只清理本次确认的池成员，不能用泛化文件名匹配删除业务快照。保留原始日志与快照身份表，便于重启后的归属审计。
