# 基准:构建、测量协议、消融设计、报告

Phase 1/5/6 使用。核心纪律:**交替、多轮、CV 门槛、一次只切一个变量**。

## 1. 构建(先 KDNN 后 ORT,顺序不能反)

KDNN(必须 THREADPOOL 运行时;ENABLE_ASAN/GCOV/HBM 必须显式传值,旧 CMake 空变量会配置失败):

```bash
cmake -S onnxruntime/core/kdnn/src/dnn -B onnxruntime/core/kdnn/build_kp950 -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$PWD/onnxruntime/core/kdnn/out" \
  -DTARGET_PLATFORM=KP950 -DKDNN_CPU_RUNTIME=THREADPOOL \
  -DENABLE_ASAN=off -DENABLE_GCOV=off -DENABLE_HBM=off
cmake --build onnxruntime/core/kdnn/build_kp950 --parallel 48
cmake --install onnxruntime/core/kdnn/build_kp950
```

ORT(`--cmake_extra_defines` 的多值必须紧跟该 flag,不能放别的 flag 后面):

```bash
./build.sh --config RelWithDebInfo --build_shared_lib --parallel 48 \
  --compile_no_warning_as_error --skip_submodule_sync \
  --cmake_extra_defines onnxruntime_USE_KDNN=ON \
  CMAKE_DISABLE_FIND_PACKAGE_protobuf=TRUE CMAKE_DISABLE_FIND_PACKAGE_Protobuf=TRUE \
  --allow_running_as_root --skip-keras-test --skip_onnx_tests --skip_tests \
  --build_dir kdnn
```
(`CMAKE_DISABLE_FIND_PACKAGE_*` 仅在系统 protobuf CMake 配置不完整的环境需要。)

**离线环境**(无外网时 FetchContent 依赖拉不下来):用 `FETCHCONTENT_SOURCE_DIR_<DEP>=<已 populated
的源码目录>` 覆盖,依赖名以现有构建的 `CMakeCache.txt` 里 `FETCHCONTENT_SOURCE_DIR_*` 条目为准
(本仓库 15 个:ABSEIL_CPP/DATE/EIGEN3/FLATBUFFERS/GOOGLETEST/GOOGLE_BENCHMARK/GSL/KLEIDIAI/
MP11/NLOHMANN_JSON/ONNX/PROTOBUF/PYTORCH_CPUINFO/RE2/SAFEINT)。已配置过的 build 目录直接
`cmake --build kdnn/RelWithDebInfo --target <target>` 增量编译即可。

benchmark harness(独立 project,只链 libonnxruntime.so):

```bash
cmake -S onnxruntime/test/alipay/benchmark/cpp -B build/alipay_benchmark -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DONNXRUNTIME_ROOT="$PWD" \
  -DONNXRUNTIME_BUILD_DIR="$PWD/kdnn/RelWithDebInfo" \
  -DCMAKE_EXE_LINKER_FLAGS="-Wl,--allow-shlib-undefined"
cmake --build build/alipay_benchmark --parallel 16
```

运行时 `LD_LIBRARY_PATH` 必须同时含 ORT build 目录与 `onnxruntime/core/kdnn/out/lib`。

## 2. 测量协议(每一项都是硬性要求)

1. **固定机器状态**:同一台机、同一 NUMA 绑定(`numactl -N <node>` 或明确 CPU 列表)、记录
   CPU/NUMA/物理核还是 SMT。鲲鹏类平台 SMT=2,**绑独立物理核(偶数 CPU 列表)与绑逻辑核差
   8%+**,这是单项最大噪声源之一。
2. **固定 ORT 状态**:execution mode(推荐 sequential;parallel 在单逻辑流下有两套线程池争核的
   历史问题)、inter=1、intra=N、spin 一致(要么都默认要么都禁)、warmup=10、iter=100。
3. **交替执行 ≥3 轮**:A,B,A,B…(奇偶轮可以正逆序),不要先跑完 A 再跑 B。每轮取 iteration
   median,跨轮取 median(median-of-medians)。
4. **CV 门槛**:记录 mean/median/p95/stddev/CV;**CV>5% 的轮次作废重跑,不许挑值**。差值小时
   (如 <2%)补五轮复验。
5. **A/B 隔离**:对比某优化时,把其他优化 gate 显式归零(如 `ORT_ENABLE_FUSED_TENSORDOT_MATMUL=0
   ORT_KDNN_FUSE_STACK=0 ORT_KDNN_ELIM_NOOP_TRANSPOSE=0`),只留被测变量。
6. 用 `scripts/run_ab_benchmark.sh` + `scripts/aggregate_results.py` 固化上述纪律,避免口径漂移。

benchmark harness 关键 flag:`--model-path --batch-size --intra-op-threads --inter-op-threads
--warmup-iter --num-iter --execution-mode sequential|parallel --optimized-model-path
--enable-profiling`(harness 自动生成固定 seed 的随机输入,逐 iteration 计时,输出
mean/median/p95/stddev/CV/throughput)。

## 3. 消融矩阵设计

固定测量协议,只切一个变量,推荐矩阵:

- **算法**:classic vs no-pack(你的两条实现路径);
- **联合 kernel**:off/qk/pv/all(若实现了联合 GEMV);
- **cache**:0 vs 默认容量;
- **batch**:1 / 32 / 128(decode 场景 batch 是主要伸缩维);
- **线程**:8C16T / 16C32T 等代表性配置。

单算子 vs 端到端**都要测**。单算子 bench 注意:外层 worker 数与 ThreadpoolIface 线程数要一致,
每个 worker 内 GEMV 固定单线程,避免嵌套并行假象。

## 4. 期望管理(历史数据锚点)

同一模型(model4,108 机器,16C32T,sequential,全图优化开):
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
- 注意 profiling 本身有 19~28% 的开销,**只做结构比较,绝对值不作数**;
- 验证开关真的命中执行路径(如 cache 的 hits/bypasses 计数),否则"A/B 差异 ≈ 0"可能只是开关
  没生效;
- 排查并行收益:先看 trace 里是否存在真实节点重叠;没有重叠就别开 inter-op 并行。
