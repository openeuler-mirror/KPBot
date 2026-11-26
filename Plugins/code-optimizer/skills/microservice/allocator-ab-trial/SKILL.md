---
name: allocator-ab-trial
description: >
  工作场景：glibc / jemalloc / tcmalloc 三方分配器交替对照——换分配器
  的性能效果验证。适用于：分配密集型负载的分配器选型、jemalloc/tcmalloc
  迁移评估、分配器相关优化的对照基线。覆盖：LD_PRELOAD 隔离（env -u
  清除继承）、交替执行（glibc→jemalloc→tcmalloc→glibc...）、逐轮比值
  与方向一致性、探测到的分配器自动纳入。不适用：非分配器单变量优化
  （用 alternating-ab-protocol）；分配器内部行为分析（用 perf/jemalloc
  profiling 工具）。
---

# allocator-ab-trial — 三方分配器交替对照协议

glibc/jemalloc/tcmalloc 的性能效果**高度依赖负载形态**——jemalloc 在
小对象高频构造负载实测**耗时 +69.5%**（性能约 -41%），与"服务器标配"的直觉相反。
结论不可跨形态搬运。

## 可执行入口

```bash
# 3 轮交替（默认），glibc 基线 + 探测到的替代分配器
bash scripts/allocator_ab.sh -c "taskset -c 288-295 ./bench --repetitions=1"

# 5 轮收官协议
bash scripts/allocator_ab.sh -c "<benchmark>" -r 5 -w 2
```

## 输出契约

- **stdout**：每轮每分配器的耗时 + 各分配器逐轮原始值 + 相对 glibc 的逐轮
  比值 + 均值与变化百分比 + 方向结论（N/N 一致快/慢/无差异/混向噪声区）
- **stderr**：异常详情（命令退出/无可解析时间值/链接期分配器锁定）
- **退出码**：`0` 正常完成；`1` 环境不可对照（未探测到替代 so）或测量失败
  （含被测程序链接期已锁定 jemalloc/tcmalloc 致 glibc 基线不可得）；
  `2` 参数错误（缺 -c、-r 非正整数、-w 非非负整数）

## 和同类 skill 的分界

- `alternating-ab-protocol`：双臂通用 A/B 协议——本 skill 是**分配器专用
  多臂**扩展（LD_PRELOAD 隔离 + 三方探测 + glibc 基线语义）
- `folly-future-optimization`：消费本 skill 做 folly 分配器归因

## 约束

- glibc 基线语义：不设 LD_PRELOAD 且 `env -u LD_PRELOAD` 清除继承
  （父进程已预加载时空串 LD_PRELOAD 不能还原 glibc）；计时前用 ldd 核实被测
  程序未在链接期锁定 jemalloc/tcmalloc——链接期已选定时 glibc 基线不可得，
  脚本直接报错停止（不拿同一分配器与自身比较）
- jemalloc/tcmalloc(_minimal)：ldconfig -p 探测到才纳入——缺失不报错
- fail-closed：命令异常退出/无可解析正时间值 → 立即中止

## 错误处理

| 现象 | 原因与处理 |
|------|-----------|
| exit 1 + "链接期已使用" | 被测程序 ldd 可见 jemalloc/tcmalloc——改用未链接替代分配器的构建，或只比较已链接分配器的 LD_PRELOAD 变体 |
| exit 1 + "未探测到" | 系统无 jemalloc/tcmalloc——安装 libjemalloc/libtcmalloc 后重测 |
| exit 1 + "无可解析时间值" | benchmark 输出无 ns/us/ms/s 或为负值/零——检查输出格式 |
| "噪声区" 结论 | 各轮相对 glibc 比值混向——增轮（-r 5）或排查环境噪声源 |

## 参考资料

- [scripts/README.md](scripts/README.md) — 脚本用法与退出码契约
