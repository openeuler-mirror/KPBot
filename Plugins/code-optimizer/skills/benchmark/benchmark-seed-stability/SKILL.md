---
name: benchmark-seed-stability
description: >
  mem0/milvus LOCOMO benchmark harness 的 seed 阶段稳定性工程：关闭 Milvus auto compaction 压住 seed 期写放大、
  seed 资源 boost 在索引构建期临时提资源、放宽 harness 的 index-ready 超时而不动被测旋钮、
  clone 复用轮用 search-recovery 等待规避 Milvus 索引就绪误判。
  适用：mem0/milvus 基准 seed/索引构建期耗时长且非确定性（紧贴超时线）、clone 复用轮索引判据在固化快照上
  永不满足、或 seed 期后台 compaction 干扰构建导致抖动。
  不适用反例：非 mem0/milvus 负载（索引就绪判据与卷语义需重定）；seed 稳定且快；或无 Milvus 索引就绪阶段。
---

# mem0/milvus 基准 seed 稳定性（标定案例）

## 何时使用

- seed 期 HNSW 索引构建是**逐 segment 串行**（indexnode slot=1），后序大 segment 每 10% 需 30–60s，
  index-ready 时间非确定，紧贴 1800s 超时线。
- clone 复用轮里，索引判据在**固化快照上永不满足**（快照已有索引状态），需要 search-recovery 等待替代。
- seed 期后台 compaction 干扰构建，导致轮间抖动。

## 核心原则

1. **把 harness 超时和被测旋钮分开**：放宽 index-ready 超时只影响 harness 等待，不动被测组件配置、不影响
   ingestion/qa 指标。
2. **seed 期压住写放大**：关 auto compaction 消除构建期的后台 compaction 抖动。
3. **临时资源 boost 只在 seed 期**：索引就绪后恢复，不污染被测相位。
4. **clone 卷的索引判据要换**：固化快照上"索引就绪"的常规判据永不满足，改用 search 就绪等待。

## 步骤速览

开关、状态机时序与归因纪律**只在 `references/seed-stability.md` 一处维护**，本节给步骤名与去向：

1. **关闭 auto compaction**（`MILVUS_SEED_DISABLE_AUTO_COMPACTION=true`；注意计时前恢复/核实原配置的对比公平性）→ references §2。
2. **seed 资源 boost**（`MILVUS_SEED_BOOST_CPUS/MEMORY_GB`；变更前先持久化容器身份 + 原限额）→ references §3。
3. **放宽 index-ready 超时**（`MILVUS_INDEX_READY_TIMEOUT→3600s`；首轮超时归因纪律 N≥3）→ references §4。
4. **clone 复用轮 search-recovery 等待**（替代索引判据，规避 `MilvusException code=106`）→ references §5。

## 门禁

- [ ] auto compaction 关闭可开关、默认零行为变更；计时前恢复/核实原配置（或两侧全程同一配置）
- [ ] seed boost 变更前先持久化容器身份 + 原限额；恢复后回读核实再清除待恢复标记
- [ ] index-ready 超时放宽不动被测旋钮
- [ ] clone 复用轮 search-recovery 等待替代索引判据
- [ ] seed 期抖动被压住（多轮 index-ready 时间稳定，N≥3 且记录分布）
- [ ] 首轮超时记录保留（segment 进度 + 资源），相同配置重复测量后才归因

## 结果（标定案例形态）

关闭 auto compaction 后 seed 期不再有后台 compaction 干扰；index-ready 放宽后实测 1458.6s，回到基线区间
（1428–1621s），首轮超时原因仍待定；clone 复用轮不再被 code=106 打断。细节见 `references/seed-stability.md` §4/§5。

## 参考

- `references/seed-stability.md`：auto compaction 关闭细节、seed boost 状态机、index-ready 超时实证、
  search-recovery 与黄金卷复用轮的衔接。