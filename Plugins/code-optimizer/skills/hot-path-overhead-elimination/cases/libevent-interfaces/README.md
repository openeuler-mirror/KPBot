# libevent-interfaces 用例说明（hot-path-overhead-elimination）

这个用例在真实的 libevent 2.1.12 源码树上验证本 skill 的配方，是"方法论可落地"的端到端证据。它只应用本 skill 的补丁，不依赖任何其它 skill。

## 为什么需要外部源码

第三方源码不随用例分发。用例需要一份**未改动的** libevent 2.1.12 源码树，通过 `LIBEVENT_SRC` 指定：

```bash
git clone --depth 1 --branch release-2.1.12-stable https://github.com/libevent/libevent.git /tmp/libevent-2.1.12
LIBEVENT_SRC=/tmp/libevent-2.1.12 bash cases/libevent-interfaces/run.sh   # 在技能根目录执行
```

未设置或源码树不干净（已经含配方产物）时，用例输出 `skipped=true` 并退出 0。这是显式的"本次未验证"，日志会说明原因。

## 施加了什么

`recipes/0001-hot-path-overhead-elimination.patch`：

- `evbuffer_invoke_callbacks_` 的空观察者判定内联进头文件，重体保留为 `evbuffer_invoke_callbacks_slow_`（`dominant_outcome_inline`）；
- 空观察者时跳过 `n_add_for_cb` / `n_del_for_cb` 记账（`absent_consumer_elision`，并维持"无观察者 ⇒ 计数为零"不变量）；
- 一并携带叶子入口、常量宽度拷贝与频率优先分派（`leaf_entry_split` / `constant_width_copy` / `hot_value_first_dispatch`），这几族只在 AArch64（鲲鹏）目标编译。

补丁不包含任何分配路径改动，因此用例验证的是"纯调用路径"的效果。

## 断言

| 检查 | 门槛 |
|---|---|
| evbuffer 回归组 | 必须通过 |
| `append` | ≥1.10x（该接口的成本集中在回调派发与记账上） |
| `readline` | ≥0.95x（回归守卫） |
| `drain` | ≥0.90x（回归守卫） |
| 解耦守卫（静态） | 补丁文本中不得出现 `evbuffer_chain_new_reference`（分配路径配方的符号） |

用例固定做 15 对交替采样并取逐对比值的中位数。`append` 是本 skill 的主战场；`readline`/`drain` 的收益属于分配路径（建链与解链），因此这里只做回归守卫。

解耦守卫是静态文本检查：本 skill 只能通过调用与记账路径取收益，补丁一旦混入分配路径改动，用例直接失败；它不采集运行期分配字节数。

参考值以目标机（Kunpeng）上的运行结果为准；配方在 Kunpeng 上的效果记录见 `references/case-study-buffer-append-notify.md`。

## 本地实测记录

- 源码树：上游 `release-2.1.12-stable`（GitHub，commit `5df3037d1055`），`LIBEVENT_SRC` 指向该树。
- 执行：2026-11-09 于本机运行 `bash cases/libevent-interfaces/run.sh`，退出码 0。
- 结果：补丁干净应用；evbuffer 回归组通过；append 1.174x、readline 1.031x、drain 0.995x（15 对逐对比值中位数）；静态解耦守卫通过。
- 说明：这是一次带源码树的实跑记录，用于说明"提供源码树后可跑通"；不同机器与负载下数值会波动，回归组与守卫结论不受影响。

## 局限

- 补丁针对 `release-2.1.12-stable` 的文件内容；其它版本需要重新生成补丁，`run.sh` 会在源码已含本用例产物符号时跳过并说明。
- 时间数据受同机其它负载影响；用例用交替采样与逐对比值中位数降低噪声。
- 叶子入口、常量宽度拷贝与频率优先分派只在 AArch64 目标编译启用；在非 AArch64 构建上这些配方不参与，`append` 的收益主要来自前两条。
