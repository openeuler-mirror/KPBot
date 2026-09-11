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

## oneDNN 优化后超过 KDNN BRGEMM 的原因分析

若同一硬件、固定核心、相同线程数和相同 MatMul shape 的 A/B 数据显示优化后的 oneDNN 快于 KDNN BRGEMM，应把它解释为**完整执行路径的综合优势**，而不是直接断言 oneDNN 的单条 BRGEMM microkernel 更快。总耗时至少包括 kernel 计算、PackA/PackB、scratch、线程调度与同步、reduction/epilogue、JIT/cache 和框架 dispatch。

| 对比维度 | 优化后 oneDNN 的优势来源 | KDNN BRGEMM 的已观察限制 | 为什么影响最终性能 | 证据/验证方式 |
|---|---|---|---|---|
| Shape-aware blocking | 按小 M 大 K 等 family 保留高效 M32/N32 tile，并针对 N256/N512 选择 K256/K512 | 通用评分偏重线程填充率，可能把 M32 过切成 M4/M8，或采用过小 kBlock | 更大的有效 tile 提高 kernel 利用率，并减少 K 循环与 tail 开销 | E1、E2；对比 verbose 的 `m_blk/n_blk/k_blk/k_chunks` |
| PackB 摊销与复用 | 通过合适 kBlock 和 `K→N→M` 循环让同一个 B tile 服务多个 M block | K128 曾令 32×512×7744 产生 61 个 K chunk，每个 chunk 重复 PackB | 小 M 大 K 场景中 packing 可能占据显著比例，减少 copy 比微调 copy 指令更有效 | E1、E3/E4；检查 `copies/reuses` 和 PackB 字节数 |
| Parallel-K | 空间 work 不足时沿 K 维并行，同时保留高效 M/N tile | 为填满线程切小 M 会牺牲 microkernel 效率；缺少或限制 parallel-K 时部分核心空闲 | 在不破坏高效 tile 的前提下提高核心利用率 | E2；检查 `nthr_used/nthr_k/work` |
| Storage/compute 解耦 | N64 物理布局可使用 N32 compute tile，LDB 仍保持物理跨度 | storage N64 与 compute N64 耦合时，小 M 被迫使用不合适的 N tile | 避免物理权重布局决定低效计算形状 | E2；验证 `storage_n_blk=64,compute_n_blk=32` |
| Scratch 与工作集 | scratch pool、PackB 单槽和覆盖写证明降低分配、清零与临时工作集 | 每次 Run 分配/清零大 buffer，或每 worker 保留多个很快被覆盖的 B slot | 减少 first-touch、缓存/TLB 压力和内存带宽消耗 | E11；报告 scratch 峰值及 `reused/slots/worker_bytes`，独立 wall-time 结论仍标为 Conditional |
| PackA 时序与缓存局部性 | PackA 后立即计算，后续 N block 复用同一 packed-A | 先打包整个 M chunk 会让较早的 A block 在首次计算前被后续写入冲刷 | 缩短生产到消费的距离，提高 packed 数据命中率 | E11；确认 pack 次数不变并补交错 A/B |
| 线程 team 与同步 | compute、barrier、reduce 合并在一个 OpenMP region；单 K 线程走无 barrier 路径 | 多 region 或无条件 barrier 对亚毫秒 MatMul 的固定成本过高 | 调度和同步开销在小矩阵/短 kernel 中占比很大 | E2；修正前单 K 线程 prepack 曾回退约 26% |
| JIT、cache 与 direct output | kernel 变体复用、descriptor 语义缓存，满足条件时直接写 dst | cache key 不完整、descriptor `memcmp`、重复 JIT 或无谓 product buffer 往返都会增加固定成本 | 稳态推理中外围固定成本可能决定端到端胜负 | 阶段 3/5；分别报告首次运行和稳态执行时间 |
| PrePack 生命周期与锁 | 常量 B 使用可复用 blocked 权重，普通 Run 不持全程锁 | 外部 packed-buffer 路线若在整个 Run 持锁，会串行化并发 Compute | 减少重复 reorder，同时保留调用间并发 | E7、E8；无锁路线 +0–4.5%，全 Run 锁路线回退 3–22% |
| 路由与 fallback | 只把实测获益的 dtype/shape/layout/thread 配置交给 BRGEMM | 无条件替换全部 FP32 MatMul 会包含微小矩阵或不利 prepack shape | 选择性路由保留优势区间，避免少数慢 shape 拉低整体模型性能 | E7、E12、E14；同时比较原后端、plain 和 prepack |

报告结论应写为：“在本次测试的目标 shape、线程配置和 blocking 方案下，优化后的 oneDNN 依靠 packing、调度、缓存和框架路径的综合改进超过 KDNN BRGEMM。”除非另有同输入、同 descriptor 的 kernel-only 微基准，不得写成“oneDNN microkernel 普遍优于 KDNN”。prepack 曾出现最高约 63.8% 收益，也出现约 26.7% 回退，因此该结论不能外推到未测试 shape。

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
