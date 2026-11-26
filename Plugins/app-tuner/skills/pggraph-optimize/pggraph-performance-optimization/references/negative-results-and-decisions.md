# 负结果、决策与回滚

## 1. 负结果清单（源资料实测）

| 方向 | 目标 | 实测 | 原因 |
|------|------|------|------|
| 密集位图替代 RoaringBitmap | BFS visited | TOTAL 1405→1803ms（慢 28%） | 小 BFS 集合 RoaringBitmap 更省；DenseVisited 分配/清零 + 转换成本高 |
| ARM `prfm` 预取 | BFS 邻居 is_active | TOTAL 1405→1659ms（慢 18%） | 图/CSR 在 L2/L3 驻留，预取无延迟可藏；额外指令/`collect Vec` 开销 |
| NEON 批量 is_active | BFS | 不适用 | aarch64 无 NEON gather；随机位图访问无法有效 SIMD |
| 手动 SVE gather `find` | UnionFind | 慢约 20% | 树浅（1-2 层）、gather 延迟 > L1/L2、谓词/同步开销、CPU OoO 已并行标量 |
| SVE gather `is_active` | BFS | 慢 9~39% | active bitmap 仅约 12.5KB，L1 命中；gather 延迟高；u32 展开污染 cache |
| `target-cpu=native` 的非 SVE 部分 | 全查询 | 0% | LSE/AES/CRC/FP16 在 pgGraph 中未被利用 |
| PG `jit=off`/`synchronous_commit=off` | 全查询 | 约 -1% | 图查询主要是 C/Rust 调用；收益小且需按版本复测 |

结论：

- 图算法热点是不规则随机访问/浅树/小位图，SIMD gather/预取往往不划算。
- 算法/数据结构优化（A+B+C+H，代号定义见 SKILL.md 速查表脚注）远大于 SIMD
  （约 -30.8% vs -2.5%——两者分属默认轮次与 40 次迭代两套测量系列，仅作各自系列内
  相对收益的对照，绝对值不可横比）。
- 负结果要保留补丁/数据，防止重复试错。

## 2. 收益/风险决策矩阵

| 优化 | 实测收益 | 风险 | 默认建议 |
|------|----------|------|----------|
| UnionFind 迭代 + halving | component -18.6% | 低（算法等价） | 采用（若 components 是目标） |
| node_deleted 快速路径 | component -19.6% | 低（只读路径） | 采用（需写路径标志正确） |
| component_sizes Vec | component -6.8% | 低（O(n) 内存） | 采用（可接受内存） |
| 数值排序 node_idx | 2-hop -20~29% | 中（排序等价性） | 采用（需正确性门禁） |
| SVE 自动向量化 | TOTAL -2.5% | 中（可移植性） | 可选；构建/运行 CPU 一致时 |
| 关闭 hydrate | 视查询，可能大幅 | 低（语义变化） | 按场景；计数/大遍历默认关 |
| 连接池/限并发 | 视环境，可能大幅 | 低（配置） | 采用；先容量扫描 |
| 反向 CSR 共享 | 预期大，未验证 | 高（工件格式/mmap） | 候选，先原型+验证 |
| 跳过 CRC | 预期中，未验证 | 高（完整性） | 仅可信/可重建场景，显式开关 |
| 密集位图/预取/SVE gather | 负 | — | 不采用 |

## 3. 回滚

### 3.1 参数/GUC

```sql
-- 修改前记录
SELECT name, setting, source FROM pg_settings WHERE name LIKE 'graph.%';

-- 回滚示例
ALTER SYSTEM RESET graph.memory_limit_mb;
-- 或 ALTER SYSTEM SET ... = <原值>;
SELECT pg_reload_conf();
```

需要重启的参数（如 shared_buffers）按 PG 规则重启；重启前确认连接/任务。

### 3.2 源码/二进制

```
① 保留旧 .so 及 SHA256
② 新 .so 失败 → 恢复旧 .so → 重启 PG
③ 工件格式不兼容 → 恢复旧工件或重新 build
④ 验证：扩展版本、1-hop traverse、SQL JOIN/checksum
```

### 3.3 数据/工件

- 源表：`pg_dump`/物理备份恢复。
- 图工件：格式/版本一致才可复用；否则 `graph.reset()` + 重新注册/build。
- 不要手工改 `.pggraph` header/CRC；重新 build 更安全。

## 4. 决策流程

```
是否有正确性证据？ ──否──▶ 回滚/修复，不看性能
        │是
        ▼
收益是否 >5% 且多轮稳定？ ──否──▶ 视为噪声，回滚或继续观察
        │是
        ▼
是否有场景回退/内存/启动代价？ ──是──▶ 权衡；默认不保留
        │否
        ▼
是否有回滚方式/记录？ ──否──▶ 补齐再上线
        │是
        ▼
保留
```

## 5. 负结果记录模板

```markdown
## 负结果：<方向>
- 日期/环境:
- 基线身份: pgGraph/.so/PG/Rust/编译参数
- 数据集/查询/口径:
- 变更: 文件/函数/diff 摘要
- 正确性: 通过/失败（证据）
- 性能: baseline vs candidate，每查询 median/TOTAL/QPS，轮次/离散度
- 结论: 负收益/无收益/不适用
- 原因分析: 机制（缓存/延迟/分配/指令）
- 保留物: patch/commit/report 路径
- 后续: 不建议重复 / 换方向
```

## 6. 不要做的事

- 不要用一次 hot run 的最好值宣布收益。
- 不要用 throughput 的 `ok=true` 代替正确性。
- 不要混合多个变量（参数+源码+编译）。
- 不要在没有工件/数据身份的情况下跨环境比数字。
- 不要把“预期收益”（反向 CSR 共享、CRC 跳过）写成已实现。
- 不要把负结果删掉；它会阻止下一次重复踩坑。
