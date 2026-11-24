# 采集参数与口径笔记

## 1. 构建

```bash
# 性能基线（无调试信息，-O3 -DNDEBUG）
scripts/build.sh -t Release

# perf 符号化 / 反汇编对照
scripts/build.sh -t RelWithDebInfo
```

约束：
- 必须 `-D SIMDJSON_DEVELOPER_MODE=ON` 才有 `benchmark/dom/parse` 与完整测试目标。
- 不要用 `-march=native`/`-mcpu=native` 等强制私有 ISA 的选项，会破坏 simdjson 运行时分派。
- 修改 `include/`/`src/` 后不要手改 `singleheader/`；需要时 `cmake --build . --target amalgamate`。

## 2. parse 基准输出

`-t`（tabbed）每行 7 列，第一列是语料名（带引号）：

```
"twitter"  <alloc c/b> <stage1 c/b> <stage2 c/b> <all c/b> <GB/s all> <GB/s stage1> <GB/s stage2>
```

- `-n N` 控制迭代；`-a arm64` 固定实现；`-s stage1` 可单独测阶段。
- 核心指标：`stage1 c/b`（机制）与 `stage1 GB/s`（阶段吞吐）；端到端看 `all GB/s`。
- 运行前固定绑核/NUMA：`numactl -N <node> -m <node>`。

## 3. perf 采集

```bash
perf record -e cycles:u -g -o perf.data -- <parse_cmd>
perf report --stdio -i perf.data | head -40
perf annotate -i perf.data --stdio -s <stage1_symbol> | head -80
```

- 采样率用默认即可；栈深度不需要 DWARF（解析热点是叶子函数）。
- **非精确采样滑移**：ARM64 上样本会落在真停顿之后 2~5 条指令。判据：
  - 热点指令是分支目标后的第一条；
  - 其操作数在上一轮已就绪（依赖不在临界路径）；
  - 往上游能找到长延迟链（水平归约、跨域传输、栈加载）。
- 不要把"最高采样指令"直接当作瓶颈写进结论。

## 4. Top-down / PMU

`scripts/topdown.sh` 采用分组 `perf stat` 避免事件 multiplexing。事件一律按名称引用，不内置平台原始事件编码。关键事件：

| 事件 | 用途 |
|---|---|
| cycles / instructions | 分母 / IPC |
| LD_CANCEL_MISALIGN | 未对齐 load |
| LD_CANCEL_FWD_HZD | store→load hazard |
| LD_CANCEL_LQ_FULL | load queue 压力 |
| memstall_anyload / memstall_store / memstall_L1miss | load/store/L1 miss stall |
| ST_SCA_FULL / ST_ORDER_FAIL | store 压力/顺序失败 |
| vfp_ptag_stall / vpd_ptag_stall | 向量寄存器/端口 stall |

- 事件组在当前平台不可用时自动跳过；全部不可用时降级为通用事件组（cycles/instructions/L1 miss）。
- 若平台内核未导出上述事件名，可在脚本同目录放置 `topdown-events.local`（每行 `事件名=原始事件编码`，编码以鲲鹏 PMU 事件文档为准，该文件不入库，模板见 `scripts/topdown-events.local.example`），脚本会把名称映射为原始事件后采集。
- 非 Kunpeng 平台没有这些专用 stall 事件，脚本会降级并在输出中标注；改用通用事件对照。

## 5. SPE（LD/ST 延迟）

```bash
perf record -e arm_spe/branch_filter=0,load_filter=1,store_filter=1/ -o spe.data -- <parse_cmd>
perf script -F cpu,op,pc,event,issue_lat,total_lat,vaddr,paddr,data_source -i spe.data > spe-ldst.txt
scripts/spe_ldst.py spe-ldst.txt --bin build-Release/benchmark/dom/parse --op LD --top 20
```

- `perf script -F` 输出是**空格分隔文本**，字段顺序即 `-F` 顺序：`cpu op pc event issue_lat total_lat vaddr paddr data_source`；`spe_ldst.py` 按此 9 列契约回退映射。
- 也接受带 `op`/`pc` 表头的逗号 CSV（如 spe-parser 产物，列顺序可任意）；`#` 横幅/注释行自动跳过。
- 无表头且列顺序非文档 9 列时，用 `--columns cpu,op,pc,...` 显式指定，否则脚本会明确报错而不是静默产出空结果。
- 关注 `total_lat` 的**分布尾部**（p99/max），不是均值；单条高延迟 LD 在均值里会被稀释。
- 对高延迟 PC 做 `objdump -d --start-address/--stop-address` 定向反汇编，检查是否有跨宽度的 NEON store→load。
- SPE 需要硬件支持 + 内核配置 + perf 版本；不可用时降级为 Top-down 的 hazard 事件 + 静态反汇编。

## 6. 口径陷阱

| 陷阱 | 正确做法 |
|---|---|
| 先跑完 A 再跑 B | AB/BA 交错 5 轮取中位 |
| 用 perf 采集跑次测性能 | 采集与测速分离 |
| 把 stage1 收益说成端到端收益 | 用 stage1 占 all 比例换算 |
| 只看 c/b 不看 GB/s | 两者同时给（频率变化会改变 c/b 与 GB/s 的关系） |
| 换语料不重测 | 非 ASCII 占比与结构密度决定收益结构 |
| 认为"编译器会复用寄存器" | 用 `objdump` 统计 `ldr/str q,[sp]` 与栈帧 |
| 把 B 层设计方向写成已实现收益 | 标注证据层级 |

## 7. 寄存器/栈帧复核命令

```bash
# NEON reload / spill 计数（stage1 附近）
objdump -d build-Release/benchmark/dom/parse | grep -c 'ldr[[:space:]]*q.*\[sp'
objdump -d build-Release/benchmark/dom/parse | grep -c 'str[[:space:]]*q.*\[sp'

# 栈帧大小（函数序言的 stp x29,x30,[sp,#-N]!）
objdump -d build-Release/benchmark/dom/parse | grep -A1 '<json_structural_indexer5index' | head
```

计数趋势比绝对值更重要：加宽 step 后 spill/reload 应下降或持平，若显著上升说明瓶颈被搬到栈加载。

## 8. 功能验证矩阵

| 类别 | 命令/输入 |
|---|---|
| DOM 回归 | `ctest -L dom` |
| On Demand 回归 | `ctest -L ondemand` |
| 按实现专项 | `SIMDJSON_FORCE_IMPLEMENTATION=arm64 ctest -L per_implementation` |
| 边界 | 空串、1B、无结构长串、跨 64B/256B、非法 UTF-8、多字节被块边界截断 |
| 对齐 | 非对齐起点、`padded_string` 移动/析构、`parser.load/load_many` 复用 |
