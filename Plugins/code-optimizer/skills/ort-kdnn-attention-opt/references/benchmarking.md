# 基准:构建、测量协议、消融设计、报告

构建与测量时按需使用。核心是可复现对照、明确统计口径和支持结论的证据；配置、轮数和工具随目标负载选择。

## 1. 构建与环境配置

先确认当前分支是否集成 KDNN。下面参数来自定制分支，执行前核对 CMake options 和 `build.sh --help`；上游不保证提供这些开关。若已有可复现构建，优先复用其流程。

使用者根据实际环境设置以下变量，不推断个人目录或机器编号：

| 变量 | 含义 |
|---|---|
| `ORT_SOURCE_DIR` / `KDNN_SOURCE_DIR` | 两个项目的源码根目录 |
| `ORT_BUILD_DIR` | 含 libonnxruntime 的实际配置目录 |
| `ORT_BUILD_ROOT` | 传给 ORT build.sh 的构建根目录（可能自动追加配置子目录） |
| `KDNN_BUILD_DIR` / `KDNN_INSTALL_DIR` | KDNN 构建与安装目录 |
| `TARGET_PLATFORM` / `BUILD_JOBS` | 当前版本支持的目标平台与构建并行预算 |
| `BENCH` / `MODEL_PATH` / `RESULTS_DIR` | 已有 benchmark 程序、模型和本次结果目录 |

KDNN 示例（目标平台、runtime 和开关以当前库支持为准）：

```bash
cmake -S "$KDNN_SOURCE_DIR" -B "$KDNN_BUILD_DIR" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$KDNN_INSTALL_DIR" \
  -DTARGET_PLATFORM="$TARGET_PLATFORM" -DKDNN_CPU_RUNTIME=THREADPOOL \
  -DENABLE_ASAN=off -DENABLE_GCOV=off -DENABLE_HBM=off
cmake --build "$KDNN_BUILD_DIR" --parallel "$BUILD_JOBS"
cmake --install "$KDNN_BUILD_DIR"
```

ORT 侧将库的 include/lib 路径映射到当前分支实际支持的 CMake 参数，再调用该分支的 build.sh。不要仅设置 `onnxruntime_USE_KDNN=ON` 就假定库路径已经正确；检查 CMakeCache、链接命令及实际加载库。仅在系统 protobuf 配置损坏时考虑禁用对应 find_package，不默认允许未解析链接符号或 root 构建。

离线构建可用 `FETCHCONTENT_SOURCE_DIR_<DEP>` 指向匹配锁定版本的依赖源码；依赖名取自当前 CMake 配置，不固定依赖列表。已有构建可用 `cmake --build "$ORT_BUILD_DIR" --target <target>` 增量编译。

benchmark harness 不随本 skill 提供。先核对其 `--help`；附带 runner 适配下文列出的 flags，其他 harness 需适配参数。

```bash
export LD_LIBRARY_PATH="$ORT_BUILD_DIR:$KDNN_INSTALL_DIR/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
# 在 skill 根目录执行；目录与输入变量应已设置。
bash scripts/run_ab_benchmark.sh "$BENCH" "$MODEL_PATH" 32 3 "$RESULTS_DIR" \
  --env-a 'ORT_KDNN_FUSE_ATTENTION=0' --env-b 'ORT_KDNN_FUSE_ATTENTION=1'
```

## 2. 测量协议（预先确定参数与噪声门槛）

1. **固定机器状态**:同一台机、同一 NUMA 绑定(`numactl -N <node>` 或明确 CPU 列表)、记录
   CPU/NUMA/物理核还是 SMT。用 `lscpu -e=CPU,CORE,SOCKET,NODE` 核实拓扑；不要假定 SMT=2 或偶数 CPU 就是独立物理核。
