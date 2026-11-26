---
name: flamegraph-parsing
description: >
  工作场景：解析 flamegraph.pl 生成的 SVG 火焰图——帧树构建、结构门禁、
  去嵌户口径、库帧定位与占比。适用于：验证火焰图数据可信度（结构门禁
  拦截挂错祖先）、计算库占比/天花板（去嵌户口径）、优化前后的帧级对照。
  覆盖：SVG 帧树解析、Σself==total 自检、负 self + 同父兄弟重叠双门禁、
  递归/模板帧去嵌套、倒置冰柱/标准布局自动判定、千分位样本数。不适用：
  火焰图驱动的优化工作流（用 flamegraph-libopt）；Go pprof 火焰图（用
  perf-optimization-report 的 pprof 管线）。
---

# flamegraph-parsing — 火焰图 SVG 解析与结构门禁

flamegraph.pl 输出的 SVG 需要程序化解析才能做库占比/天花板计算。
直接对同名帧求和会把递归深度计入（实测 3.6 倍虚高）——本 skill 的
解析器做**去嵌户口径**聚合。

## 可执行入口

```bash
# 解析 + 完整性自检（Σself 必须 == total）
python3 scripts/parse_flamegraph_svg.py graph.svg

# 聚合某个库的帧
python3 scripts/parse_flamegraph_svg.py graph.svg --lib "google::protobuf"

# 去嵌户口径（报库占比/天花板时必须用）
python3 scripts/parse_flamegraph_svg.py graph.svg --lib "folly" --denest --top 20

# JSON 输出（拒绝覆盖已存在的文件）
python3 scripts/parse_flamegraph_svg.py graph.svg --lib "absl" --denest --json out.json
```

详细算法与坑（布局方向判定、几何覆盖建树、self 口径、合并图降级边界、
解析回归案例）见 [解析参考](references/parsing-guide.md)；脚本用法与
退出码契约见 [scripts/README.md](scripts/README.md)。

## 输出契约

| 输出 | 含义 |
|------|------|
| `integrity: sum(self)==total` | 树构建回归必要条件；失败 exit 2，数字不可引用 |
| `structure: OK/ADVISORY` | 结构门禁：负 self（非合并图硬失败）+ 同父兄弟重叠（所有图硬失败） |
| `library naive=X%` | 库帧实例样本直接求和——**会虚高** |
| `library de-nested=Y%` | 含库帧的调用栈占比（栈级去重）——报占比用这个 |
| `top functions` | 帧名份额排行 |

## 和同类 skill 的分界

- `flamegraph-libopt`：火焰图驱动的**优化工作流**（怎么找优化点）——
  本 skill 是**解析工具层**（怎么从 SVG 提取可信数据）
- `perf-optimization-report`：Go pprof 火焰图管线——本 skill 解析
  flamegraph.pl SVG

## 约束

- 只读：不修改 SVG
- 只解析 flamegraph.pl 风格 `<title>name (N samples, P%)</title>`
- merged 线程图：`(N threads merged)` 帧下孙帧跨层挂接自动处理
