---
name: compare-p-over-time
description: >
  离线对比旧 P 与新 P 的 vLLM-Ascend 容器快照，以旧 P 为基线定位环境漂移。
---

# P 环境变更前后对比

## 工作流程

1. 确认旧 P 和新 P 两份不同的快照。核对 `collection_meta.json` 的
   `targets`、`role`、节点和采集时间，以及目标 `inventory.json` 是否存在。
   比较器会展开包内全部容器，因此每份包只接受一个对象。角色缺失或标为 `PD`
   时，请用户确认 P 角色并注明来源。遇到角色或采集时间与指定的新旧顺序不符，
   先核对输入。目录和它的压缩副本若是同次采集（节点、对象、采集时间及清单
   相符），停止历史对比。
2. 在父 skill 目录运行现有比较器，先传旧 P，再传新 P；输出目录须为空或不存在。

```bash
python3 scripts/compare.py <旧P采集包> <新P采集包> --out <P前后对比目录>
```

3. 检查 `对比结果.json`：应有两个环境和一个组合，且组合中的 `left`、`right`
   分别指向旧 P、新 P。若 `comparisons[0].comparable` 为 `false`，将整组记为
   「无法比较」，引用失败侧 `environments[].error`；退出码 0 或零差异不代表相同。
4. 核对两侧 `inventory.json` 的 `collection_status.failed` 和 `never_started`。
   任一侧的 `cgroup` 探针失败或未启动时，对应的 `cgroup: …` 字段无法比较，
   从漂移清单和计数中排除。Docker inspect 配额是独立证据，照常核实。

## 输出

记录旧 P 和新 P 的节点、容器、角色来源、采集时间、采集包路径，以及对比状态
（完成 / 部分完成 / 无法比较）和 `对比结果.json` 路径。优先说明变更前后的
核心包、启动参数、CPU/NUMA 配额及其他性能相关差异。缺采按「无法比较」说明，
不算配置变化；配置差异也不等于性能回退。敏感字段只引用名称和受限证据位置。
