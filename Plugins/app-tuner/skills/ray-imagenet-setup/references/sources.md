# 公开来源与边界

- [Ray 官方图像批推理示例](https://docs.ray.io/en/latest/data/examples/pytorch_resnet_batch_prediction.html)：图像预处理、batch actor 的公开接口示例。本 skill 独立编写有界安装验收，不复制特定实验的框架补丁。
- [Ray 2.52.0 发布](https://github.com/ray-project/ray/releases/tag/ray-2.52.0)：固定框架版本；latest 文档可能比安装版本新，命令以本次安装自检及用例为准。
- [PyTorch 历史版本安装](https://pytorch.org/get-started/previous-versions/)：torch 2.6.0 / torchvision 0.21.0 CPU 配套安装源。
- [ResNet18 权重预处理定义](https://docs.pytorch.org/vision/0.21/models/generated/torchvision.models.resnet18.html)：使用 DEFAULT 的 transforms，读取预处理参数不会下载权重。本 skill 不调用预训练模型构造器。
- [Python 官方容器镜像](https://hub.docker.com/_/python)：Dockerfile 固定版本与 digest。网络不通时可在同架构联网机器构建、导出自己构建的镜像并校验后转入，不依赖未提供的离线总包。

数据由用户提供或由随附生成器在本地生成，未提供 ImageNet 下载器。仅声称实际执行过的架构和数据规模；本流程不安装 SVE/native、HTTP 或 Ray 私有接口补丁。
