# 脚本说明

## alloc_size_audit.py

把"申请了多少字节"拆成两层来算：库自己的分配下限，和分配器的尺寸类。两层分开报，才能判断浪费是源码可修的，还是分配器取整造成的。

```bash
python3 alloc_size_audit.py --sizes sizes.txt --floor 1024 --op-count 200000
python3 alloc_size_audit.py --sizes sizes.json --allocator jemalloc --json
python3 alloc_size_audit.py --sizes sizes.txt --floor 1024 \
    --actual-bytes-per-op 1280.04 --actual-peak-live 5249160
```

输入：每行一个请求尺寸、JSON 列表、或 `尺寸 -> 计数` 映射。

**尺寸口径**：输入必须是**该分配点实际向分配器请求的整块字节数**（对象头 + 元数据 + 内联数据），不是其中某个字段的大小。libevent 现场调用参数是 16 B（`sizeof(struct evbuffer_chain_reference)`），helper 再加 48 B 头，所以基线观察值是 1024、修复后是 64；若只喂 16 会得到 64x 的虚高通胀，喂 1024 则得到 1.0 的假阴性。不确定口径时，先用 `alloc_probe.c` 取实测 `bytes_per_op` 反推每块请求尺寸。

输出字段：

| 字段 | 含义 |
|---|---|
| `requested_bytes` / `effective_bytes` | 名义总量与经下限抬高后的总量 |
| `inflation_ratio` | `effective / requested`，即浪费倍数 |
| `floor_hit_share` | 命中分配下限的请求占比 |
| `histogram[]` | 每个尺寸的名义值、下限后值、分配器落点、通胀倍数 |
| `requested_bytes_per_op` / `effective_bytes_per_op` | 除以 `--op-count` 后的每操作字节 |
| `model_error` | 模型预测与 `--actual-bytes-per-op` 的相对偏差 |
| `recommendation` | `exact_size_allocation` / `reject` / `no_floor_inflation` |

`--allocator` 目前支持 `glibc`（16 字节对齐 + 页级大块）与 `jemalloc`（内置 small/large size class 表）。模型是近似值，所以必须用 `--actual-bytes-per-op` 交叉验证；`model_error` 显著时先解释偏差，不要直接采信模型。

## alloc_probe.c

链接期分配计数器。用 `--wrap` 接管 `malloc/calloc/realloc/free`，统计申请字节、次数、释放次数与峰值活字节，进程退出时打印一行：

```
alloc_report bytes=... calls=... frees=... peak_live=...
```

```bash
cc -O2 bench.c <skill>/scripts/alloc_probe.c \
   -Wl,--wrap=malloc -Wl,--wrap=calloc -Wl,--wrap=realloc -Wl,--wrap=free \
   -o bench_alloc
./bench_alloc <args>
BENCH_ALLOC_REPORT=0 ./bench_alloc <args>   # silence
```

用法约束：

- 它是观测器，不改变分配语义，也不缓存对象；每次调用都原样转发给真实分配器。
- `bytes` 是请求字节之和，即在相同输入下完全可复现的量，适合做门禁；`peak_live` 依赖指针→尺寸表，表满时退化为少计而不是崩溃。
- 只统计经过被链接对象的调用；通过 `dlsym` 或其它分配器 API 的直接调用不在统计内。
- **必须与被测对象静态链接**：`--wrap` 只改写链接期可见的调用。若被测库按共享库方式链接（`-levent_core`），probe 只能看到 bench 自身的分配，会给出"几乎没有分配"的假结论；这种情况需改用 `LD_PRELOAD` 方案，或先把被测对象编成静态库。
- **单线程假设**：计数器与指针表是普通全局变量，多线程并发分配会少计。当前用例都是单线程；跨线程场景请把并发度降到 1 再取证，或改用 `LD_PRELOAD` + 线程安全计数器。
- 与 `LD_PRELOAD` 方案相比，链接期包装不需要运行环境配合，也不会影响同机上其它进程。

## 局限

- 尺寸类模型是近似的；跨分配器比较时看趋势不看绝对值。
- 不区分 arena、不统计 `mmap` 阈值行为；这些需要 `strace -c` 或分配器自身的统计。
- 时间收益不在本脚本职责内：申请字节下降不必然等于吞吐提升，后者由 A/B 实测负责。
