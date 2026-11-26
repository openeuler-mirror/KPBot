---
name: openzl-bench-protocol
description: >
  工作场景：为 OpenZL 建立可复现的性能基线与调优证据——unitBench 构建与运行、自定义场景注册
  （KV Cache / 新 codec / kernel 与 graph 两类）、熵模式切换、真实数据准备（torch .pt → raw .bin
  附分布统计）、绑核与测量口径、多机部署复测、topdown TMA 采集。配套脚本 bench_kv.sh、
  td_loop.sh、prepare_kvdata.py。不适用：非 OpenZL 项目的基准测试；只解释 benchmark 代码不运行。
---

# OpenZL 基准测试工程协议

为 OpenZL 性能工作提供可信的测量基础设施：同一数据、同一构建、同一绑核口径下的吞吐与压缩率，以及供归因用的 topdown 证据。本 Skill 是 `openzl-compression-tuning` 的测量底座，也独立用于基线建立与回归检测。

## unitBench 协议（首选基准）

轻量场景基准，Make 构建，是调优闭环（改动 → 复测）的默认工具。

```sh
make unitBench                 # 自动拉取并编译 deps（zstd、lz4）
./unitBench --list             # 列出全部场景（90+）
./unitBench -h                 # 用法
```

常用选项：

| 选项 | 语义 | 调优用法 |
|------|------|---------|
| `-i=<secs>` | 每文件测试时长（内部循环至耗尽） | 稳态测量，默认 2s 起步 |
| `-p=<mode>` | 场景参数（KV 场景为熵模式） | 0=Huffman-token、1=FSE、2=自动选型、3=multi 分块 |
| `--csv` | CSV 输出 | 脚本解析 |
| `-z` | 仅压缩不解压 | 单方向测量 |
| `-B=<bytes>` | 按块切分输入 | 块大小敏感性 |
| `--save-result` | 保存首个产物 | 位流级对比 |

**场景两类语义**（注册自定义场景前先明确）：

- **kernel 场景**（func 型）：直接测内核函数，Encode 场景只计编码方向（decode 标 N/A），Decode 场景的 preparation 编码不计时；
- **graph 场景**（graphF 型）：完整引擎 round-trip，自动选型 + decoder fusion，自动逐比特校验无损并输出压/解双向吞吐。

**round-trip 校验语义**：graph 场景内建校验；kernel 场景 Decode 的 preparation 产物与输出 memcmp。自定义场景必须保留校验路径——吞吐数字没有正确性背书不可用。

**注册自定义场景三步**（新增 codec/数据格式的基准）：

1. `benchmark/unitBench/scenarios/codecs/<name>.c` + `.h`：实现 encode wrapper、encode bound、decode preparation、decode wrapper、decode outSize 五函数；
2. `benchmark/unitBench/benchList.h`：include 头文件 + scenarioList 追加条目（`UNITBENCH_DIRS` glob 自动纳入编译）；
3. `make unitBench && ./unitBench --list | grep <name>` 确认注册。

wrapper 设计要点：preparation 做一次性工作（编码、头部写入），wrapper 只含被测路径；循环内**零分配**（每迭代 malloc 的页故障可占 8~15% 内核时间，见 `openzl-compression-tuning` §Core findings 7）；输出头部带模式字段，encode/decode 两侧 `-p` 必须一致。

## openzl_benchmark（CMake · Google Benchmark）

全量套件（500+ 用例，E2E 与微基准），用于广度回归与横向对比：

```sh
cmake -B build-bench -DOPENZL_BUILD_BENCHMARKS=ON -DOPENZL_BUILD_TESTS=ON \
      -DOPENZL_BUILD_PYTHON_EXT=OFF -DOPENZL_BUILD_PYTHON_DEMO=OFF \
      -DOPENZL_BUILD_PARQUET_TOOLS=OFF -DCMAKE_BUILD_TYPE=Release
cmake --build build-bench --target openzl_benchmark -j$(nproc)
./build-bench/benchmark/openzl_benchmark --benchmark_filter='E2E .*Entropy.*' \
    --benchmark_min_time=0.2 --benchmark_out=result.json
```

