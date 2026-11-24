# scripts/parse_flamegraph_svg.py

火焰图 SVG 解析与库帧聚合工具（纯 Python 标准库，>= 3.8，只读输入，不执行任何代码）。

## 用法

```bash
# 基础解析 + 完整性自检（Σself 必须 == 100%）
python3 scripts/parse_flamegraph_svg.py graph.svg

# 聚合某个库的帧（pattern 为子串匹配，发生在第一个 '(' 之前的部分）
python3 scripts/parse_flamegraph_svg.py graph.svg --lib "google::protobuf"

# 去嵌套口径（同名递归/模板帧按栈去重计数）——报"库占比/路径天花板"时必须用
python3 scripts/parse_flamegraph_svg.py graph.svg --lib "folly" --denest --top 20

# JSON 输出（拒绝覆盖已存在文件）
python3 scripts/parse_flamegraph_svg.py graph.svg --lib "absl" --denest --json out.json
```

## 输出口径

| 输出 | 含义 |
|------|------|
| `integrity: sum(self)==total` | 树构建回归的必要条件（对任意父指针分配恒成立，**单独不能**拦挂错祖先——见下一条）；失败时退出码 2，**任何数字不可引用** |
| `structure: ... OK` / `structure: ADVISORY ...` | 结构门禁：负 self（非合并图）+ 同父兄弟帧重叠（所有图）——**挂错祖先在此拦截**；失败退出码 2。合并图判定只认根区（深度 ≤1）的严格 `(N threads merged)` 标记；降级时输出 ADVISORY 措辞（不再打印 OK），深层帧名或松散子串不触发降级 |
| `orientation` | `root-top`（root 帧 y 最小，冰柱图——flamegraph.pl `--inverted` 的几何）/ `root-bottom`（root 帧 y 最大，火焰图——flamegraph.pl **默认输出**的几何）。按最大样本帧的 y 实测判定，与工具名无关；判反会在建树时报 root 不唯一 |
| `library naive=X%` | 库帧实例样本直接求和——**会虚高**（递归深度/调用次数都计入；可超 100%） |
| `library de-nested=Y%` | 含库帧的调用栈样本占比（栈级去重，每栈只计一次）——报库占比/天花板用这个 |
| `category ...` | 库帧 self 按语义类别分解（构造/析构/拷贝/序列化/查找/分配）；merged 图上可能含负 self（artifact，有 note 标注） |
| `top functions` | 帧名份额排行（naive 或 de-nested 口径） |

## 处理的真实形态（均有实测输入验证）

- 样本数带千分位逗号（perf 计数单位，如 `1,570,780,153,186`）
- `<g>` 嵌套包裹（title+rect 直接配对解析，容忍任意包装层）
- 合并线程图：`(N threads merged)` 帧下孙帧跨层挂接（父搜索扩展到全部更浅层）；
  子样本和超父样本的帧被检测并以 stderr 告警，de-nest 份额按有效总量归一化（保持 ≤100%）
- 倒置冰柱与标准布局自动判定
- 非树几何（任何祖先都覆盖不到的帧）→ ParseError 退出 2

## 边界

- 只解析 flamegraph.pl 风格 `<title>name (N samples, P%)</title><rect .../>`；
  其他格式（d3-flamegraph JSON 等）不支持
- `--lib` 是子串匹配：模式太宽（如 `std`）会把业务代码算进库——先用 top 输出核对
  匹配到的帧再引用占比
- 类别分解按函数名关键词，粗粒度参考，不替代人工归类
