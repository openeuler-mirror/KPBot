# NPU 终端检查与任务恢复

## 验证节点和设备

```bash
hostname
pwd
npu-smi info
```

按用户任务记录主机名、工作目录、卡号、Chip、Phy-ID、每个 die 的显存与占用进程。做 PyTorch 任务时再运行以下检查。

```bash
python3 -V
python3 -c 'import torch, torch_npu; print(torch.__version__, torch_npu.__version__, torch.npu.device_count())'
```

为 1P1D 选设备时，先检查现有可见性设置和调度器分配，再对每个候选逻辑编号分别启动短进程验证。不要把 npu-smi 的物理 ID 直接填入启动命令。分设环境下，即使 CPU 与 NPU 两边都显示 /mnt/workspace，也要在目标节点验证文件存在。

## 断线恢复与服务验收

断线后重新选中原实例，检查主机名、设备、工作区文件、后台进程、端口与最近日志。按现场状态续跑中断步骤。已有权重或镜像层时先验证完整性；避免重复下载和启动。

对推理服务，通过用户实际使用的入口发出多 token 请求。对 1P1D，核对 P 和 D 的设备绑定、同一请求的处理记录以及 KV 交接日志或指标。报告实际可用的入口、模型、启动和日志位置。仅完成安装、设备枚举或健康探测时，明确写出已验证的阶段。
