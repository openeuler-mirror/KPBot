---
name: kptools-ray-imagenet-benchmark
description: 当需要对已有 Ray/Python/torchvision 运行环境执行有界 Ray ImageNet 预处理基准 smoke、测量图像吞吐与延迟并严格核验结果时使用。生成确定性微型 JPEG，现场复查前置，默认独立预热与至少三轮计时，交付原生 Ray stats、原始行证据和 JSON；不下载权重，不冒充完整 ImageNet 精度或生产压测。
license: MulanPSL-2.0
metadata:
  skill_type: tool_assisted
  category: benchmark
  domain: ray_imagenet
  version: 1.0.0
  compatibility: Linux；Python 3.11 与本技能声明的单一已验证 Ray/torchvision CPU 运行时
---

# Ray ImageNet 有界预处理 Benchmark

## 范围

这是一个 **synthetic JPEG → torchvision ResNet18 标准预处理 → Ray Data 批处理 digest** 的真实执行 smoke。默认启动并拥有一个本地 Ray 运行时，在 `finally` 关闭它；显式 `--address` 可连接已有服务，此时只断开本次 driver。绝不执行全局 `ray stop`，不读取或停止其他技能/服务。

合成图不是 ImageNet；`ResNet18_Weights.DEFAULT.transforms()` 只读取预处理元数据，不下载权重，也不执行模型推理。因此结果不包含、也不能推导完整 ImageNet top-1/top-5 准确率、训练效果或生产容量。

门槛只声明实际验证的 Python 3.11.x、Ray 2.56.0、torch 2.13.0+cpu、torchvision 0.28.0+cpu、NumPy 2.1.3、PyArrow 23.0.1、Pillow 13.0.0。其他版本 fail closed 并记录 `wrong_version`，不声称其上游必然不兼容。技能自带全部前置代码，不依赖 sibling skill 或旧报告。

## 默认执行

在本 skill 目录运行，输出目录必须尚不存在：

```bash
python3 scripts/benchmark.py --output results/smoke-001
```

默认 8 张确定性小 JPEG、batch size 4、2 CPU、256 MiB 对象存储、1 轮预热、3 轮计时、总超时 240 秒。预热、fixture 生成、串行参照和 Ray 初始化不进入计时汇总。可调整但均有硬边界：`--images 4..64`、`--batch-size 1..16`、`--cpus 2..16`、`--warmup 1..5`、`--rounds 3..20`、`--timeout 1..900`。没有无限时间/无限轮数选项。

连接已有服务时，显式给出不含凭据的端点：

```bash
python3 scripts/benchmark.py --output results/cluster-smoke-001 \
  --address ray://ray-head.example.com:10001 --rounds 3
```

认证/TLS 只使用目标 Ray 版本支持的安全文件或 `RAY_*` 环境变量；不得把密码写入 argv 或地址。日志、异常和 JSON 对声明的认证值脱敏。

## 正确性与指标

每轮重新执行 Dataset，Actor 对实际收到的 batch tensor 逐行重算 SHA-256，与独立串行预处理参照按 ID 精确比较。行数、唯一 ID、dtype/shape、有限数值和 digest 任一不符都失败；失败轮次保留原始 rows/stats，不产生成功汇总。

每轮保存：

- `wall_elapsed_ms`：`from_items` 到 `take_all` 的墙钟时间；
- `throughput_images_per_second = 输出图片数 × 1000 / wall_elapsed_ms`；
- 图片数、输入字节、逐行 transform 延迟、Actor batch 延迟；
- Ray 原生 `Dataset.stats()` 原文及原始 digest 行文件；
- 最终 `result.json`，其 summary 只聚合通过的正确计时轮。

详见 [metrics 与结果契约](references/metrics.md) 和 [官方来源](references/sources.md)。这些数值仅用于该确定性 smoke 的复现，不是 TPC、完整 ImageNet 或业务性能声明。

## 失败、清理与退出码

父进程创建独占输出目录和 0600 日志，子进程位于自有进程组。supervisor 在创建进程前构造独立的环境白名单，仅保留运行所需的系统路径/语言/临时目录、Python 导入与编码、TLS、Python/原生线程设置和 明确列出的 Ray TLS/认证与运行配置（不按前缀整体放行），以及内部 child 标记；任意其他凭据或 Secret 不进入 child 及其派生的 Ray worker，且不修改父进程 `os.environ`。超时只终止该组；本地 Ray 在正常异常路径由 `finally` 关闭，自有 session 目录只清理本轮创建项。Ray 的 AF_UNIX socket 需要足够短的系统临时目录；可通过本次进程的 `TMPDIR` 指定短路径，失败时保留证据而不删除他人 socket。已有输出永不删除或覆盖。

- `0`：全部前置、预热、计时轮和父侧语义校验通过；
- `1`：前置、认证、运行时、正确性或结果契约失败，保留失败结果/日志；
- `2`：参数错误或输出碰撞；
- `124`：总超时；`130`：中断。两者均尽力保留结构化结果与日志。

## 回归与真实验证

```bash
python3 scripts/test_contract.py --result results/contract-001.json
python3 scripts/test_contract.py --result results/e2e-check-001.json \
  --e2e-result results/smoke-001/result.json
```

第一条执行 tier-A，第二条额外重算 fixture 哈希、比较全部 raw rows 与串行参照、核对原生 stats 和 timed-only summary。两条中的 mock 都只证明契约；真实 E2E 证据来自先执行的默认 benchmark。tier-A 覆盖成功语义、错误/缺失版本、工具缺失、认证分类、真实短命子进程超时/失败、输出碰撞、预热排除、严格正确性和脱敏。真实 E2E 必须在匹配运行时执行，并确认 `success=true`、`correctness=true`、1 个 warmup、至少 3 个 timed rounds、stats 非空且无全局清理。
