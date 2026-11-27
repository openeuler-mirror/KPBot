# 按验证问题定位证据

从用户指定的 PR、版本或社区台账记录进入。只沿与命题相关的路径追查到具体实现、测试和基准，记录最终 SHA、函数和直接链接。

| 验证问题 | 一手入口 | 在实现和测量中核对 |
| --- | --- | --- |
| 某个 CPU 指令或算子路径是否实际命中？ | [oneDNN](https://github.com/uxlfoundation/oneDNN)、[OpenVINO](https://github.com/openvinotoolkit/openvino)、[llama.cpp](https://github.com/ggml-org/llama.cpp) | 运行时 dispatch 条件、实际 CPU ISA、精度和张量形状。用相同输入对照路径、输出正确性和每 token 周期。 |
| 异步拷贝是否从请求关键路径移走了等待？ | [vLLM](https://github.com/vllm-project/vllm)、[SGLang](https://github.com/sgl-project/sglang)、[TensorRT-LLM](https://github.com/NVIDIA/TensorRT-LLM) | 提交线程、内存注册、DMA 方向、消费者同步和缓冲区复用。将 CPU 阻塞、设备开始/完成事件与同一批次的延迟对齐。 |
| 内存放置或线程调度能否改善 CPU 推理？ | 被查后端的分配/调度代码、[Linux 内核文档](https://docs.kernel.org/) | 固定模型权重、线程数、并发和输入，只改变一个绑定或放置条件，记录页放置、远端访问、带宽或尾延迟。 |

厂商文章或模型卡可用于定位版本与上游 PR。复核其中的性能数字时追到原始测量，区分模型发布、后端实现与实际部署结果。
