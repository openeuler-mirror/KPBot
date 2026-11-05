请使用 ascend-vllm-profiler skill 分析这份 NPU 推理 trace。

要求：
1. trace 文件：input/trace_view.json（torch_npu profiler 导出的 chrome trace 格式）。
2. 回答三个问题：① ProfilerStep#1 窗口的四分类占比；② ProfilerStep#2 窗口的四分类占比；③ 函数 forward_core（ProfilerStep#1 内唯一实例）时段的四分类占比。
3. 四分类 = Computing / Communication / Communication(Not Overlapped) / Free，单位为百分比，保留两位小数；口径满足 Computing + 未重叠通信 + Free = 100%。
4. 结果写入 opt/answer.json，JSON schema：
   {"step1": {"computing": 0.0, "communication": 0.0, "comm_not_overlapped": 0.0, "free": 0.0},
    "step2": {"computing": 0.0, "communication": 0.0, "comm_not_overlapped": 0.0, "free": 0.0},
    "forward_core": {"computing": 0.0, "communication": 0.0, "comm_not_overlapped": 0.0, "free": 0.0}}
5. 不要修改 input/ 下的任何文件，不要写 opt/answer.json 以外的产物文件。
6. 完成后用一句话说明你的分析方法。
