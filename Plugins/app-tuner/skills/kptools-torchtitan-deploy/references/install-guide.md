# 安装详解 — 昇腾软件栈与 torchtitan-npu 部署

> 事实来源：docs/user-guides/installation.md（master），排障视角重排。

## 1. 昇腾软件栈（六件套，按序装）

| 依赖 | 说明 |
|------|------|
| 昇腾 NPU 驱动 | 驱动固件安装指南（hiascend 文档） |
| 昇腾 NPU 固件 | 同上 |
| Toolkit（开发套件） | CANN 软件安装指南 |
| Kernel（算子包） | 同上 |
| NNAL（Ascend Transformer Boost 加速库） | **装前必须先 `source /usr/local/Ascend/cann/set_env.sh`** |
| PyTorch + torch_npu | Ascend Extension for PyTorch 配置与安装文档 |

> ⚠️ NNAL 顺序坑：不 source set_env 就装 NNAL 是高频失败点（环境变量未
> 就位，安装脚本找不到 CANN 组件）。见 pitfalls B-13。

版本配套见 SKILL.md 首表。**torch/torch_npu/CANN/python 四者必须同表配套**，
单独升级任何一个都可能破坏补丁体系的假设（仓内 patches 是针对特定版本文件
打的，如 FSDP 逐参数精度补丁明确针对 torch 2.14.0.dev）。

## 2. 安装 torchtitan-npu（三步）

```bash
git clone https://gitcode.com/cann/torchtitan-npu.git
cd torchtitan-npu
python3 -m pip install -r requirements.txt
python3 -m pip install -e .
```

- PyPI 安装暂未提供（待 torchtitan 稳定版），只支持源码 editable 安装
- 卸载：`pip uninstall torchtitan_npu`

## 3. torchao-npu（可选，低精度训练必需）

仓内已内置适配代码（`experiments/torchao-npu/torchao_npu`），两种装法：

```bash
# 装法一：editable 安装（自动带 torchao==0.17.0 依赖）
python3 -m pip install -e ./experiments/torchao-npu

# 装法二：PYTHONPATH 方式（不动包管理）
python3 -m pip install torchao==0.17.0
export PYTHONPATH="/path/to/parent${PYTHONPATH:+:${PYTHONPATH}}"
# parent 必须直接包含 torchao_npu/__init__.py；
# 用仓内源码时 parent = <torchtitan-npu>/experiments/torchao-npu
```

注意：通用训练脚本只透传量化 CLI，不会自动改可选依赖路径。

## 4. 部署后验证清单

```bash
# ① 设备可见
npu-smi info
# ② CANN 环境（set_env 探测链见 launch-reference）
source /usr/local/Ascend/ascend-toolkit/set_env.sh
# ③ torch_npu 导入冒烟
python3 -c "import torch, torch_npu; print(torch.npu.is_available())"
# ④ 单卡 debugmodel 冒烟（无需真实模型/数据）
NGPU=1 bash scripts/run_train.sh \
  --training.local-batch-size 1 --training.seq-len 2048 --training.steps 5
```

④ 的默认资产与覆盖方式：

| 资产 | 默认 | 覆盖方式（CLI 参数） |
|------|------|---------------------|
| tokenizer | `tests/assets/deepseek_v3` | `--hf-assets-path <目录>` |
| 数据集 | `tests/assets/c4_test` | `--dataloader.dataset <名>` + `--dataloader.dataset-path <目录>` |

> ⚠️ `run_train.sh` **不读取** `HF_ASSETS_PATH`/`DATASET`/`DATASET_PATH`
> 环境变量（上游 quickstart 明文声明）——export 这些变量会**静默失效**。
> 环境变量形式仅被 `examples/deepseek_v4/debug/*.sh` 示例脚本消费。

看到逐步 step/loss/tps/tflops/mfu 输出即部署成功。
