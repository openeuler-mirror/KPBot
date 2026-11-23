# 采集操作指南 — collector 的时机、长任务与跨机取回

> SKILL.md 是速查；本文逐步展开。命令层标注：目标机 = 运行采样器
> 与被观测进程的机器（Linux）。

## 1. 采什么、为什么这样采

| 通道 | 数据源 | 说明 |
|------|--------|------|
| A 配置面 | `/proc/PID/task/*/status` 的 `Cpus_allowed_list` | 允许掩码时间线：绑核"配置"是否生效、何时被谁改了 |
| B 实际面 | `/proc/PID/task/*/stat` 的 processor(39)/utime(14)/stime(15) | 实际落点直方图 + 忙闲（活跃样本 = 相邻在场 tick 增量/时窗 ≥20%） |

采集本身只读 /proc，每线程每 tick 两个小文件——对目标的扰动可忽略。

## 2. 时机选择

| 时点 | 价值 |
|------|------|
| warmup 后稳态 | 主用例：掩码/线程清单已稳定，事件日志干净 |
| 启动后立即 | 捕捉框架初始化期的线程出生与绑核动作（事件密集） |
| 训练阶段切换 | 掩码/线程可能变化（--diff 对比） |

- 首个 tick 时全部在册线程记为新线程（事件日志语义），属预期行为
- 生存期短于一个采样间隔的线程采不到（统计与事件日志均提示）

## 3. 长任务模式

```bash
# 1) 启动（目标机上）
bash collect.sh --auto -T 7200 --out /tmp/af_session1 &
# 2) 观察日志（tail -f trace.log 直接看事件）
tail -f /tmp/af_session1/trace.log
# 3) 结束后（自动落盘）直接取走输出目录
```

- run 模式结束后自动写终态名册与统计；Ctrl-C（信号）也会写 end 行 +
  名册，已采数据完整保留
- 长间隔轮询注意目标机自身的探活机制（空闲自动关机型环境）

## 4. 跨机取回（collector 与 analyzer 分机部署）

典型流程：目标机（NPU 训练机）只装 collector 采集，把输出目录
（trace.log + samples.jsonl + topology_raw.txt）拷回分析机，在分析机上
运行 `kptools-thread-affinity-analyzer` 出 HTML 报告。

- **必须带上 topology_raw.txt**：NUMA/NPU 归属以采样机快照为准，
  分析机没有它时报告的归属列降级
- 样本目录是纯文本，scp/rsync/压缩包均可

## 5. 超时与重试

| 场景 | 建议 |
|------|------|
| 交互前台跑 | `-T` 设定窗口，结束后自动落盘；Ctrl-C 亦可 |
| 长任务后台跑 | `nohup bash collect.sh ... &`，用 `tail -f trace.log` 观察 |
| 采样间隔 | 常规 0.5s；迁移/抖动诊断 0.1s；下限 0.05s |

## 6. 与 analyzer 的对接

- 输出目录直接作为 analyzer 的参数：`analyze.py <输出目录>`
- 数据契约（samples.jsonl 行格式 / topology 分节 / trace.log 事件类型）
  见 analyzer 的 `references/data-contract.md`
- 联合判读：内存侧分析（train-mem-affinity）同样消费 topology_raw.txt
