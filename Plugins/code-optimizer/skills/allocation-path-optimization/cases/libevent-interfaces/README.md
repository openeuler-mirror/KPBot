# libevent-interfaces 用例说明（allocation-path-optimization）

这个用例在真实的 libevent 2.1.12 源码树上验证本 skill 的配方，是"方法论可落地"的端到端证据。它只应用本 skill 的补丁，不依赖任何其它 skill。

## 为什么需要外部源码

第三方源码不随用例分发。用例需要一份**未改动的** libevent 2.1.12 源码树，通过 `LIBEVENT_SRC` 指定：

```bash
git clone --depth 1 --branch release-2.1.12-stable https://github.com/libevent/libevent.git /tmp/libevent-2.1.12
LIBEVENT_SRC=/tmp/libevent-2.1.12 bash cases/libevent-interfaces/run.sh   # 在技能根目录执行
```

未设置或源码树不干净（已经含配方产物）时，用例输出 `skipped=true` 并退出 0。这是显式的"本次未验证"，日志会说明原因。

## 施加了什么

`recipes/0001-allocation-path-optimization.patch`：

- 引用型链的创建改用精确尺寸申请：`sizeof(struct evbuffer_chain) + sizeof(struct evbuffer_chain_reference)`，替代通用分配函数的 1024 B 下限（`exact_size_allocation`）；
- 通用分配函数与数据型链的分配保持不变——下限对拥有内联数据区的链仍然正确；
- 补丁不含任何调用路径改动。

## 断言

| 检查 | 门槛 |
|---|---|
| evbuffer 回归组 | 必须通过 |
| drain 申请字节 | ≥8x 下降（确定性口径，相同输入下完全可复现） |
| `drain` | ≥2.0x（建链/解链是该接口的全部成本） |
| `readline` | ≥0.95x（回归守卫） |
| `append` | ≥0.90x（回归守卫） |

用例固定做 15 对交替采样，speedup 取逐对比值的中位数，避免同机负载漂移把收益放大或抹平。

申请字节是本用例的主判据：它由数据结构与分配下限决定（链头 + 引用记录 对 1024 B 下限），不随机器波动。时间指标只作为方向性验证。

`readline` 也受益，因为该接口内部用引用链承载待解析的行、并在消费后解链；`append` 只分配数据链，因此保持不变。

参考值以目标机（Kunpeng）上的运行结果为准；配方在目标机上的效果记录见 `references/case-study-metadata-allocation.md`。

## 局限

- 补丁针对 `release-2.1.12-stable` 的文件内容；其它版本需要重新生成补丁，`run.sh` 会在源码已含配方产物时跳过并说明。
- 绝对收益依赖分配器：在 glibc 上最明显（`brk`/trim/页回收），在 jemalloc/tcmalloc 上会收窄；用例的确定性判据（申请字节）不受分配器影响。
- 时间数据来自共享机器，噪声大于专用服务器；用例用交替采样与中位数降低噪声。
