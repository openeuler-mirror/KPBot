# DiskANN 性能采集

文中命令相对所属 skill 根目录执行。

## 执行流程

1. 确认存在有效的 `diskann-benchmark-baseline` Manifest。
2. 读取 [profile-matrix.md](profile-matrix.md)，选择能够区分当前假设的最小证据集。
3. 执行 `scripts/profile_run.sh --out-dir <dir> -- <benchmark command...>` 完成第一轮联合采集。
   默认先探测 perf 权限和事件；不可用时执行一次原命令并保存缺口。必须采 perf 时传 `--perf required`，关闭时传 `--perf off`；不能把未采到计数的运行当作完整 profile。
4. 仅针对仍未解决的问题追加 `perf record`、调用图、反汇编或源码阶段打点。
5. 保存原始输出、完整命令、退出码和时间戳；不得只保留汇总结论。
6. 将完整证据目录交给 `diskann-source-analysis` 的瓶颈分析步骤。

## 约束

- Profiling 必须沿用基线的绑核和数据集契约。
- 测量采集工具自身开销；重型 tracing 应单独作为诊断轮次。
- worker 阻塞在系统调用或处于混合阶段时，高 CPU 占用不能单独证明 CPU bound。
- 设备利用率不能单独证明有效 I/O；必须关联实际读请求、时延、队列深度和应用等待时间。

## 按需资源

- [profile-matrix.md](profile-matrix.md)：根据待区分假设选择最小采集工具和指标时读取。
- `scripts/profile_run.sh`：沿用冻结基线执行联合采集并保存原始产物。
