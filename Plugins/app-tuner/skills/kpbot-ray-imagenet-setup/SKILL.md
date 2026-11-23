---
name: kpbot-ray-imagenet-setup
description: 安装并验收 CPU 模式的 Ray ImageNet 图像预处理测试环境；适用于从空环境构建公开依赖、用合成 JPEG 自检或验证用户已有 ImageNet JPEG 子集，交付版本、正确性结果和运行证据。
---

# Ray ImageNet 场景安装与验收

交付可运行的独立环境，以及 JPEG 解码 → ResNet18 标准预处理 → Ray 批处理 MockActor 的验收结果。MockActor 逐行计算实际收到的 batch tensor 摘要并输出模拟标签，不下载模型权重，不代表真实模型推理或官方完整 ImageNet 基准。

## 输入与环境

Linux、Docker、可访问官方 PyPI 和 PyTorch CPU wheel 索引的网络；构建约需数 GiB 磁盘。运行示例限制为 4 CPU、8 GiB 内存、1 GiB 共享内存，不修改宿主机 Python、内核参数或已有集群。仅连接脚本自己新建的本地 Ray 实例。Python 与依赖版本固定在 Dockerfile、requirements.txt、requirements-lock.txt 和 requirements-cpu-lock.txt；CPU 架构和依赖自检失败即停止，不绕过不匹配版本。

构建和运行需要当前用户可访问 Docker daemon（通常属于 `docker` 组或使用受授权的 root）。容器只读挂载用户数据、写入本次结果目录；安装器不写宿主机 Python、systemd 或全局配置。没有 Docker 权限时返回 `blocked`，不要改用宿主机全局安装。

以下命令在 skill 根目录执行。镜像名和容器名按本次实验命名，不覆盖他人镜像。Dockerfile 基础镜像固定到公开多架构 digest；依赖锁文件为每个安装的 wheel 提供 sha256，构建时使用 `--require-hashes` 且只从官方源下载：PyPI 运行时依赖走 `https://pypi.org/simple`，CPU torch/torchvision 单独走 `https://download.pytorch.org/whl/cpu`。锁文件与来源记录见 [依赖锁定来源](references/lock-provenance.json)。此流程已在 Linux x86_64、Python 3.11.14 环境完成验收；锁文件按该架构冻结，其他架构需按官方索引重新生成并校验锁后再构建。

```bash
docker build -t kpbot-ray-imagenet-setup:test .
docker run --rm --cpus 4 --memory 8g --shm-size 1g kpbot-ray-imagenet-setup:test
```

自检应报告 `status=passed`，版本一致且 torchvision CPU 算子可运行。失败保留构建日志；不继续运行验证。构建后的 `/installed-packages.txt` 是实际解析依赖清单，`/torch-install.json` 和 `/python-install.json` 是 pip 安装报告；归档它们、锁文件来源记录和镜像 digest，复现时使用同一镜像 digest。默认构建只使用官方索引；如需内部镜像，应先独立审查并更新锁文件的受信哈希与来源记录。

网络受限时可使用完整的离线 wheel 缓存路径。先从官方源下载锁文件列出的全部 wheel（仍按锁文件哈希校验），再让构建通过本机回环 HTTP 服务读取它们；`--require-hashes` 在两种路径都生效。默认在线构建不依赖该缓存。

```bash
mkdir -p wheelhouse
python3 -m pip download --only-binary=:all: --require-hashes \
  --index-url https://pypi.org/simple -r requirements-lock.txt -d wheelhouse
python3 -m pip download --only-binary=:all: --require-hashes --no-deps \
  --index-url https://download.pytorch.org/whl/cpu -r requirements-cpu-lock.txt -d wheelhouse
python3 -m http.server 8765 --bind 127.0.0.1 --directory wheelhouse &
CACHE_PID=$!
trap 'kill "$CACHE_PID"' EXIT
docker build --network=host --build-arg PIP_NO_INDEX=1 \
  --build-arg PIP_FIND_LINKS=http://127.0.0.1:8765 \
  -t kpbot-ray-imagenet-setup:test .
kill "$CACHE_PID"
trap - EXIT
```

缓存只应包含锁文件中的 wheel；下载后核对 wheel 文件名和 sha256 与 `references/lock-provenance.json` 一致。清理时只结束本次 `CACHE_PID`，不使用全局 `pkill` 或 Docker prune。

## 无外部数据的小样本验收

