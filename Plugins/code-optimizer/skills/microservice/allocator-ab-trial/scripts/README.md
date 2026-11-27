# scripts/allocator_ab.sh

glibc/jemalloc/tcmalloc 三方分配器交替对照协议（纯 bash，只读）。

## 用法

```bash
bash scripts/allocator_ab.sh -c "<benchmark 命令>" [-r 轮数] [-w warmup]
```

## 退出码

| 码 | 语义 |
|----|------|
| 0 | 正常完成（含逐轮比值、均值与方向结论输出） |
| 1 | 环境不可对照（未探测到替代 so）、测量失败（命令异常/无可解析时间值/非正值）或链接期分配器锁定（glibc 基线不可得） |
| 2 | 参数错误（缺 -c、-r 非正整数、-w 非非负整数） |

## 输出

| 输出 | 含义 |
|------|------|
| 每轮耗时 | 各分配器每轮的 ns 值（glibc -> jemalloc -> tcmalloc 交替） |
| 逐轮比值 | 各替代分配器相对 glibc 的 opt[i]/glibc[i] 序列 |
| 均值与变化 | 各分配器均值 vs glibc 均值（±%） |
| 方向结论 | N/N 一致快/慢（可下结论）、无差异、混向（噪声区禁止下结论） |
