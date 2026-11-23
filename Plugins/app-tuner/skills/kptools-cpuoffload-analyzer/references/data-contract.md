# 数据契约 — kptools-cpuoffload-collector 产物与分析器 schema

## collector 产物（bundle 目录）

```
<bundle>/
├── probe.<tag>.json    # L0 外部快照（每 tag 一份，追加式）
├── trace.log           # 人类可读采集日志（追加式，分析器不消费）
├── alloc.<pid>.jsonl   # L1 埋点（可选）——kind/size/va/tid/caller/t_ns
├── marks.<pid>.jsonl   # L1 标记（可选）——opt_step_begin/end, affinity
└── stats.<pid>.json    # L2 CpuMemoryPool.stats()（可选）
```

### probe.<tag>.json

```json
{
  "skill": "kptools-cpuoffload-collector", "version": "1.0.0",
  "tag": "after_init", "ts": 1795405522.0,
  "pfn_readable": true, "sample_ratio": 64,
  "pattern": "/dev/devmm_svm",
  "ranks": [
    {
      "pid": 1000, "rank": 0, "local_rank": 0, "world_size": 2,
      "total_va_kb": 4194304,       // VA 预留（不等同驻留！）
      "total_present_kb": 2097152,  // 真实物理页驻留量
      "node_kb": {"0": 1677722, "1": 419430},
      "vmas": [
        {"start": 4194304, "end": 12582912, "perms": "rw-p",
         "pathname": "/dev/devmm_svm", "size_kb": 6291456,
         "present_kb": 2097152, "node_kb": {"0": 1677722, "1": 419430}}
      ]
    }
  ]
}
```

**关键口径**：
- `VA 预留（size_kb）≠ 驻留（present_kb）`——devmm 是 VA 预留
- `pfn_readable: false` 时 node_kb 的 key 可能全为 `"unknown"`
- 三波 pin 的时间轴语义：`after_init` 时 nova 为 0 是预期

### alloc.<pid>.jsonl（L1，可选）

每条一行 JSON：
```json
{"kind": "alloc", "source": "PinnedFlatPool", "method": "_grow",
 "size": 1073741824, "va": "0x7f...", "tid": 12345,
 "caller": "cpu_offload/pinned_pool.py:103", "t_ns": 1795400000123456789,
 "pid": 1000}
```

### stats.<pid>.json（L2，可选）

`CpuMemoryPool.stats()` 的 dict 快照——new_slabs / evictions /
reuse_hit_rate / active_bytes_peak / fragmentation 等 15 个字段。

## analyzer 输出

| 文件 | 内容 |
|------|------|
| `summary.json` | 结构化结论：timeline[] / findings[] / self_checks{} |
| stdout（markdown） | 三自检结果 + 总量时间演化 + 每 rank node 分布 + L2 stats + 口径声明 |