已知坑：

- 不要加 `-DOPENZL_BUILD_CLI=OFF`（logger target 缺失导致配置失败，`tools/CMakeLists.txt` 已知问题）；
- SAO 系列需要 `silesia/sao` 语料（官方站已失效，用 GitHub 镜像 SilesiaCorpus），缺失只是跳过注册；
- 用例名带空格，filter 正则用整段子串；
- 长跑偶发进程消失（宿主机硬件错误/IO 异常）时分段 filter + 失败重试，按 benchmark 名合并去重。

## 数据准备（scripts/prepare_kvdata.py）

真实分布数据是结论有效的前提。把 torch `.pt` KV Cache 转为 raw 字节流并输出**决定熵 codec 选型的分布统计**：

```sh
python3 scripts/prepare_kvdata.py qwen_test_kv.pt --n-true 113541120   # BF16（截掉 repeat 填充）
python3 scripts/prepare_kvdata.py qwen_test_kv_e4m3.pt --dtype e4m3    # FP8 E4M3
```

**安全前置**：`.pt` 是 pickle 格式，脚本固定以 `torch.load(..., weights_only=True)` 加载（仅反序列化纯张量，拒绝任意对象），并对 torch < 2.6 直接报错退出（旧版默认 `weights_only=False`，安全性随机器上的 torch 版本漂移）。即便如此，输入 `.pt` 仍只能来自本机可信产出或已校验哈希的来源，禁止对来路不明的下载文件直接执行本脚本。

产出：全量 `.bin` + 8MB 快速验证切片（默认 `--slice-mb 8`）+ 统计（指数 top-k 覆盖率、边际熵、成对联合熵）。

**文件名契约**（三脚本统一按此推导：全量 `<stem>.bin`、切片 `<stem>_<N>MB.bin`，stem = 输入 `.pt` 文件名去扩展名，N = `--slice-mb`）：

| 输入 .pt | 格式 | 全量 | 8MB 切片（默认） | 128MB 切片（`--slice-mb 128`） |
|----------|------|------|------------------|-------------------------------|
| `qwen_test_kv.pt` | bf16 | `qwen_test_kv.bin` | `qwen_test_kv_8MB.bin` | `qwen_test_kv_128MB.bin` |
| `qwen_test_kv_e4m3.pt` | e4m3 | `qwen_test_kv_e4m3.bin` | `qwen_test_kv_e4m3_8MB.bin` | `qwen_test_kv_e4m3_128MB.bin` |
| `qwen_test_kv_e5m2.pt` | e5m2 | `qwen_test_kv_e5m2.bin` | `qwen_test_kv_e5m2_8MB.bin` | `qwen_test_kv_e5m2_128MB.bin` |

`bench_kv.sh -s 8MB` 与 `td_loop.sh --slice <N>MB`（默认 8MB，与 `--slice-mb` 默认值对齐）按同一规则在 `KVDATA_DIR` 内定位文件；td_loop.sh 请求的切片缺失时回退全量并输出 WARN（非静默）。

统计的意义：

- **top-k 覆盖率 / 熵**决定根表命中率与 Huffman 码长分布，是解码优化收益的先验；
- **成对联合熵 < 边际熵**说明相邻元素相关，token 打包（两两组码）可超"边际熵上界"——评估压缩率上界时用联合熵，不要误判超上界为错误；
- 合成随机数据（均匀分布）对熵 codec 是最坏输入，不能代替真实语料下结论。

## 测量口径与噪声控制

1. **预热与时长**：区分冷启动与稳态；`-i=2` 起步，小收益结论需更长时长与多次重复。
2. **绑核口径**：先探测 SMT 拓扑再绑定物理核线程 0（不同机器兄弟核编号规律不同：有相邻编号与间隔编号两种）：

