---
name: kptools-ray-data-environment-check
description: 只读检查本机 Ray Data 环境。用户提到 Ray Data 环境检查、运行前置、Ray/NumPy/PyArrow 版本、CPU/内存或 /dev/shm 是否满足时立即使用；不启动 Ray，不判断生产健康。
license: MulanPSL-2.0
metadata:
  compatibility: Linux；Python 3.11；现有 Ray 2.56.0、NumPy 2.1.3、PyArrow 23.0.1；仅需 Python 标准库驱动
  category: environment_check
  side_effect_free: 'true'
---

# Ray Data 环境检查

## 使用

```bash
python3 scripts/check_environment.py --output results/environment.json
```

退出码：`0=ready`、`1=blocked`、`2=输出/参数错误`、`3=unknown`。输出路径必须不存在，父目录会自动创建；脚本不安装或下载依赖。

## 边界

逐项输出 `ready/blocked/unknown`、`mandatory` 和原始 `evidence`。检查 Linux、Python、精确包版本、可见 CPU、有限可用内存、空闲共享内存、可写新输出、NumPy/PyArrow 原生运算及所需 Ray API 签名。不会调用 `ray.init`、`ray start/stop` 或连接已有集群；结果只描述当前本地前置条件，绝不推断生产健康、容量或性能。

详见 [探针与指标参考](references/probes.md)。
