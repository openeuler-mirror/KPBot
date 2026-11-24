# 安装与验收记录

复制到新实验结果目录，逐项填写；未验证项明确写未验证。

| 字段 | 实际值或相对证据位置 |
|---|---|
| 编号、时间、目的 | |
| 主机系统、架构、Docker 版本 | |
| 基础/最终镜像 digest、构建命令、构建日志 | |
| 依赖锁文件及 `references/lock-provenance.json` 来源记录 | |
| `pip --require-hashes`、CPU wheel 安装报告、installed-packages.txt 归档位置 | |
| Python/Ray/torch/torchvision/numpy/pyarrow/Pillow 自检输出 | |
| CPU quota/cpuset、内存、共享内存、Ray 逻辑 CPU | |
| 本次结果目录名（`results/runs/<UTC时间>-<pid>` 或显式 `--output-dir`） | |
| 数据类型：合成 / 用户 JPEG 子集，以及 `dataset` 字段值 | |
| 输入目录身份、选择上限、输入 manifest 校验和 | |
| 越界符号链接、改名 PNG、损坏文件检查结果 | |
| 原图尺寸、`--max-pixels`、输入字节数、估算峰值内存、`--max-estimated-mib` | |
| JPEG 数量、batch size、预处理定义、MockActor 逐行实际 tensor 摘要 | |
| 实际命令、退出码、stdout/stderr | |
| 输出行数、逐行 tensor 指纹/模拟标签核对 | |
| Dataset stats、result.json、失败项 | |
| 完整命令耗时与 pipeline 耗时范围（分别记录） | |
| 本次容器/进程及清理检查（含无 Ray 残留进程） | |

验收只判断安装与正确性，pipeline 计时包含 Ray Data 执行但不包含镜像构建和串行参考计算；不能直接作为真实推理性能结论。固定输出目录无法复用：`run_pipeline.py --output`（父 CLI 与子 worker 一致）或 `check.py --output-dir` 指向已存在目录时均以退出码 2 拒绝，旧 result.json 不会被当作本次结果读取，已有字节保持不变。
