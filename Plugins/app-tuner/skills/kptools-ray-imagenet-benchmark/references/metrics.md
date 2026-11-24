# 指标、正确性与结果契约

## 测量边界

一轮的墙钟区间从 `ray.data.from_items` 构造开始，到 `take_all()` 返回全部输出结束，覆盖 Ray Data 调度、JPEG 解码、ResNet18 transforms、batch Actor digest 和结果收回。`dataset.stats()` 读取与文件序列化在墙钟停止后进行。Ray 初始化、fixture 生成、串行参照、预热以及后处理都不进入 `wall_elapsed_ms`。

默认先执行 1 个完整 warmup，再执行 3 个 timed rounds。预热同样验证正确性并保存证据，但其耗时从不进入 summary。轮次和图片数固定，不按“运行多久”无限追加。

| 字段 | 单位/公式 | 范围 |
| --- | --- | --- |
| `wall_elapsed_ms` | 单调时钟差 ×1000，毫秒 | 每轮端到端 Dataset 区间 |
| `throughput_images_per_second` | `output_rows × 1000 / wall_elapsed_ms` | 每轮合成 JPEG，不是业务图片 |
| `transform_latency_ms` | 每张图在 UDF 内的解码+transform 墙钟 | min/median/max；不与端到端耗时相加 |
| `actor_batch_latency_ms_per_row` | Actor 重算 batch digest 的耗时，写入该批每行 | 只表示 digest，不是模型推理 |
| `native_dataset_stats` | Ray 原生 `Dataset.stats()` 原文 | 保留 Ray 自己的计数、阶段与单位 |
| `input_bytes` / `output_rows` | 字节 / 图片行 | fixture 与实际输出计数 |

summary 的 min/median/max 只来自 timed rounds；吞吐每轮先计算再汇总，不把多轮混成未标口径的平均数。原始样本数组保留，便于复算。UDF 时间可重叠，不能据此把各阶段占比相加到 100%。

## 精确正确性

串行参照对同一 JPEG bytes 独立执行 transforms。Ray UDF 先计算 tensor digest，Actor 再对**批处理后实际收到的 tensor** 重算 digest，避免仅转发上游 digest 而漏掉错位、交换或 dtype 变化。通过要求：

1. 输出行数与输入完全一致，ID 唯一且集合一致；
2. 每个 ID 的 post-batch SHA-256 与串行参照完全一致；
3. tensor shape 为 `(N,3,224,224)`、dtype `float32`、全部有限；
4. 每行 transform/actor 延迟有限且非负；
5. Ray 原生 stats 非空；
6. 父进程复核成功状态、正确性、预热/计时轮数和 warmup 排除标记。

任何一轮失败都停止后续测量并返回非零；已成功或失败的轮次、stats、rows、stdout/stderr 原样保留。不完整轮次不进入成功 summary。

## 文件

- `fixture/`：本轮生成的 JPEG 与含 SHA-256 的 `fixture.json`；`is_imagenet=false`。
- `serial-reference.json`：独立参照 digest。
- `warmup-*`、`timed-*`：每轮原生 stats 与原始行证据。
- `child-result.json`：被测 child 的结果；`result.json`：父进程监督、脱敏和语义复核后的最终结果。
- `stdout.log` / `stderr.log`：本轮日志；声明的认证值会脱敏。

`result.json` 中 `success=true` 必须同时有 `correctness=true`、完整 timed rounds 和有效 summary。该 smoke 不构造模型、不下载参数、不计算标签，`full_imagenet_accuracy=null`。
