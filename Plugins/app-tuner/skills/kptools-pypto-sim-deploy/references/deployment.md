# a2a3sim 冒烟与测试替身

## 环境

选择 Linux CPU 容器和匹配的 PyPTO、Simpler、PTOAS、pypto-lib 版本。CannLab 的连接方式以本机 OpenCode cannlab agent 配置为准。CPU 模拟可在持久容器内运行，通常不需要开 NPU Pod。给服务、原生 trace、诊断日志和工作目录分配本次测试的独立位置。

CPU 上仅在实际遇到相应 Python ABI 问题时使用该环境的 pyfix312。不同 Python 版本分别核对依赖和 PYTHONPATH。hello_world 编译或运行失败时，先检查实际加载的 PyPTO、Simpler、PTOAS、编译器与 pypto-lib pin，再定位编译、模拟执行或对拍失败。使用本次运行的退出码与 PASS 作为证据。

## 模拟验收

1. 在当前 pypto-lib 跑 examples/beginner/hello_world.py --sim，校验 Simpler、PyPTO、PTOAS 及编译依赖，检查实际内核运行与对拍 PASS。
2. 若用户接受测试替身，可启动真实 ServingServer，或真实 P 节点 Server、Scheduler 和 Worker，注入测试模型、tokenizer、执行器。Worker spawn 后也应用同一替身。把测试内核接到 Worker Prefill 里，HTTP 200 和 Worker 内核 PASS 同时验收。
3. 需要逐 rank 时，用两个明确的逻辑设备并发运行测试内核，分别取原生 device_wall。两个独立内核只能验证 rank 归属，模型 collective 需专门验证。设备采样可在终块运行 Qwen3-14B greedy 内核，回收实际 sampled_token_ids，并通过测试断言 Host sampler 没有参与。
4. 没有真实 D 时，可用测试脚本替换 D 的预约、搬运和确认，保留 P 的真实入口、调度及 Worker。验证短请求、跨 chunk 请求、实际采样位置与 P 入口到 ready 的区间覆盖。标注测试 logits、D/KV 替身和实际内核。

本次已验证模拟器上的 P HTTP、双逻辑 rank、设备 greedy 采样和计时闭环。真实 checkpoint、生产逐 rank 接入和实卡 P/D 需要独立验证。