```bash
docker run --rm --name kpbot-ray-imagenet-setup-smoke --cpus 4 --memory 8g --shm-size 1g \
  -v "$PWD/results:/skill/results" kpbot-ray-imagenet-setup:test \
  bash cases/image-smoke/run.sh
```

每次运行创建新的 `results/runs/<UTC时间>-<pid>` 结果目录，旧轮次产物保持不变；`results/image-smoke/result.json` 是供用例断言使用的当前结果索引，会更新为本次成功或失败结果。需要显式目录时运行 `python cases/image-smoke/check.py --output-dir results/my-run`，目标已存在会以退出码 2 拒绝并提示换目录。不要把不同实验复用同一个结果目录。

用例生成 16 张确定性 JPEG（重复生成逐字节一致），经真实 Ray Data 执行；逐行比较串行参考的预处理 tensor SHA-256、shape、有限数值及模拟标签。MockActor 在 Actor 内对实际收到的每行 tensor 重新计算 SHA-256，因此同形状 tensor 错位会被发现。还验证类 ImageNet 的嵌套目录、大小写后缀和非 JPEG 忽略、损坏/缺失/空 JPEG、改名为 `.jpg` 的 PNG、越界符号链接、非法参数、像素上限与解码内存预检、已有输出不被覆盖、外层超时返回 124，并检查测试结束后没有残留 Ray 进程。读取本次结果目录的 `result.json`（或 `results/image-smoke/result.json`），必须 `success=true`；同时保留输入清单、逐行指纹、Dataset stats、stdout/stderr。合成数据不属于 ImageNet，也不证明全量数据、模型推理或性能收益。

## 验证已有 ImageNet 数据

使用用户已取得、已解包的 JPEG 目录。该脚本是有界安装验收，不承担 80 万张图片的全量 benchmark。它递归选择排序后的前 `--limit` 张（默认 16，最多 10000），先校验每个候选项解析后的真实路径仍在输入根目录内，并按内容确认 Pillow 识别为 JPEG；越界符号链接、改名 PNG 和无法解码的文件会报告对应输入并在创建输出目录前失败。随后在创建输出目录前读取原图尺寸，把解码 RGB、RGB 转换缓冲区和输出 tensor 计入估算，并执行明确的 `--max-pixels`（默认 50000000）与 `--max-estimated-mib`（默认 4096）上限。先小样本再扩大，不要用默认上限直接处理完整 ImageNet。

```bash
docker run --rm --name kpbot-ray-imagenet-setup-check --cpus 4 --memory 8g --shm-size 1g \
  -v "$IMAGENET_DIR:/input:ro" -v "$PWD/results:/skill/results" kpbot-ray-imagenet-setup:test \
  python scripts/run_pipeline.py --input /input --limit 16 --batch-size 4 \
  --cpus 4 --max-estimated-mib 4096 --max-pixels 50000000 --timeout 180 \
  --output results/user-001
```

`IMAGENET_DIR` 指向用户实际数据目录；`/input` 是该命令明确创建的容器挂载位置。用户入口默认把数据记录为 `user-provided-jpeg-directory`，即使目录中碰巧存在 `fixture.json` 也不会改标为合成数据；只有合成验收入口显式传入 `--dataset-kind synthetic` 且校验 `make_fixture.py` 写入的标记后才记录为 `synthetic`。输出目录必须不存在；每次实验换用新的 `--output`。缺失、空目录、无 JPEG、无法解码、越界链接、改名 PNG 或结果不一致时失败，不伪造数据、不跳过坏图。输入只读挂载。命令超时会终止自身进程组，容器 `--rm` 在退出后回收本次 Ray 进程。被中断后核对这个确切容器名，不使用全局 `ray stop`、`pkill` 或 Docker prune。

填写[安装与验收记录](references/record.md)，注明数据是合成还是用户数据，不能将行数通过写成完整 ImageNet 已验证。脚本退出 0 表示通过，1 表示输入/依赖/运行/内存预检失败，参数错误或结果目录已存在退出 2，外层等待超时退出 124。参数错误、输入校验和内存/像素预检失败发生在创建输出目录之前；进入执行后的失败会在 stderr 和已创建输出目录的 result.json 中记录，超时可能只有部分文件或没有结果文件。

公开依据与依赖边界见[来源](references/sources.md)。环境安装与本次正确性验收到此结束；需要定位吞吐瓶颈时，将这些产物交给性能分析流程，不自动打框架补丁。
