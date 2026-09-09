# 阶段 8：证据与交付

来源仓库为 `kdnn_brgemm_integrate` 的 `brgemm-ort-integration` 分支，配套数据来自 ORT `docs/kdnn_brgemm/`。交付时把路径替换为评审者可访问的仓库链接或制品位置。

## 证据索引 E1–E15

| ID | 提交/材料 | 可支持的结论 |
|---|---|---|
| E1 | `b8bd7df4`，`bench_kblock512_results.txt`，P3 §8.3 | kBlock 128 导致 61 chunks；扫描后 512 使目标 shape 从 1.4–2.8× 回退变为相对 MLAS 0.53–0.78× |
| E2 | `659f81f7`，FamilyBlocking 4 用例，5 轮交错 A/B | storage/compute 解耦与 SVE256 parallel-K；目标 -21%~-63%；kThreads=1 barrier 修正 |
| E3/E4 | `b7567098`，实现记录 §6 | PackB M-chunk 复用；16T N512 5.7×、N1024 6.8×；copies/reuses 单测 |
| E5 | `5c102277` | 大二次幂 packed LDA 512→528、1024→1040 与数值测试 |
| E6 | `659f81f7` | plain N256 family PackB；32×256×3400 plain 改善 43%/53% |
| E7 | P3 §7.2/§7.3 | prepack 高度 shape-dependent：最高 +63.8%，也出现 -26.7%，必须三路比较 |
| E8 | `a1e34047`/`983342b5`，P2 §4 | blocked 无锁路线 +0–4.5%；全 Run 锁的外部 buffer 路线回退 3–22% |
| E9 | `ab_interleaved_node2_5rounds.txt` | worktree/LD_PRELOAD/verbose 验证；曾发现 gtest 静态链接旧库 |
| E10 | P3 优化计划 §7 与固定核实验 | min 比 avg 偏 15%+；仅绑 node 时 sd 15–30μs，固定核后 1–3μs |
| E11 | `464e9c71/bca5dd92/664c6987/b7567098` | 预取、scratch pool、单槽、PackA 交错、batch 合并；部分仅结构/正确性证据 |
| E12 | P3 §6/§8.4 | 微小矩阵不凭算子级收益路由；kBlock 修复后需重审旧禁用边界 |
| E13 | P2 report | FusedTensordotMatMul 端到端 +11–23%，属于综合/经典路径，不单归因 BRGEMM |
| E14 | `4c5b4441/b7567098` | dtype、post-op、小矩阵与 fallback 路由基础设施；无独立性能结论 |
| E15 | 集成文档 §7 | SUM scale/post-op 索引适配器正确性修复；非性能优化 |

## 证据等级

- Confirmed：固定核、交错 A/B、正确性和全矩阵回退均完成，可写明确收益。
- Conditional：有结构、内存或局部数据，但缺独立 wall-time A/B；只能描述机制与验证范围。
- Infrastructure/Correctness：路由、adapter 或测试基础设施，不得包装成性能收益。
- Pending：未实现或未测量，只列后续工作。

当前 Pending 包括：新权重 K16×N32 物理布局、多实例/NUMA 下重搜 blocking、SVE256 microkernel 二阶段、M=1 GEMV 免 PackB、SVE512 低空间撤销 PackB、跨 session 的 prepack layout-key 验证。

## 交付清单

- commit、编译器/依赖/ISA、硬件/NUMA/固定核心、命令和环境变量。
- shape、plain/prepack、线程数、三轮平均时间中位数、标准差、ratio 和首次 reorder。
- CSV、benchmark 脚本、verbose、JIT dump、正确性与完整回归日志。
- 每条收益绑定证据 ID；旧结论被新 blocking 推翻时显式标记失效。
- 峰值 scratch/cache/prepack 内存、锁粒度、fallback、已知局限和 Pending。

没有原始数据或只有 Conditional 证据时，使用“预期/结构性改善/待 A/B 验证”，不得使用确定的加速百分比。
