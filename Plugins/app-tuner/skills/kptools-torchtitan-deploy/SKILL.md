---
name: kptools-torchtitan-deploy
description: >
  torchtitan-npu 部署手册与踩坑指导：torchtitan 的昇腾（Ascend）后端扩展
  插件在 NPU 上的安装、配置与启动排障。覆盖版本配套表（master: PyTorch/
  torch_npu 2.14.0 + CANN 9.2.0 + Python 3.12 + Triton Ascend 3.2.1）、
  昇腾软件栈六件套（驱动/固件/Toolkit/Kernel/NNAL/PyTorch）安装、三步源码
  安装与 torchao-npu 可选件、新配置系统（--module/--config，2026-05 重构后
  弃用 --job.config_file TOML）、run_train.sh 单机/多机启动与环境变量逐项
  说明（TORCHINDUCTOR_NPU_BACKEND/HCCL 超时/多流/显存）、单卡 debugmodel
  冒烟验证、以及基于源码补丁证据（DT_INT64 编译中止/roofline CUDA 探测/
  异步 D2H/确定性双变量/FSDP 逐参数精度等）和实战环境（910B3 sync-start
  地板/NNAL 顺序/网卡选择）的踩坑实录。当用户需要部署/安装 torchtitan-npu、
  torchtitan 在昇腾上跑不起来、启动脚本报错、--module --config 配置怎么写、
  torchrun/HCCL 建连超时、编译报 DT_INT64 或 Triton 探测错误、版本配套
  怎么选、torchao-npu 低精度训练部署、多机 NODE_IPS 启动时触发。
license: MulanPSL-2.0
compatibility: 昇腾训练硬件（Atlas 800T A3 等）；目标环境需配套软件栈（见版本表）；cannlab 分设环境上操作链路见 kptools-cannlab-env
metadata:
  skill_type: debugging
  category: deployment
  domain: torchtitan_npu
  version: "1.0.0"
---

# kptools-torchtitan-deploy — torchtitan-npu 部署手册与踩坑指导

torchtitan-npu 是 torchtitan 的昇腾后端扩展插件（配置级 override + PyTorch
backend 注册）。部署它的第一道坑不是代码，是**版本配套**；第二道坑是
**配置系统在 2026-05 重构过**（网上/旧文档的 `--job.config_file` TOML 用法
已全部失效）。本 skill 按部署主线组织：配套 → 安装 → 冒烟 → 启动 → 排障。

## 版本配套表（部署前必查）

| torchtitan-npu | torchtitan | PyTorch | torch_npu | CANN | Python | Triton Ascend |
|----------------|-----------|---------|-----------|------|--------|---------------|
| master | main | 2.14.0(daily) | 2.14.0(daily) | **9.2.0** | **3.12.x** | 3.2.1 |
| v0.2.2-dev | v0.2.2 | 2.10.0 | 2.10.0 | **9.0.0** | **3.11.x** | 3.2.1 |

- 混搭（如 CANN 9.0 环境 + master 分支）是未定义行为，报错形态千奇百怪
  ——排障第一步永远是核对这张表
- cannlab NPU Pod（CANN 9.2.0 + 构建 python 3.12.9）与 master 分支天然配套

## 部署工作流

```
1 环境检查：npu-smi info / CANN 版本 / python 版本 / set_env 可 source
2 安装：pip install -r requirements.txt && pip install -e .（详见 install-guide）
3 可选件：torchao-npu（低精度训练必需，两种装法）
4 冒烟：单卡 debugmodel 跑通（见下）
5 正式启动：run_train.sh 单机 / run_train_multinodes.sh 多机（详见 launch-reference）
```

### 冒烟验证（单卡）

```bash
NGPU=1 bash scripts/run_train.sh \
  --training.local-batch-size 1 --training.seq-len 2048 \
  --training.steps 5
```

- tokenizer/数据集有内置默认（`tests/assets/deepseek_v3` 与 `c4_test`），
  冒烟无需下载真实模型；覆盖用 `HF_ASSETS_PATH` / `DATASET` / `DATASET_PATH`
- 看到逐步 loss 输出（step/loss/tps/tflops/mfu/elapsed_time_per_step）
  即部署成功；指标解读归 train-metrics

## 启动速查（新配置系统）

