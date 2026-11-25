---
name: temporal-hydration-offload
description: >
  L3 水合下沉：把"存储类型 → 领域对象"的字段级转换（时间戳、向量、JSON、数组）挪到原生扩展，
  L2 之后客户端仍钉满单核时的下一步。可单独用于任何解释型客户端的类型水合热点。
  场景入口类触发：时序水合、hydration、时间戳解析慢、类型转换、datetime 热点、水合下沉。
  杠杆实施类触发：原生扩展、pyo3、Rust 水合、sitecustomize 激活。
---

# L3 水合下沉

## 前置条件

- L2 之后客户端仍钉满单核；
- 火焰图里"时序/向量/JSON 转换"占 20%+。本例热点是把驱动的时序类型转成 Python 对象——
  纯 Python 实现每步都要建对象、走属性协议。

## 做法

1. 写一个原生扩展，在原生侧解析时序类型并直接构造目标对象，Python 侧只留薄入口；
2. 用 monkey-patch 替换驱动的内部水合函数（本例 `neo4j._codec.hydration.v2.temporal.hydrate_datetime`）；
3. 用 `sitecustomize.py` + 环境变量激活，便于与基线切换：

```python
import os
if os.environ.get('EXT', '0') == '1':
    import neo4j_time_ext as ext
    ext.install()
```

4. 安装用预编译 wheel，运行时不需 Rust/maturin：

```bash
pip install client/wheels/neo4j_time_ext-0.1.0-cp39-abi3-manylinux_2_34_<arch>.whl
```

## 实测（标定案例，L2 之上）

| 架构 | 检索 | 写入 |
|---|---|---|
| x86_64 | 9.80 → 6.46 s（−34%） | 6.37 → 4.80 s（−25%） |
| 鲲鹏 | 11.31 → 7.32 s（−35%） | 6.87 → 4.91 s（−29%） |

## 风险

- 类型语义逐字段对拍（时区、精度、None）；
- 扩展与驱动内部路径强绑定，升级驱动可能 `ImportError`；必须保留关闭开关回到纯 Python；
- 编译型客户端不需要"下沉"，只需检查是否把缓冲区拷进了中间结构。

## 什么时候才需要 Rust 工具链

`glibc < 2.34`、非 x86_64/aarch64、或要改扩展源码时，才需要 `rustc ≥ 1.75` + `maturin` 本地编译
（`maturin build --release`）；wheel 部署侧不需要。