2. **固定 ORT 状态**：单变量实验保持 execution mode、线程、spin 和测量窗口一致；调度本身是候选时可改变并明确归因。runner 的 sequential/inter=1/warmup=10/iter=100 是示例配置；按实际负载选择，不能据此排除 parallel。
3. **交替执行 ≥3 轮**:A,B,A,B…(奇偶轮可以正逆序),不要先跑完 A 再跑 B。本工具取每轮 mean，再取跨轮 median（median-of-round-means）；不是 iteration median 或全局 p95。需要分位数时另保存原始样本。
4. **CV 门槛**:记录 mean/median/p95/stddev/CV;预先约定 CV 门槛（默认跨轮 mean 的 CV≤5%）；超标保留全部日志并调查重跑，不挑值。差值小时
   应增加样本或改善隔离，并检验配对差异的不确定性；CV 达标本身不证明差异显著。
5. **A/B 隔离**:对比某优化时,把其他优化 gate 固定为相同值（仅做裸路径消融时归零）(如 `ORT_ENABLE_FUSED_TENSORDOT_MATMUL=0
   ORT_KDNN_FUSE_STACK=0 ORT_KDNN_ELIM_NOOP_TRANSPOSE=0`),只留被测变量。
6. 可选用附带 runner/aggregator；不匹配 harness 或执行模式时沿用现有工具，保持指标可比。

benchmark harness 关键 flag:`--model-path --batch-size --intra-op-threads --inter-op-threads
--warmup-iter --num-iter --execution-mode sequential|parallel --optimized-model-path
--enable-profiling`(harness 自动生成固定 seed 的随机输入,逐 iteration 计时,输出
mean/median/p95/stddev/CV/throughput)。

## 3. 消融矩阵设计

固定对照条件，选择与实现有关的维度；组合依赖可整体比较再消融：

- **算法**:classic vs no-pack(你的两条实现路径);
- **联合 kernel**:off/qk/pv/all(若实现了联合 GEMV);
- **cache**:0 vs 默认容量;
- **batch**:1 / 32 / 128(decode 场景 batch 是主要伸缩维);
- **线程**:8C16T / 16C32T 等代表性配置。

端到端任务以端到端指标验收，单算子测量用于解释机制；局部 kernel 任务可仅报告其验证范围。记录外层 worker 与库线程配置，避免未说明的超订。

## 4. 期望管理(历史数据锚点)

同一历史模型（attention-model-A，测试主机 A，16C32T，sequential，全图优化开）:
- 单算子 B128 加权:classic 11.57ms → 通用 no-pack 8.51ms(+26%)→ 联合 no-pack 5.18ms(+55%);
- **端到端 B128**:classic+cache 39.8ms → 联合 no-pack+cache 39.2ms(**+1.7%**);
- cache 本身端到端 +1.7~4.6%;融合(含消除 Transpose 链)是最大单项。

原因:attention 本体只占端到端 ~11%(融合后口径),单算子加速按占比折算。**报告两端数字**,
不要只报单算子。

## 5. 报告模板

```markdown
# <模型> attention 优化报告
## 1. 结论摘要(一段话 + 主表)
| 配置 | B1 | B32 | B128 | (mean/median/p95/CV, ms) |
## 2. 机器与协议
CPU/NUMA/绑核、ORT 配置、warmup/iter、轮次、CV 判定、构建 commit
## 3. 优化内容(逐项:动机 → 设计 → 数字)
每项:解决什么问题(引用 profile 占比)、设计要点、单算子数字、端到端数字、A/B 正确性结果
## 4. 消融矩阵
## 5. 边界与不成立条件
哪些结论只在 (Sq=1, H=4, d=32, 本机) 成立;Sq>1 时怎么办;其他模型的适用性判断
## 6. 验证证据清单(对应 validation.md 各层,逐条 ✓ + 数字)
## 7. 放弃的方向及原因
```

## 6. 性能归因技巧

- 开 `--enable-profiling` 拿 chrome trace,按节点/算子类聚合耗时占比(对比优化前后两份 trace);
- 历史 profiling 开销为 19~28%，当前开销需测量；使用诊断数据定位热点，以关闭诊断的性能轮评估收益;
- 验证开关真的命中执行路径(如 cache 的 hits/bypasses 计数),否则"A/B 差异 ≈ 0"可能只是开关
  没生效;
- 排查并行收益:先看 trace 里是否存在真实节点重叠;当前无重叠时检查图依赖和执行配置，再判断是否值得探索并行。