```sh
lscpu | grep -E "Architecture|CPU\(s\)|Model name|Thread|Core|Socket|NUMA"
cat /sys/devices/system/cpu/cpu0/topology/thread_siblings_list
taskset -c <物理核列表> ./unitBench -i=2 <scenario> <file>   # 单线程基准 taskset 只统一口径，不提供并行度
```

3. **反序复测**：小收益（<5%）用 AB/BA 或等价交错顺序；方向不一致默认判噪声。
4. **共享机器**：确认同机用户负载，个别慢用例低负载复测；全量长跑分段执行。
5. **压缩率与吞吐分开记录**：压缩率与机器/绑核无关（确定性），三机不一致即实现有非确定路径。

## Topdown TMA 采集（scripts/td_loop.sh）

热点负载循环（unitBench `-i` 持续语义，零进程重启开销），供 topdown 工具包裹：

```sh
# AMD Zen（perf 原生 TMA metric）
taskset -c 4 perf stat -M frontend_bound,backend_bound,bad_speculation,retiring \
    scripts/td_loop.sh --cpu 4 --duration 20 --scenario decompress --format e4m3

# Kunpeng（鲲鹏 TMA 脚本 -c 模式，-s/-e 与 --cpu 一致）
bash <TMA脚本路径> -s 4 -e 4 -c "scripts/td_loop.sh --cpu 4 --duration 20 --scenario compress"
```

判读指引：retiring 低 + backend_bound 高 → 访存/执行延迟（查表布局、依赖链）；frontend_bound 高 → 取指/分支（展开、块恒等查表）；bad_speculation 高 → 分支误预测。topdown 结论与 perf stat IPC/L1-miss 交叉验证后再定优化方向（详见 `openzl-compression-tuning`）。

## 脚本清单

| 脚本 | 用途 |
|------|------|
| `scripts/prepare_kvdata.py` | .pt → .bin 转换 + 8MB 切片 + 指数分布统计（top-k / 边际熵 / 联合熵） |
| `scripts/bench_kv.sh` | KV 场景吞吐 + 压缩率表（bf16/e4m3/e5m2 × 熵模式 × 绑核），缺数据/场景自动跳过 |
| `scripts/td_loop.sh` | topdown 采集热点负载（compress/decompress × 格式 × 单轮时长/多轮） |

三脚本共享约定：`KVDATA_DIR`（默认 `$HOME/kvdata`）与 `UB`（unitBench 路径，默认 `./unitBench`）环境变量；数据文件名遵循「数据准备」节的文件名契约表（`<stem>.bin` / `<stem>_<N>MB.bin`），bench_kv.sh 与 td_loop.sh 按同一规则推导，无需手工重命名。

## Anti-patterns

| # | 反模式 | 正确做法 |
|---|--------|---------|
| 1 | 拿合成随机数据测熵 codec | 真实语料或按统计匹配的分布 |
| 2 | kernel 场景每迭代 malloc 大缓冲 | preparation 一次性分配或跨迭代复用 |
| 3 | 不绑核直接比较跨机数字 | 探测拓扑后统一物理核口径 |
| 4 | 传输源码树带预编译依赖产物 | 排除 `*.a`/`*.so*`，远程重新构建 |
| 5 | 全量单次长跑遇中断即作废重跑 | 分段 filter + 失败重试 + 按名合并 |
| 6 | 压缩率与吞吐混在一张结论里 | 分开记录：压缩率应机器无关 |
| 7 | encode/decode 两侧 `-p` 不一致 | 模式字段写入头部，两侧对齐 |
| 8 | 用 `-p` 单模式结论替代选型 | 全模式（0~3）+ 编码/解码两方向实测 |

## References

- [multi-machine-deployment.md](references/multi-machine-deployment.md) — 多机部署复测协议：拓扑探测、统一绑核、代码与数据分发（rsync 排除规则与已知坑）、远程构建验证、结果回收与横向对比。
