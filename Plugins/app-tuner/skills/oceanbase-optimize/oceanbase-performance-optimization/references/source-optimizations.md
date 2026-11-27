# 源码优化点

前置：先用 `oceanbase-hotspot-analysis` 证明热点存在。OB 业务热点多而分散（最大约 4%），
不要期待单点指令优化带来大幅提升。以下“实测”来自 OB 4.2.5.5 系列补丁/实验。

## 1. ApplySrv 自适应线程（insert 首选）

### 1.1 现象

- 950 上 `T<tenant>_ApplySrv` 线程接近打满单核（线程级 `top -H`/`pidstat -t -p <pid> 1` 持续 ≥90% 单核），AMD 不到该水平。
- worker 在等提交回调推进，TPS 上不去；加 worker 无效。
- 热点：`serialize` 相关（日志回写前遍历 id 数组、嵌套索引、随机访问）。

### 1.2 改动思路

- 位置：`src/logservice/applyservice/ob_log_apply_service.h`（apply 服务与回调队列）。
- 把 ApplySrv 线程数从固定/较少改为**按负载自适应**（增加工作线程/队列），消除单点。
- 需要保证：提交顺序/可见性语义不变；空闲时不浪费 CPU；有上限避免线程爆炸。

### 1.3 实测

| 场景 | 收益 |
| --- | --- |
| insert（950） | +12% |
| write | 约 -3%（可能为转储波动） |
| read/select/update | 无明显变化或略降 |
| 叠加多队列 | 无劣化 |

### 1.4 风险

- 线程数增加带来调度/内存开销；需要上限与自适应退避。
- 必须做全场景 A/B，不能只看 insert。
- 合入上游/客户版本需要评审与长期维护。

## 2. sql_nio–worker 多队列亲和（read/select/update）

### 2.1 现象

- `sql_nio` 与 worker 交互频繁；线程在多个 L3/cluster 间共享数据，L3 miss 高。
- 轻查询高 QPS 时，调度与队列开销占比上升。

### 2.2 改动思路

- 线程分组：奇数索引线程绑前半 CPU，偶数索引绑后半 CPU（示例）。
- 新增 2 个 net 队列：队列 1 服务偶数 id 线程，队列 2 服务奇数 id 线程。
- 任务类型枚举扩展（示例）：`OB_RPC, OB_MYSQL, OB_TASK, OB_TS_TASK, OB_SQL_TASK, OB_SQL_SOCK_TASK, OB_DAS_PARALLEL_TASK`。
- 位置：observer 网络/请求分发与租户队列（`src/observer/ob_srv_deliver.*`、
`src/observer/omt/` 附近；以补丁实际文件为准）。

### 2.3 实测

| 场景 | 收益 |
| --- | --- |
| read | +4.7% |
| select_random_points | +4.6% |
| update_non_index | +3.6%（proxy 不绑核时可达 +10%） |
| write | 无明显效果 |
| insert | 有一定劣化；需叠加 ApplySrv 优化 |

LLC-load-misses 下降（幅度按当次 perf 采样填写）。

### 2.4 风险

- 改动队列/线程绑定，影响调度与公平性；需验证高并发/混合负载。
- insert 场景必须叠加 ApplySrv，否则可能负收益。
- 与 cgroup/绑核策略耦合，换部署方式要重测。

## 3. gettimeofday 热点消除（update，低风险）

### 3.1 现象

- 950 update 场景 `gettimeofday` 热点占比需按当次火焰图实测（不沿用其它机型的值）。
- 调用点通常是 `common::ObTimeUtility::current_time()`（`deps/oblib/src/lib/time/`），
上层走 `clock_gettime/gettimeofday`；火焰图中可能显示为 libc 符号。

### 3.2 改动

- 用运行时 LD_PRELOAD 替换为更快的时钟实现（如 `myTimeAndGettimeofday.c` 一类）。
- 不改 OB 源码；需要保证时间语义（单调性/精度）满足事务/日志需求。

### 3.3 实测

- 热点占比及下降幅度需按当次火焰图实测；update 吞吐 +1.4%（绝对值按当次口径填写）。

### 3.4 风险

- 预加载库影响所有时间调用；需要兼容性测试与回滚（去掉 LD_PRELOAD）。
- 不同版本/工具链可能已优化，先测再上。

## 4. serialize 随机访问（实验，需谨慎）

- 现象：ApplySrv serialize 遍历 id 数组 → `sys_var_idx` → 再索引另一数组 → 调成员函数；
`sys_var_idx` 不可预测，预取效果差。
- 实验：
  - 手动把 `idx` 置 0：减少随机读取，insert +14%，但**牺牲数据准确性 → 拒绝**。
  - 预取（prfm）：无明显收益。
- 结论：正确性优先；除非能保持语义，否则不做。下一步是单独抠函数段对比 920B/950/AMD。

## 5. false sharing / cacheline 对齐

- 实测：920B、950 对齐优化前后性能均无明显变化。
- 原因：相关函数热点占比很小。
- 结论：不作为优先项；先用 perf 证明 false sharing（如 `perf c2c`）再改。

## 6. 数据预取

- 对规律访存有效；OB 的微块解码/索引树已有预取（`ObIndexTreeMultiPassPrefetcher`）。
- 对手动随机数组（serialize）预取无效，开销可能大于收益。
- 结论：先确认访存模式，优先用现有预取器/数据结构优化。

## 7. 减少 cluster 间共享（950 线性度）

- 950 的 L3 是 cluster 内 private，跨 cluster 时延高；小模型实验显示跨 cluster 性能影响
影响幅度需按小模型实测（L3 容量减半无变化，主要是时延）。
- 可能的源码方向：减少跨 cluster 的共享数据结构/原子操作（如 `recv_request` 的队列）。
- 风险：架构级改动大，收益有限；需要小模型先验证。

## 8. 已证伪的“热点优化”

| 尝试 | 结果 | 原因 |
| --- | --- | --- |
| jemalloc/tcmalloc | 无收益 | 无 malloc 热点 |
| CRC32 硬件指令 | 无收益 | 无 CRC 热点 |
| KAE | 只写场景收益未量化（需实测） | 只有转储 zstd 路径 |
| 锁微调（预取/isb/自旋） | 无收益 | 锁占比需实测 |
| false sharing 对齐 | 无收益 | 热点占比小 |
| 数据预取 | 无收益 | 随机访存 |
| 手写 NEON 字符串/TSC | 约 0.4%，噪声内 | 热点占比/VID 类型决定 |
| 全量第三方升级 | 读路径≈0 | 收益主要来自 OB 补丁 |

## 9. 补丁落地检查单

- **干净重编纪律**：应用与回退补丁两侧都必须从干净构建目录整树重编（`build.sh` 目录清空重来；OB 构建未启用 `--enable-depend` 类头文件依赖跟踪，只改头文件/公共结构后直接 `make` 可能复用旧对象——实验版仍用旧逻辑、回滚「看似成功」）。重编后安装、重启目标实例，并核产物身份（md5/版本，`scripts/verify_build_opts.sh`）。
- 有热点证据（函数/线程/占比）。
- 改动范围与语义影响写清楚（顺序、可见性、并发、内存）。
- 有开关或可回滚（配置/编译宏/LD_PRELOAD）。
- 正确性：冒烟/计数/事务回读/长稳。
- 性能：五场景/业务模型 A/B，≥3 轮，分场景判定。
- 记录版本/commit/md5/编译参数。
- 负结果入台账。
