---
name: kptools-ascend-profiling-compare-h2d-d2h
description: |
  专项对比：两份 Ascend/torch_npu profiling 的主机-设备数据搬运（H2D/D2H/D2D）——
  拷贝次数、字节数、带宽、方向分布、与计算的重叠率、同步（阻塞型）拷贝占比。
  当用户明确要看“拷贝变多/变少了吗”“H2D 带宽/重叠率怎么样”“同步拷贝占比”时使用。
  通用“优化前后差多少 / 瓶颈变了吗”走 kptools-ascend-profiling-compare；
  设备气泡走 kptools-ascend-profiling-compare-bubble；算子维度走 -compare-opstats；
  单份 trace 分析走 kptools-ascend-profiling-analysis(-nostack)。
---

# H2D/D2H 拷贝对比

## 适用范围

- 同一负载两份采集的**数据搬运维度**：次数、字节、带宽、方向、重叠率、同步占比。
- 不解释“为什么空闲”（bubble 的职责），不做算子统计（opstats）。

## 输入

每个采集支持目录（`*_ascend_pt` / `ASCEND_PROFILER_OUTPUT`）或裸 `trace_view.json`。

事件识别与层级选择（**只取一层，避免同一笔拷贝跨层重复计数**）：

1. **设备层（主口径）**：带任务类型且名称命中 `Memcpy`/`Memset`/`TransData`/`h2d/d2h/d2d`/`copy_` 的设备事件，或 `task type=Memcpy`。`SDMA_SQE`/`PCIE_DMA_SQE` 描述符事件不计（它们是任务描述符，不是拷贝本体）。
2. **host 层（仅当设备层为空时兜底）**：优先 `acl_memcpy_*`/`acl_memset_*`（cpu_op），其次 `AscendCL@aclrtMemcpy*`，最后 `python_function`；不会与设备层混用。
3. **CSV 兜底**：`kernel_details.csv` 名称匹配上述关键字的行（仅次数）。
4. `api_statistic.csv` / `api_detail.csv` 的 `aclrtMemcpy*` 计数仅用于交叉核对，写入 `notes`，不参与指标。
5. `python_function`/`enqueue`/`dequeue` 等 host 层事件不会在设备层存在时计入；host 侧方向计数只作为 `notes` 参考。

注：compare / compare-bubble / compare-opstats / compare-fusion / stream 与本 skill 为同批 PR 交付，合入前暂用 kptools-ascend-profiling-analysis / -analysis-nostack 承接。

## 工作流

```bash
python3 scripts/memcpy_diff.py <base> <cand> --out-html report.html --json result.json \
  [--min-delta-count 0] [--min-delta-bytes 0] [--min-delta-pct 1.0]
```

## 指标与口径

| 指标 | 单位 | 含义 | 判定方向 |
|---|---|---|---|
| `memcpy_count` | count | 拷贝/转换事件数 | 越少越好 |
| `memcpy_bytes` | bytes | 已知大小的拷贝总字节（支持 `size(Byte)` 等带单位键） | 越少越好 |
| `memcpy_bandwidth_gbps` | Gbps | `字节×8 / 已知大小拷贝忙碌区间并集` | `info`（描述性） |
| `memcpy_overlap_pct` | pct | 拷贝忙碌区间 ∩ kernel 忙碌区间 ÷ 拷贝忙碌 | **越高越好**（判定已取反） |
| `memcpy_sync_pct` | pct | 拷贝窗口内 host 阻塞于 synchronize 的时长占比 | 越低越好 |
| `memcpy_h2d_count` / `memcpy_d2h_count` / `memcpy_d2d_count` | count | 方向分布 | `info` |
| `memcpy_size_unknown_count` | count | 大小未知的拷贝事件数（不计入字节/带宽） | `info` |
| `memcpy_unknown_direction_count` | count | 方向识别不出的事件数 | `info` |

