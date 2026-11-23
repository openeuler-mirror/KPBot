# 安装与验收记录

复制到新实验结果目录，逐项填写；未验证项明确写未验证。

| 字段 | 实际值或相对证据位置 |
|---|---|
| 编号、时间、目的 | |
| 主机系统、架构、Docker 版本 | |
| 基础/最终镜像 digest、构建命令、构建日志 | |
| Python/Ray/torch/torchvision/numpy/pyarrow/Pillow 自检输出 | |
| installed-packages.txt 归档位置 | |
| CPU quota/cpuset、内存、共享内存、Ray 逻辑 CPU | |
| 数据类型：合成 / 用户 JPEG 子集 | |
| 输入目录身份、选择上限、输入 manifest 校验和 | |
| 输入字节数、估算峰值内存、--max-estimated-mib | |
| JPEG 数量、batch size、预处理定义、模拟推理 | |
| 实际命令、退出码、stdout/stderr | |
| 输出行数、逐行 tensor 指纹/模拟标签核对 | |
| Dataset stats、result.json、失败项 | |
| 完整命令耗时与 pipeline 耗时范围（分别记录） | |
| 本次容器/进程及清理检查（含无 Ray 残留进程） | |

验收只判断安装与正确性，pipeline 计时包含 Ray Data 执行但不包含镜像构建和串行参考计算；不能直接作为真实推理性能结论。
