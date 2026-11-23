# TTFT 报告口径

- `BREAKDOWN` 提供请求级 `ttft`、`forward` 等汇总。`EngineCore.forward` 提供执行级总耗时，每个执行仅展示一次。转发、模型准备和设备内核的时间不能从总耗时拆出。
- `wait_for_save`、`enqueue` 和 `join` 是每个 TP 的调用边界。异步保存任务有独立的 `q_wait`、`build`、`lookup`、`prepare`、`sync` 和 `put`。主机计时可能重叠，避免相加重建 `wait_for_save`。
- `put (s)` 是现有 `backend.put` 调用的主机侧总耗时。`keys` 与 MiB 是报告关联到的提交记录。它们的统计范围与 Native Mooncake 阶段计时或设备传输计时不同。
- 优先使用明确的请求 ID、`execution_id`、TP、PID 等关联字段。缺少执行身份的旧边界作为未绑定证据展示。缺失、冲突和歧义均保留，实测零单独显示。避免按日志顺序推断归属。
- Tokenizer 只将有明确请求归属、输入计数 `n` 为正整数且耗时合法的业务测量用于残差。`n` 表示输入 prompt 数。手动选择的旧证据仅展示。

默认保留所有可识别 TP，`--tp-rank N` 仅筛选显示的 TP。`--mode all` 可展示其他可关联执行，`--mode prefill` 用于 Prefill。日志缺少必需标记时，`--force` 可尝试根据尚存的请求级 `BREAKDOWN` 生成报告。保留预检输出中的缺失项，报告按部分证据解读。

离线检查和夹具验证的是标记、关联与渲染。验证 P/D KV 传输需检查同一真实请求的 P 端发送和 D 端接收完成事件。核对外部缓存时，保存路径的查重结果不能单独证明加载成功。