方向判定优先级（**数字编码只用于 `args` 值，不参与名称匹配**）：`args` 的 `kind/direction` → 事件名关键字 → 设备任务的 `connection_id` 对应 `AscendCL@` 事件名 → `link type` 含 `ON_CHIP` 或 `transport type=SDMA` 归 `d2d`（启发式，`notes` 说明）→ `Memset` 归 `h2d`（启发式，`notes` 说明）→ `unknown`。数值按 CANN `aclrtMemcpyKind` 顺序枚举：**1=H2D、2=D2H、3=D2D**，0=H2H 计 `unknown`。通用 `aclrtMemcpyAsync`/PCIe DMA 若无方向信息则计 unknown。

`Δ = base − cand`；`|Δ| ≤ noise` 判 `noise`。全量 count/bytes 只作描述，方向判定使用**共同 step 的逐对差值中位数**；噪声带为
`max(--min-delta-count/--min-delta-bytes, MAD(逐对差值))`；bytes 的已知大小事件数两侧不一致时（覆盖范围不可比）判 `info`；`overlap_pct`/`sync_pct` 为固定
`--min-delta-pct`（不做 MAD 叠加），与实现一致。

`confidence`：两侧 trace 都正常 = `high`；csv 兜底、任一侧无拷贝事件、size 全部未知或任一降级 = `medium`；无 step 信息 = `low`。

## 输出

- `per_step` 保留真实 `ProfilerStep#N` 编号，按共同 id 对齐；缺对应样本的步骤保留并给 `null`，不重编号错配。
- 终端摘要、A.1 九字段 JSON、HTML（指标表、方向分布、数据完整性提示、降级与口径）。

## 降级路径（始终产出报告）

1. 无任何拷贝事件（甚至无设备 kernel）→ 指标全 0，`summary` 写明「未发现拷贝事件」，仍出报告（不返回错误页）。
2. 无 `size` 字段 → 只出次数/方向，字节与带宽为 0，计入 `memcpy_size_unknown_count`。
3. 方向识别不出 → 归 `unknown`，计数可见，避免把 H2D 变化误读成 D2H。
4. 无 kernel 窗口 → 重叠率 0 且写入 `degraded`。
5. 只有 `kernel_details.csv` → 仅计次数，标注降级。
6. 输入不可读 → HTML 错误页 + 退出码 3。

## 真实数据口径

- 设备任务用 `Task Type` 或 `task type`（`AI_*`、`SDMA_SQE`/`PCIE_DMA_SQE`、`Memcpy`），`cat` 多为 None。
- 方向：事件名里的 `host_to_device` / `device_to_host`（下划线归一化为空格后可识别）；`aclrtMemcpyAsync` 这类通用接口无法判方向 → 计入 `memcpy_unknown_direction_count`。
- 真实 trace 的设备 `Memcpy` 事件 size 键为 `size(Byte)`（如 `{"size(Byte)": 16384}`），本 skill 已支持；缺 size 的事件计入 `memcpy_size_unknown_count`。
- 设备 `Memcpy` 的 `link type=ON_CHIP`/`transport type=SDMA` 归 `d2d`；通用 PCIe DMA 无法判方向时计 unknown，host 侧方向计数写在 `notes` 供参考。
- 时间单位 µs；`ProfilerStep#N` 只按名字识别。

## 限制

- count/bytes/带宽/重叠/同步只统计**设备拷贝任务层**；host/Python/队列层不参与，避免同一拷贝重复计数。
- `aclrtMemcpy`/`aclrtMemcpyAsync` 等 API 调用计数与设备 memcpy 事件并非一一对应（异步提交、同笔搬运多 API 参与等），仅作交叉参考，不做次数一致性断言。
- 大小未知的拷贝无法估算带宽；报告显式给出未知事件数。
- 不支持跨采集时间戳对齐，重叠率只在各自采集内计算。

## 开发校验

```bash
python3 -m unittest tests.test_profiling_h2d_d2h -v
```
