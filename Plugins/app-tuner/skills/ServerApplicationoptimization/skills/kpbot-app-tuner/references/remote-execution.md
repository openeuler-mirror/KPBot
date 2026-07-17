# 远程执行模式

当优化目标与执行环境分离时（如通过 SSH 操作远程服务器，或客户端压测机与服务端分离部署），需遵循以下规则。

## 监控-压测同步

**禁止**手动协调多个终端分别启动监控和压测——这极易导致监控数据缺失测试窗口。

统一使用配套脚本编排监控和压测的生命周期：

```bash
# 本地模式
scripts/run_benchmark_with_monitor.sh \
  --pid 12345 --core-list "32-39" \
  --command "<benchmark-command>" \
  --output-dir /tmp/run1

# 远程模式（SSH）
scripts/run_benchmark_with_monitor.sh \
  --pid 12345 --core-list "32-39" \
  --ssh-server root@192.168.90.170 \
  --ssh-client user@192.168.90.105 \
  --remote-command "<benchmark-command>" \
  --output-dir /tmp/run1
```

脚本关键设计：
- 与具体压测工具解耦（通过 `--command` / `--remote-command` 传入任意压测命令）
- 自动编排：启动监控 → 等待稳定 → 预热 → 压测 → 额外采集 → 停止监控 → 汇总结果
- 自动设置 `LC_ALL=C` 确保监控工具输出可解析
- 远程模式下自动通过 SCP 拉取服务端和客户端监控数据

## 多机操作原则

- 服务端监控（mpstat/pidstat/iostat/sar）在服务端执行
- 客户端 CPU 监控在客户端执行
- 压测命令在客户端执行
- 所有监控数据最终拉取到统一的 `--output-dir`

## SSH 注意事项

- 命令过长时拆分为多条 SSH 调用，避免 shell 转义问题
- 后台任务使用 `nohup ... &` 并记录 PID
- 采集结果用 `scp` 拉取，而非在远程端拼接
- 监控启动后必须等待 3-5 秒再启动压测，确保数据窗口完整覆盖
