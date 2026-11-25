# 数据契约 — kptools-cpuoffload-collector 产物与 analyzer 输入/输出 schema

对应 collector 版本 **1.1.0**（MR !312 起）：node_kb 全层级**KB 口径**
（不再有 `*_pct` 百分比字典——旧版采样模式产物已废弃）。

## collector 产物（bundle 目录）

```
<bundle>/
├── probe.<tag>.json    # L0 外部快照（每 tag 一份；同 tag 重跑需 --force）
├── trace.log           # 人类可读采集日志（追加式，分析器不消费）
├── run_meta.json       # world_size/device_node_map（首个快照写一次）
├── alloc.<pid>.jsonl   # L1 埋点（可选）——kind/size/ptrs/tid/caller/t_ns
├── marks.<pid>.jsonl   # L1 标记（可选）——opt_step_begin/end 等
└── stats.<pid>.json    # L2 CpuMemoryPool.stats()（可选，手动落盘，见下）
```

⚠ `stats.<pid>.json`：collector 与 L1 hook **均不自动生产**。需要 L2
数据时，在训练侧自行调用 `CpuMemoryPool.stats()` 并把 dict 写成该
文件名放入 bundle（字段随上游 nova 版本，常见键：new_slabs/
evictions/reuse_hit_rate/active_bytes_peak/fragmentation）。

### probe.<tag>.json

```json
{
  "skill": "kptools-cpuoffload-collector", "version": "1.1.0",
  "ts": 1795405522.0,
  "ts_min": 1795405522.1, "ts_max": 1795405522.4, "duration_s": 0.312,
  "tag": "after_init", "pfn_readable": true, "sample_ratio": 64,
  "pattern": "/dev/devmm_svm",
  "expected_ranks": 2, "partial": false, "missing": [],
  "ranks": [
    {
      "pid": 1000, "rank": 0, "local_rank": 0, "world_size": 2,
      "ts": 1795405522.1, "mode": "sampled_1_64",
      "total_va_kb": 4194304,          // VA 预留（不等同驻留！）
      "total_present_kb": 2097152,     // 真实物理页驻留量（KB）
      "node_kb": {"0": 1677722, "1": 419430, "unknown": 0},
      "vmas": [
        {"start": 4194304, "end": 12582912, "perms": "rw-p",
         "pathname": "/dev/devmm_svm", "size_kb": 6291456,
         "present_kb": 2097152,
         "node_kb": {"0": 1677722, "1": 419430},
         "sampled": {"samples": 32, "step": 64}}   // 采样证据（--full 无此键）
      ]
    }
  ]
}
```

**关键口径**：
- `node_kb` 恒 **KB**：full 精确值；`sampled_1_N` 为外推值（每 VMA 带
  `sampled` 证据）
- present 但 PFN 被屏蔽（无 CAP_SYS_ADMIN）/zone 外的页归入
  `"unknown"` 桶——`pfn_readable: false` 时非零 present 的 rank 会看到
  主桶 unknown；node_kb 为空 dict 且 present > 0 → 采集异常
- `pagemap_unreadable: true`（rank 级）：该 rank 只有 VA 大小，present
  为 null——analyzer 自检对其跳过
- `partial: true` + `missing[]`：部分 rank 探测失败（process_gone/
  permission_denied），已采集部分仍可分析
- 三波 pin 的时间轴语义：`after_init` 时 nova 为 0 是预期
- 快照**排序按 `ts`**（analyzer 侧），tag 命名不承担排序语义

### run_meta.json

```json
{"skill": "...", "version": "1.1.0", "ts": 1795405522.0,
 "world_size": 2, "sys_root": "/sys",
 "device_node_map": {"0000:8f:00.0": {"node": 1}}}
```

NPU 识别为 PCI **vendor 0x19e5 + class 0x120000 双校验**（排除同
vendor 的 Huawei 网卡等非加速设备）。

### alloc.<pid>.jsonl（L1，可选）

每条一行 JSON（collector `scripts/sitecustomize.py` 埋点，**文件名
必须保持 sitecustomize.py**）：

```json
{"kind": "alloc", "source": "PinnedFlatPool", "method": "_grow",
 "size": 1073741824, "result_ptr": "0x7f...", "arg_ptr": null,
 "tid": 12345, "caller": "cpu_offload/pinned_pool.py:103",
 "dur_us": 173, "t_ns": 1795400000123456789, "pid": 1000}
```

- `kind`：`alloc` | `copy`（pin_copy）| `free`
- `result_ptr`（alloc/copy 的返回 tensor）/ `arg_ptr`（free 的入参
  tensor）——**配对标识**：analyzer 自检 2 用其还原"快照时刻仍存活
  的分配量"（alloc↔free 配对），而非拿累计值硬比
- 无 ptr 的事件（如 `_grow` 返回 None、旧版 hook）：计入
  "无法配对"，存活量为下界并降级披露

### marks.<pid>.jsonl（L1，可选）

```json
{"kind": "opt_step_begin", "step": 1, "t_ns": ..., "pid": 1000}
```

`kind`：`hook_installed` / `pinned_pool_patched` / `mempool_patched` /
`optimizer_step_patched` / `opt_step_begin` / `opt_step_end` /
`patch_error`。analyzer 消费 `opt_step_begin` 计训练步数（步边界锚点）。

## analyzer 输出

默认写到 **bundle 同级 `<bundle名>_report/**`（不写 bundle——bundle
只读）**；`--out` 可显式指定。写入均为临时文件 + 原子替换。

| 文件 | 内容 |
|------|------|
| `summary.json` | 结构化结论：probes[]（按 ts）/ timeline[] / findings[] / self_checks{passed,issues,skipped} / l1_steps{} / l1_pools{} / parse_warnings[] |
| stdout（markdown） | 三自检（含跳过项与数据缺口）+ 总量时间演化 + 每 rank node 分布（GiB 展示、快照列）+ L1 步数/池拆分 + L2 stats + 口径声明 |

退出码：0 成功；1 参数错误（含 argparse 用法错误）；2 bundle 为空
（大概率训练未启用 cpu offload）；3 三自检失败（报告仍输出，结论不作数）。
