# 拷贝事件识别与口径

## 1. 事件识别

关键字：`memcpy`、`memset`、`transdata`、`h2d`、`d2h`、`d2d`、`copy_`（大小写不敏感）。
来源与层级（**只取一层**）：

| 优先级 | 来源 | 提供 | 局限 |
|---|---|---|---|
| 1 | `trace_view.json` **设备层**拷贝任务 | 名称/时间/args（size、kind、connection_id、link/transport） | 通用 PCIe DMA 无方向 |
| 2 | `trace_view.json` **host 层**（仅设备层为空） | `acl_memcpy_*` > `AscendCL@aclrtMemcpy*` > `python_function` | 方向仅从名称可得 |
| 3 | `kernel_details.csv` | 名称/耗时/起点 | 无 size；仅计次数 |
| 4 | `api_statistic.csv` | API 调用次数 | aclrtMemcpy/aclrtMemcpyAsync 等调用计数与设备事件非一一对应（异步提交、同笔搬运多 API 参与），只做交叉参考 |

**避免重复计数**：
- 设备层存在时，`python_function` / `enqueue` / `dequeue` / host API 都不计入（只把 host 方向计数写入 `notes`）。
- `SDMA_SQE` / `PCIE_DMA_SQE` 描述符事件不是拷贝本体，不计入。
- host 层兜底时只选一层（cpu_op `acl_memcpy_*` → API → python_function），不混层。
- trace 有拷贝事件时不再合并 csv 行。

## 2. 方向判定

1. `args` 中键名归一化后命中 `kind/direction/dir/copytype/copy_type`：
   - 数值按 CANN `aclrtMemcpyKind` 顺序枚举：`1→h2d`、`2→d2h`、`3→d2d`，`0`（H2H）计 `unknown`；**数字键不参与名称匹配**。
   - 字符串 `h2d/d2h/d2d`、`host to device` 等 → 直接映射。
2. 事件名中的独立词 `h2d/d2h/d2d`（要求前后不是字母，避免 `h2dxxx` 误命中）。
3. 设备任务 `connection_id` 对应 `AscendCL@` 事件名中的方向（通用 `aclrtMemcpyAsync` 无名向）。
4. `link type` 含 `ON_CHIP` 或 `transport type=SDMA` → `d2d`（设备内搬运启发式，`notes` 明示）。
5. `Memset` → 归 `h2d`（零填充通常在 host 侧发起），报告 `notes` 明示这是假设。
6. 其余归 `unknown`，单独计数，不并入任何方向；host 侧方向计数写入 `notes` 参考。

D2D 误标常见于把 device 内拷贝写成 `Memcpy`：若 `args.kind=3`（D2D）优先于名称，可纠正。

## 3. 指标定义

- `memcpy_count`：拷贝事件条数。
- `memcpy_bytes`：仅累加 `size>0` 的事件；未知大小事件计入 `memcpy_size_unknown_count`。size 键支持带单位形式（`size(Byte)` 等）。
- `memcpy_bandwidth_gbps`：`Σbytes × 8 / (已知大小拷贝忙碌区间并集) / 1e9`；重叠拷贝不重复计时。
- `memcpy_overlap_pct`：拷贝忙碌区间（并集）∩ kernel 忙碌区间（并集）的时长 ÷ 拷贝忙碌时长。
- `memcpy_sync_pct`：拷贝忙碌区间 ∩ host synchronize 类事件区间的时长 ÷ 拷贝忙碌时长。
- 方向计数：按第 2 节结果分别统计。

时间统一按 µs 解释（torch_npu/CANN chrome trace 规范，绝对时间戳也是 µs），不做 ns 量级猜测/转换。

## 4. 判定方向

| 指标 | 语义 | verdict 规则 |
|---|---|---|
| count / bytes | 搬运越少越好 | Δ>noise → improved |
| overlap_pct | 重叠越高越好 | Δ<−noise → improved（取反） |
| sync_pct | 阻塞占比越低越好 | Δ>noise → improved |
| bandwidth / 方向计数 / 未知计数 | 描述性 | `info` |

计数/字节全量值仅作描述，方向判定用共同 step 的逐对差值中位数；噪声为 `MAD(逐对差值)`；已知大小事件数两侧不一致时 bytes 判 `info`（覆盖范围不可比）。百分比噪声用 `--min-delta-pct`（默认 1.0）。

## 5. 降级与置信度

| 情况 | 行为 | confidence |
|---|---|---|
| 两侧 trace 正常、有 kernel 与 size | 全指标 | high |
| csv 兜底 / 无 kernel / 无 size / 任一侧无拷贝 | 部分指标为 0 并写 degraded | medium |
| 无 ProfilerStep | 整窗结论 | low |
| 输入不可读 | HTML 错误页 + 退出码 3 | — |

## 6. 边界

- 设备气泡：`kptools-ascend-profiling-compare-bubble`。
- 算子统计：`kptools-ascend-profiling-compare-opstats`。
- 步耗时与四分类：`kptools-ascend-profiling-compare`。
