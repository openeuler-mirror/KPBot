---
name: kptools-pypto-monitor
description: >
  Use when adding or auditing PyPTO Serving P-node timing, PD_DIAG logs,
  Step and TP attribution, or request timeline continuity.
license: MulanPSL-2.0
compatibility: PyPTO Serving P 节点（已启动，PD_DIAG 诊断环境）
metadata:
  skill_type: debugging
  category: serving
  domain: pypto_serving
  version: "1.0.0"
---

# PyPTO Serving P 节点打点

先追踪当前 checkout 的真实调用。入口在 pypto_serving/pd/integration.py，交接在 pypto_serving/pd/service.py，调度与队列在 pypto_serving/serving/engine/async_engine.py 和 serving/sched/scheduler.py，计算 Worker 在 serving/server/serving_worker.py。复用 pypto_serving/observability/pd_diag.py，核对其消费者 tools/pd_metrics_report.py。

| 阶段 | 边界与归属 |
|---|---|
| req_resolve、tokenizer、add_request | 从 P HTTP handler 到编码开始，覆盖解析、配置、渲染和预检。编码结束后进入 add_request，直到 Scheduler 入队。把 D 端预约的 begin 计入 add_request。 |
| schedule | queue_time 从入队或恢复可调度到成功调度开始。schedule 到 Worker 队列提交边界，schedule.dispatch 延伸到 Worker 开始。hash/lookup/alloc 只记调度内实际调用，入队前的 hash 归 add_request。other 是调度覆盖区间并集减已知明细区间并集。 |
| Worker 与 TP | worker.forward 是计算 Worker 外框。device.forward 和 device.sampling 只取执行 rank 的运行时量值。P 终块需要首 token 时才可能采样。sampling 表示实际的 Host sampler 调用。若执行器已返回 sampled_token_ids，Host 采样留空。 |
| KV 交接 | wait_for_save.prepare 从 Worker 结束开始，包含回收、计划和 D 授权。按 chunk 记录 wait_for_save.put.submit、wait_for_transfer、finalize。末块的 D 确认到 P 生成 ready 归末块 finalize。无 copy 时控制收尾仍计入 prepare，put 留空。 |
| 请求终点 | prefill_ttft 从 tokenizer 开始到 P 收回终块结果。prefill.ready 从 P handler 入口到响应对象生成。Router 首输出、客户端收包均为其他边界。 |

## 日志契约与方法

- 每条是 [PD_DIAG] 加空格，再接单行 JSON。当前 version=2、event=span、role=prefill，记录 span、host、pid、run_id、status。区间用单调时钟 start_ns、end_ns、duration_ns。若设备只有原生 duration，就记录 duration_ns。
- 单请求用 request_id；共享调度用 request_ids、engine_index、step_id；Worker 用实际命令 step_id；设备另带 tp_rank、device_id；KV 交接带 request_id、step_id、chunk。用显式 ID 关联。
- 同节点用 StepResult.worker_interval_ns 传递 Worker 边界，先核实 step ID 和时间顺序。相邻串行段共用端点。异步计算与传输保留各自区间，用并集检查闭环。跨主机或设备时钟不直接相减。批次和并行 rank 不分摊成请求独占耗时。
- 本卡已有 KV 的读取属于 forward。本地 DDR restore 发生实际搬入时才填 load。逐 rank Mooncake 直写应从 owner 执行点取实际耗时，P/D 计划、整组确认仍然共享。未执行、融合或证据不足的时间留空。模拟验证过逐 rank 契约，正式模型运行时接入看当前代码和设备证据。
- 诊断默认关闭。保持 /metrics 和 SA profile 独立。核查关闭时不读钟与请求失败、取消、无 copy 路径。

## 启动与验收

在 P 启动前设置 PYPTO_SERVING_PD_DIAG=1 和 PYPTO_SERVING_PD_DIAG_LOG=/绝对路径/prefill.log。使用当前 checkout 的 pypto-serving 和 pypto-serving-router。真机启动见同目录的 kptools-pypto-deploy，模拟器冒烟见 kptools-pypto-sim-deploy。诊断开关独立于 --profile。

用短、长请求覆盖单 step 和多 step。验证请求、step、rank、ready 和原始日志。观察 hash/lookup 时，用超过一个完整 block 的相同前缀重复发请求，区分实际查询未命中和没执行。对每条成功请求检查入口到 ready 的区间并集。执行 python tests/unit/serving/test_pd_diag.py。交付时区分实机、模拟和替身。生成 HTML 使用 kptools-pypto-monitor-viz。
