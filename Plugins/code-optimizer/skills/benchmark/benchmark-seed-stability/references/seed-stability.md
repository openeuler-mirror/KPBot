# seed 稳定性全量（标定案例）

## 1. 问题形态

- seed 期 HNSW 索引构建逐 segment 串行（indexnode slot=1，`active=1 slot=0`），后序大 segment 每次 10%
  需 30–60s，index-ready 时间非确定。
- 关闭 auto compaction 前的 seed 期，后台 compaction 与构建争抢，放大多轮抖动。

## 2. auto compaction 关闭

- 开关：`MILVUS_SEED_DISABLE_AUTO_COMPACTION=true`。
- 生效：seed 前 `sed dataCoord.compaction.enableAutoCompaction:false` + restart，保持到 run 结束。
- 纪律：总开关与手动 `compact()` 不动；默认 false 零行为变更。
- **对比公平性**：关闭态若持续到 ingestion/qa，则两侧计时窗口内后台任务不同（基线默认值 compaction 仍在跑）。
  需在计时前**恢复并核实原配置**，或要求两侧全程用同一配置（都关/都开，作冻结条件）；并说明中断后恢复方式
  ——run 异常退出后 compaction 可能停在关闭态，下次启动前回读并恢复/核实，否则基线漂移。

## 3. seed 资源 boost 状态机

```
apply 前：若 state 文件存在**且带待恢复标记** → 先恢复残留（上次异常退出未恢复）
boost    ：docker update **之前**先写容器 id/name + 原限额 + 待恢复标记，再 docker update 提 CPU/内存
build 完 ：索引就绪 → 恢复原资源 → 回读核实生效值 → 清除待恢复标记
```

入口：`MILVUS_SEED_BOOST_CPUS` / `MILVUS_SEED_BOOST_MEMORY_GB`（空 = 关）。label 过滤读 `COMPOSE_PROJECT_NAME`
（不硬编码 compose project）。

**状态机必须"变更前持久化容器身份 + 原限额"**：boost 执行 `docker update` **之前**，先把容器
id/name 与**原 CPU/内存限额**写进 state 文件——否则首次 update 成功后进程中断，下次就找不到原限额。
恢复顺序：应用前回读原限额 → 恢复 → 回读 `docker inspect` 生效值（HostConfig.NanoCpus / HostConfig.Memory）
核实 → 再清除待恢复标记；核实不一致重试。

## 4. index-ready 超时实证

- 默认 `MILVUS_INDEX_READY_TIMEOUT=1800`，首次计时轮卡死在 HNSW 种子索引构建（`wait_milvus_index_ready.py`
  TimeoutError after 1801s），无 replay 产物 → 无效轮。
- 提 3600s 后 index-ready 实测 1458.6s，落在基线 1428–1621s 区间。
- **归因纪律**：1458.6s 本就低于原 1800s 门限，后一次成功不能解释首次超时、也不能证明耗时已稳定。
  必须保留首轮超时记录（卡在哪个 segment、`pending_index_rows` 进度、当时资源），核对首轮的 segment 进度
  与资源，再在相同配置下重复测量多轮（N≥3，记录分布）后才归因"harness 波动"；之前结论停留 inconclusive。
- 关键：放宽超时**只影响 harness 等待**，不动被测旋钮、不影响 ingest/qa 指标。

## 5. clone 复用轮 search-recovery

固化快照（黄金卷）上，索引已"就绪"过一次，常规索引判据（pending=0/indexed≥total）永不满足。
clone 复用轮改用 `wait_milvus_search_ready.py`：探测 search 可用即放行。即刻 replay 首请求会报
`MilvusException code=106`（= `ErrCollectionOnRecovering`，collection 恢复中、可重试）——search 探测正是
等这个恢复完成，而不是"索引判据永不满足"本身（两者是不同层面的等待）。

## 6. 与黄金卷 / 统计设计的衔接

- 黄金卷 build 轮需要 seed 稳定（关闭 compaction + boost），否则 seal 的是抖动产物。
- clone 复用轮需要 search-recovery 等待替代索引判据。
- 三者共同保证：多轮数据池一致 + seed 时间收敛 + 复用轮不被误判打断。

## 7. 坑点

- index 构建是 C++/knowhere + jemalloc（`LD_PRELOAD`），Go 的 GC 参数无法影响；别把 seed 抖动归因到 GC。
- seed boost 状态文件的**待恢复标记未清除**会导致"apply 前恢复"逻辑误恢复——正常收尾必须回读核实后
  **清除待恢复标记**，保证幂等（已恢复态 ≈ 无待恢复标记，apply 前不再重复恢复）。