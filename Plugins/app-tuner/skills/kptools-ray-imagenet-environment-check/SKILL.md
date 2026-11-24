---
name: kptools-ray-imagenet-environment-check
description: 当需要在 Ray ImageNet 预处理基准前只读检查现有 Python/Ray/torchvision 安装、非默认 Ray 端点、认证或 CPU/共享内存/内存/磁盘前置时使用。交付逐探针 JSON 证据与 ready/blocked/unknown 结论；不安装软件、不启动或停止服务，也不以端口连通或导入成功冒充可用。
license: MulanPSL-2.0
metadata:
  skill_type: tool_assisted
  category: environment_check
  domain: ray_imagenet
  version: 1.0.0
  compatibility: Linux；已安装的 Python 3.11 与本技能声明的单一已验证运行时配置
---

# Ray ImageNet 环境只读检查

## 范围与输入

检查即将运行本场景合成 JPEG 预处理 smoke 所需的现有环境。默认只检查本地运行时，不启动 Ray；显式给出 `--address` 时，连接指定现有 Ray 服务，读取资源并执行一个仅返回版本和整数校验和的微型任务。除新建指定 JSON 报告及其父目录外，不写数据库/主题、不改 OS、不安装包、不启动或停止服务。

版本门槛只覆盖实际执行过的接口配置，不是广泛的“兼容版本”声明：

| 项目 | 已验证配置 |
| --- | --- |
| Python | 3.11.x |
| Ray | 2.56.0 |
| torch | 2.13.0（仅 CPU wheel；接受 `+cpu` 本地后缀） |
| torchvision | 0.28.0（接受 `+cpu` 本地后缀） |
| NumPy / PyArrow / Pillow | 2.1.3 / 23.0.1 / 13.0.0 |

其他版本返回 `wrong_version`，含义是“未在本技能已验证配置内”，不是断言上游版本必然不兼容。报告记录实际完整版本。

## 执行

在本 skill 目录运行，输出路径必须尚不存在：

```bash
python3 scripts/check_environment.py --output results/environment-001.json
```

默认要求 2 个有效 CPU、256 MiB `/dev/shm`、2048 MiB 可用内存和 512 MiB 临时磁盘余量。可按实际计划调整有界参数，例如：

```bash
python3 scripts/check_environment.py \
  --output results/environment-002.json --cpus 2 \
  --object-store-mib 256 --min-memory-mib 2048 --timeout 30
```

检查非默认现有端点时，使用不含凭据的地址：

```bash
python3 scripts/check_environment.py \
  --output results/cluster-001.json \
  --address ray://ray-head.example.com:10001
```

TLS/认证使用目标 Ray 版本支持的安全文件或相应 `RAY_*` 环境配置。密码和令牌不得放入 argv、地址 userinfo、日志或报告；脚本只读取声明的认证变量用于脱敏，不打印变量值。`ray.shutdown()` 在现有服务模式仅断开本次 driver，不停止服务。

## 结果与退出码

- `0 / ready`：所有 mandatory 探针通过；optional 诊断例外列在 `warnings`。
- `1 / blocked`：至少一个 mandatory 探针失败，原因区分 `absent`、`wrong_version`、`timeout`、`auth`、`unreachable`、`resource`、`error`。
- `3 / unknown`：没有明确失败，但 mandatory 证据未知。
- `2`：参数错误、输出已存在或无法创建输出；已有字节保持不变。

报告含每项 `name/mandatory/status/reason/evidence`、实际版本、配置与限制。本地 `ready` 只表示声明的运行前置成立，不代表 Ray 已启动或端到端任务已通过；现有端点通过微型任务也不证明生产健康、容量、业务权限或完整 ImageNet 准确性。探针口径见 [probes](references/probes.md)，公开依据见 [sources](references/sources.md)。

## 可执行回归

```bash
python3 scripts/test_contract.py --result results/contract-001.json
```

tier-A 回归覆盖成功、错误/缺失版本、工具缺失、认证、超时、unknown、输出碰撞、脱敏及评测契约；mock 只证明契约，不是真实 Ray E2E。真实前置验证应在用户指定的隔离运行时再次执行上述环境检查。
