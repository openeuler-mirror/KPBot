# 官方来源与实现边界

- [Ray 2.56.0 发布说明](https://github.com/ray-project/ray/releases/tag/ray-2.56.0)：本技能实际验证的 Ray 版本锚点。
- [Ray `ray.init`](https://docs.ray.io/en/ray-2.56.0/ray-core/api/doc/ray.init.html)：连接已有地址或初始化本地运行时的接口语义。
- [Ray `ray.shutdown`](https://docs.ray.io/en/ray-2.56.0/ray-core/api/doc/ray.shutdown.html)：driver 断开/本地运行时关闭接口；本技能从不执行全局 `ray stop`。
- [Ray `from_items`](https://docs.ray.io/en/ray-2.56.0/data/api/doc/ray.data.from_items.html) 与 [`Dataset.map_batches`](https://docs.ray.io/en/ray-2.56.0/data/api/doc/ray.data.Dataset.map_batches.html)：本次实际检查的参数。
- [Ray `ActorPoolStrategy`](https://docs.ray.io/en/ray-2.56.0/data/api/doc/ray.data.ActorPoolStrategy.html)：有界单 Actor compute 配置。
- [PyTorch 历史版本](https://pytorch.org/get-started/previous-versions/)：torch/torchvision 版本配对与官方安装来源；本技能不安装依赖。
- [torchvision ResNet18](https://docs.pytorch.org/vision/0.28/models/generated/torchvision.models.resnet18.html)：`ResNet18_Weights.DEFAULT.transforms()` 预处理定义；读取 transforms 不下载模型参数。
- [Python 3.11 subprocess](https://docs.python.org/3.11/library/subprocess.html)：有限等待、非 shell 参数传递与进程控制依据。

版本检查和 CPU 预处理探针参考了用户授权的 MulanPSL-2.0 参考材料并独立改写；未复用安装器、容器定义、依赖锁或来源 ZIP。新代码继承本仓库 MulanPSL-2.0。最新文档可能描述不同接口，现场以报告记录的实际版本及本次 API 探针为准。
