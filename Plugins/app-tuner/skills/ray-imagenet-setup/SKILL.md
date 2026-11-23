---
name: ray-imagenet-setup
description: 安装并验收 CPU 模式的 Ray ImageNet 图像预处理测试环境；适用于从空环境构建公开依赖、用合成 JPEG 自检或验证用户已有 ImageNet JPEG 子集，交付版本、正确性结果和运行证据。
---

# Ray ImageNet 场景安装与验收

交付可运行的独立环境，以及 JPEG 解码 → ResNet18 标准预处理 → Ray 批处理 MockActor 的验收结果。MockActor 只校验批次并输出模拟标签，不下载模型权重，不代表真实模型推理或官方完整 ImageNet 基准。

## 输入与环境

Linux、Docker、可访问公开 Python 镜像/PyPI/PyTorch CPU wheel 索引的网络；构建约需数 GiB 磁盘。运行示例限制为 4 CPU、8 GiB 内存、1 GiB 共享内存，不修改宿主机 Python、内核参数或已有集群。仅连接脚本自己新建的本地 Ray 实例。Python 与依赖版本固定在 Dockerfile、requirements.txt 和 requirements-lock.txt；CPU 架构和依赖自检失败即停止，不绕过不匹配版本。

构建和运行需要当前用户可访问 Docker daemon（通常属于 `docker` 组或使用受授权的 root）。容器只读挂载用户数据、写入本次结果目录；安装器不写宿主机 Python、systemd 或全局配置。没有 Docker 权限时返回 `blocked`，不要改用宿主机全局安装。

以下命令在 skill 根目录执行。镜像名和容器名按本次实验命名，不覆盖他人镜像。Dockerfile 基础镜像固定到公开多架构 digest；CPU torch/torchvision 单独从 PyTorch CPU 索引安装，其余依赖按 `requirements-lock.txt` 固定；CPU wheel 的本地版本后缀记录在自检与安装报告中，不写入公共锁文件。此流程已在 Linux x86_64、Python 3.11.14 环境完成验收，其他架构须现场运行自检与用例。

```bash
# 默认使用官方 PyPI
docker build -t kpbot-ray-imagenet:test .
# 官方 PyPI 传输慢时的可选参数；本次验证构建使用这个公共华为云镜像
docker build --build-arg PYPI_INDEX_URL=https://repo.huaweicloud.com/repository/pypi/simple \
  -t kpbot-ray-imagenet:test .
docker run --rm --cpus 4 --memory 8g --shm-size 1g kpbot-ray-imagenet:test
```

自检应报告 `status=passed`，版本一致且 torchvision CPU 算子可运行。失败保留构建日志；不继续运行验证。构建后的 `/installed-packages.txt` 是实际解析依赖清单，`/torch-install.json` 和 `/python-install.json` 是 pip 安装报告；归档它们和镜像 digest，复现时使用同一镜像 digest。默认 `PYPI_INDEX_URL=https://pypi.org/simple`，换镜像只改变 Python 包下载源，不改变 CPU torch 索引或锁定版本。

## 无外部数据的小样本验收

```bash
mkdir -p results
docker run --rm --name kpbot-ray-imagenet-smoke --cpus 4 --memory 8g --shm-size 1g \
  -v "$PWD/results:/skill/results" kpbot-ray-imagenet:test \
  bash cases/image-smoke/run.sh
```

用例生成 16 张确定性 JPEG（重复生成逐字节一致），经真实 Ray Data 执行；逐行比较串行参考的预处理 tensor SHA-256、shape、有限数值及模拟标签。还验证类 ImageNet 的嵌套目录、大小写后缀和非 JPEG 忽略、损坏/缺失/空 JPEG 输入、非法参数、内存预检、已有输出不被覆盖、外层超时返回 124，并检查测试结束后没有残留 Ray 进程。读取 `results/image-smoke/result.json`，必须 `success=true`；同时保留输入清单、逐行指纹、Dataset stats、stdout/stderr。合成数据不属于 ImageNet，也不证明全量数据、模型推理或性能收益。

## 验证已有 ImageNet 数据

使用用户已取得、已解包的 JPEG 目录。该脚本是有界安装验收，不承担 80 万张图片的全量 benchmark。它递归选择排序后的前 `--limit` 张（默认 16，最多 10000），按文件大小估算 driver 参考张量、对象存储和 worker batch 的内存；超过 `--max-estimated-mib`（默认 4096）时在创建输出目录前失败。先小样本再扩大，不要用默认上限直接处理完整 ImageNet。

```bash
docker run --rm --name kpbot-ray-imagenet-check --cpus 4 --memory 8g --shm-size 1g \
  -v "$IMAGENET_DIR:/input:ro" -v "$PWD/results:/skill/results" kpbot-ray-imagenet:test \
  python scripts/run_pipeline.py --input /input --limit 16 --batch-size 4 \
  --cpus 4 --max-estimated-mib 4096 --timeout 180 --output results/user-001
```

`IMAGENET_DIR` 指向用户实际数据目录；`/input` 是该命令明确创建的容器挂载位置。缺失、空目录、无 JPEG、无法解码或结果不一致时失败，不伪造数据、不跳过坏图。输出目录必须不存在；输入只读挂载。命令超时会终止自身进程组，容器 `--rm` 在退出后回收本次 Ray 进程。被中断后核对这个确切容器名，不使用全局 `ray stop`、`pkill` 或 Docker prune。

填写[安装与验收记录](references/record.md)，注明数据是合成还是用户数据，不能将行数通过写成完整 ImageNet 已验证。脚本退出 0 表示通过，1 表示输入/依赖/运行/内存预检失败，参数错误退出 2，外层等待超时退出 124。参数错误和内存预检失败发生在创建输出目录之前；进入执行后的失败会在 stderr 和已创建输出目录的 result.json 中记录，超时可能只有部分文件或没有结果文件。

公开依据与依赖边界见[来源](references/sources.md)。环境安装与本次正确性验收到此结束；需要定位吞吐瓶颈时，将这些产物交给性能分析流程，不自动打框架补丁。
