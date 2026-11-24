# 公开来源与边界

- [Ray 官方图像批推理示例](https://docs.ray.io/en/latest/data/examples/pytorch_resnet_batch_prediction.html)：图像预处理、batch actor 的公开接口示例。本 skill 独立编写有界安装验收，不复制特定实验的框架补丁。
- [Ray 2.56.0 发布](https://github.com/ray-project/ray/releases/tag/ray-2.56.0)：固定框架版本；latest 文档可能比安装版本新，命令以本次安装自检及用例为准。
- [PyTorch 历史版本安装](https://pytorch.org/get-started/previous-versions/)：torch 2.13.0 / torchvision 0.28.0 CPU 配套安装源。
- [官方 PyPI Simple 索引](https://pypi.org/simple)：运行时依赖的唯一默认包索引；每个 wheel 的 sha256 记录在 `requirements-lock.txt`，构建时以 `--require-hashes` 校验。
- [官方 PyTorch CPU wheel 索引](https://download-r2.pytorch.org/whl/cpu)：CPU torch/torchvision wheel 的唯一默认来源；对应 sha256 记录在 `requirements-cpu-lock.txt`。
- [ResNet18 权重预处理定义](https://docs.pytorch.org/vision/0.28/models/generated/torchvision.models.resnet18.html)：使用 DEFAULT 的 transforms，读取预处理参数不会下载权重。本 skill 不调用预训练模型构造器。
- [Python 官方容器镜像](https://hub.docker.com/_/python)：Dockerfile 固定版本与 digest。网络不通时可在同架构联网机器构建、导出自己构建的镜像并校验后转入。

## 依赖锁定来源

`requirements-lock.txt` 的每个包和 wheel 哈希来自对应版本的官方 PyPI JSON API（例如 `https://pypi.org/pypi/ray/2.56.0/json`），并只选择 CPython 3.11 Linux x86_64 可安装的 wheel；sdist 不进入锁文件。`requirements-cpu-lock.txt` 的 torch/torchvision 哈希取自官方 CPU 索引链接中的 SHA-256 片段，并与 pip 目标环境解析报告互核，URL 指向 `download-r2.pytorch.org` 的官方 CPU wheel。逐项来源、wheel 文件名和 sha256 见 `references/lock-provenance.json`。锁文件面向 Linux x86_64 / CPython 3.11 冻结；其他架构需重新从官方索引解析并校验，不能沿用本锁。

数据使用用户已有 JPEG 目录，或由随附生成器在本地生成。运行流程使用标准 Ray 和 torchvision 接口，验证范围为实际执行过的架构和数据规模。
