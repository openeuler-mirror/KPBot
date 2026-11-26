# 官方来源与实现边界

- [Ray 官方 PyTorch ResNet 批预测示例](https://docs.ray.io/en/ray-2.56.0/data/examples/pytorch_resnet_batch_prediction.html)：Ray Data 图像预处理与批处理的官方场景参考；本技能不复制其完整数据/模型流程。
- [Ray 2.56.0 发布说明](https://github.com/ray-project/ray/releases/tag/ray-2.56.0)：实际验证的版本锚点。
- [Ray `from_items`](https://docs.ray.io/en/ray-2.56.0/data/api/doc/ray.data.from_items.html)、[`Dataset.map_batches`](https://docs.ray.io/en/ray-2.56.0/data/api/doc/ray.data.Dataset.map_batches.html) 与 [`ActorPoolStrategy`](https://docs.ray.io/en/ray-2.56.0/data/api/doc/ray.data.ActorPoolStrategy.html)：本次执行的 Dataset/批 Actor 接口。
- [Ray `Dataset.stats`](https://docs.ray.io/en/ray-2.56.0/data/api/doc/ray.data.Dataset.stats.html)：原生执行统计来源。
- [Ray `ray.init`](https://docs.ray.io/en/ray-2.56.0/ray-core/api/doc/ray.init.html) 与 [`ray.shutdown`](https://docs.ray.io/en/ray-2.56.0/ray-core/api/doc/ray.shutdown.html)：自有本地运行时和已有 driver 的生命周期边界；不使用全局 `ray stop`。
- [torchvision ResNet18](https://docs.pytorch.org/vision/0.28/models/generated/torchvision.models.resnet18.html)：`ResNet18_Weights.DEFAULT.transforms()` 的官方预处理定义；只读取元数据，不下载权重。
- [PyTorch 历史版本](https://pytorch.org/get-started/previous-versions/)：torch 2.13/torchvision 0.28 官方版本配对来源。本技能不安装或锁定分发包。
- [Pillow JPEG](https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html#jpeg)：本地合成 JPEG 编解码依据。

确定性 RGB-byte JPEG、逐行 SHA-256 及 Actor 内重算思路参考用户授权的 MulanPSL-2.0 材料并独立改写。未复用原安装器、容器、依赖锁、私有路径或来源 ZIP；新代码继承本仓库 MulanPSL-2.0。