```bash
# 单机 8 卡（模型/配置由 --module/--config 指定，CLI 参数直接覆盖）
NGPU=8 bash scripts/run_train.sh \
  --module torchtitan.models.deepseek_v3 --config deepseek_v3_debugmodel \
  --training.local-batch-size 1

# 8 卡 EP8/DP8 起点示例（examples 下有成品脚本）
bash examples/deepseek_v4/debug/deepseek_v4_flash_8p_cpt_4k_a3.sh

# 多机：NODE_IPS 逗号列表，顺序即 rank
NODE_IPS=ip1,ip2 bash scripts/run_train_multinodes.sh ...

# 单进程调试（不开 torchrun）
COMM_MODE=gloo bash scripts/run_train.sh ...
```

> ⚠️ **配置系统重构（2026-05）**：master 用 `--module` + `--config`
> （config_registry 机制），**不再支持 `--job.config_file` 加载 TOML**。
> 旧文档/旧脚本全部要改。NPU 扩展字段挂 `training.extension.*` 与
> `extension.<group>.*`。

## 常见错误路由

| 现象 | 第一嫌疑 | 详见 |
|------|---------|------|
| 启动即报 import/算子缺失 | 版本配套错位（先核对版本表） | pitfalls B-12 |
| `DT_INT64 not supported` 全图编译中止 | torch_npu gather 白名单缺陷（仓内已带补丁，多半是版本错位绕过了补丁） | pitfalls A-1 |
| Triton CUDA driver 探测崩溃 | Inductor roofline 估算在 NPU 走了 CUDA 路径 | pitfalls A-2 |
| HCCL 建连超时/卡在建图 | 大 EP 建连慢是预期（3600/7200s 是防御值，勿调小） | launch-reference |
| 多机起不来 | 网卡选择（docker0）/ NODE_IPS 顺序 | launch-reference |
| 编译路径异常/D2H 结果错 | 异步 DeviceCopy/FlexAttention 已由仓内 workaround 处理，先核对版本 | pitfalls A-3/A-4 |
| 首 dispatch 瞬间 fatal | 910B3 sync-start 20 块硬件地板（并行度/kernel 同步启动需求超限） | pitfalls B-16 |
| 确定性训练结果仍不确定 | 只开 torch 开关不够，需 HCCL_DETERMINISTIC + CLOSE_MATMUL_K_SHIFT 双变量 | pitfalls A-6 |

## 约束

- 本 skill 管部署/配置/启动/排障；**链路操作**（怎么把代码传到 NPU Pod）
  归 `kptools-cannlab-env`，**训练指标解读**归 `train-metrics`，
  **上板性能采集**归 `tool-perf-collect`
- 低精度训练（all_block_fp8 等 recipe）**仅 A5（Ascend 950）硬件**支持，
  A3 上开 `--extension.quantization.*` 会直接失败
- PyPI 安装暂未提供（主线待 torchtitan 稳定版），只支持源码安装

### 和同类 skill 的分界

- 远程环境链路与 NPU Pod 操作：`kptools-cannlab-env`
- 训练日志指标（tps/mfu/step time）解析：`train-metrics`
- 上板性能采集与加速比对比：`tool-perf-collect`
- 通用昇腾环境采集（非 torchtitan）：各 kptools-* collector

## 错误处理

| 现象 | 原因与处理 |
|------|-----------|
| `pip install -e .` 失败 | python 版本与分支不配套（版本表）；requirements 版本冲突先核对 torch/torch_npu |
| NNAL 装不上 | 安装 NNAL 前必须 `source /usr/local/Ascend/cann/set_env.sh`（install-guide §1） |
| 启动后无日志 | run_train.sh 默认 `--local-ranks-filter 0`（只打 rank0），多 rank 用 `LOG_RANK=N` |
| set_env.sh 找不到 | 脚本按 `ASCEND_SET_ENV_PATH` → 4 个候选路径探测，非标准路径用环境变量显式指定 |
| 多机 docker0 被选为通信网卡 | 脚本已自动跳过 docker0；自定义网络环境注意 HCCL_SOCKET_IFNAME |
| 冒烟 OOM | debugmodel 很小，OOM 说明环境异常（显存被占/驱动问题），`npu-smi info` 排查 |

## 参考资料

- [安装详解](references/install-guide.md) — 软件栈六件套/三步安装/torchao-npu 两种装法/验证清单
- [启动参考](references/launch-reference.md) — 单机/多机环境变量逐项说明/配置系统/并行与低精度门槛
- [踩坑实录](references/pitfalls.md) — 源码补丁证据组（A）+ 实战环境组（B），每条含症状/根因/证据位置
