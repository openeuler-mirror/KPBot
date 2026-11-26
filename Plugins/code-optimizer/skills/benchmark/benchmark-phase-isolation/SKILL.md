---
name: benchmark-phase-isolation
description: >
  mem0/milvus LOCOMO benchmark harness 的相位隔离：把"写与读混跑"的 trace-stress-replay 负载切成
  干净的 ingestion / Milvus 索引就绪屏障 / qa 窗口，用 barrier（全量 ingest → 索引就绪 → 全量 qa）、
  --phase 单相位重放与 AsyncJsonlWriter 异步事件落盘，并把精确窗口写进 phase_windows 供事后定界。
  适用：mem0/milvus 基准里写入/构建/查询相位墙钟重叠、要单独对比某相位的吞吐/时延；要复现
  phase_mode/phase_windows；或要把"恒定开销（seed/索引构建）"与"可变相位（ingest/qa）"切开只对比可变相位。
  不适用反例：非 mem0/milvus 负载（相位命名与索引就绪判据需重定）；负载本就串行无相位重叠；
  或基准本身没有 Milvus 索引就绪阶段（barrier 无从触发）。
---

# mem0/milvus 基准相位隔离（barrier / --phase / AsyncJsonlWriter）

## 何时使用

- 基准默认 mixed 模式：每个用户"先若干 ingest 再若干 QA"串行、用户间并发，ingestion 与 qa 墙钟重叠，
  无法干净回答"到底是写慢还是读慢"。
- 想把 seed + 索引构建这类**恒定开销**与 ingestion/qa 这些**可变相位**切开，对比只落在可变相位。
- 需要 `phase_windows` 给采集器/计时做精确边界，而不是假设相位边界是干净的。

## 核心原则

1. **相位是"有名字的可变开销窗口"**：先按"恒定 vs 可变"切，再用阶段标记或逐请求事件定界；对比只锁
   可变相位里的"吞吐 + 分位时延"组合。
2. **barrier 是解决写读重叠的正解**：全量 ingestion → 等待索引就绪 → 全量 qa，写与读被一个屏障切开。
3. **异步事件落盘**：逐请求事件改用后台线程批量写 JSONL；相位标记不打进 JSONL 流，按阶段标记契约
   打到 **stdout**（`kind`/`phase` + `flush=True`，契约见 `opt-measurement-infra`，独立 PR 待合入）。
4. **精确窗口以 phase_windows 为准**，定界取事件发生时间，不要假设"干净边界"、不要用 JSONL 落盘墙钟。

## 步骤速览

模式表、去中心化 barrier 三步顺序、AsyncJsonlWriter 替换细节与坑点**只在 `references/phase-isolation.md`
一处维护**，本节给步骤名与去向：

1. **切相位（三种模式）**：mixed（默认）/ barrier / 单相位（`--phase ingestion|qa`）；`phase_windows` 各模式
   都有，barrier 下另有 `phase_mode`/`index_settle`/分相位 requests → references §2。
2. **分片 barrier 去中心化语义**：先等全部分片 ingestion 完成 → 核对索引覆盖最终写入 → 放行 qa → references §3。
3. **AsyncJsonlWriter**：后台批量写逐请求事件、把高频小写移出关键路径 → references §4。
4. **用 phase_windows 定界**：`mem0-api-replay-summary.json` 里的 `phase_windows.{ingestion,qa}` 取事件发生时间定界 → references §4/§5。

## 门禁

- [ ] barrier / mixed / 单相位三种模式可切换，默认 mixed 零行为变更
- [ ] **按 phase_mode 列必需字段**：`barrier` 才有 `index_settle`；mixed 不生成该字段（只有 seed 后的
      `stable_polls`）。验收时按模式核对，不要对所有模式都要求 `index_settle`，否则正常 mixed 回放也会验收失败
- [ ] `phase_windows` 落进 summary（事件发生时间，非落盘墙钟）
- [ ] 单相位运行只检查所选相位的窗口（如 `--phase qa` 只验 qa 窗口，不要求 ingestion 窗口）
- [ ] 分片 barrier 先等所有分片 ingestion 完成 → 核对索引覆盖最终写入 → 再放行 qa；分片失败/超时中止本轮
- [ ] 事件落盘异步化把高频小写移出请求关键路径，对被测吞吐影响可忽略（必要时以无事件落盘轮对照验证）
- [ ] 对比只锁可变相位，排除 seed/索引恒定开销

## 结果（标定案例形态）

mixed 下 ingestion/qa 墙钟重叠；barrier 切开后 ingestion 与 qa 可分相位对比（本项目主口径为 qa retrieval
吞吐，基线见 `opt-onboarding-and-baseline`，独立 PR 待合入）。细节见 `references/phase-isolation.md` §5。

## 参考

- `references/phase-isolation.md`：三种模式完整语义、分片 barrier 论证、AsyncJsonlWriter 替换细节、
  与统计设计的衔接、坑点清单。