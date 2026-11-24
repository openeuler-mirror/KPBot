# 热点确定：两种入口

优化顺序决定了这次工作的价值。先把"改哪个函数"定下来，再动手写代码。

## 第一问：有没有热点函数文件

开工前必须问用户，不要自己假设。问法要具体，让用户能直接回答：

> 有没有已经采集好的热点函数文件（perf report、gprof、火焰图折叠栈、或者一份
> 函数名+占比的清单）？有的话给我路径；没有的话我扫描整个项目找可向量化的循环。

用户回答"有"就走 A，回答"没有"或"不确定"就走 B，并把 B 的扫描结果作为下次的
热点依据存档。

## A. 按热点文件排序

文件格式不用先问，直接读前 50 行判断：

| 形态 | 识别特征 | 提取方式 |
| --- | --- | --- |
| `perf report` 文本 | `Overhead  Command  Shared Object  Symbol` | 取 Overhead% 和 Symbol |
| `perf annotate` / `--stdio` | 带源码行和百分比 | 只取函数级汇总 |
| gprof flat profile | `% time   cumulative   self` | 取 `% time` 和 name |
| 火焰图折叠栈 | `a;b;c 1234` | 按 `;` 切分，统计叶子帧 |
| 自定义清单 | `func,12.3%` / TSV / CSV | 按列猜，猜不到就问 |

提取规则：

1. 百分比降序排序，丢掉占比 < 1% 的尾巴。
2. C++ 符号先 demangle（`c++filt`）；模板实例化保留完整签名，`<...>` 不要截断。
3. 去掉 `[kernel.kallsyms]`、`libc.so`、`libstdc++` 这类外部帧——它们不在本次范围。
   如果外部帧占了前面几名，说明热点在库调用里，要回头问用户是否要换优化目标。
4. 每个函数名都要在项目源码里找到定义。找不到的（被内联、被重命名、版本不一致）
   单独列出来放 `other/hotspots/unresolved.md`。
5. **解析失败率超过一半时停下来问用户**，不要拿一份半懂的热点表往下做——那会浪费
   整轮优化时间，而且结论不可归因。

解析结果写成 `other/hotspots/source.json`：

```json
{
  "origin": "user-file",
  "path": "/abs/path/perf.txt",
  "total_overhead_covered": 82.4,
  "functions": [
    {"rank": 1, "symbol": "x265::pixel_ssd_4x4", "demangled": "...",
     "share": 12.3, "file": "source/common/pixel.cpp",
     "line": 412, "resolved": true}
  ],
  "dropped": [{"symbol": "memcpy", "reason": "external"}],
  "notes": ""
}
```

之后按 `rank` 顺序逐个处理。

## B. 没有热点文件：扫描项目

没有任何采样数据时，靠结构特征排序。分两步：先机器筛，再人读。

### B1. 机器筛

```bash
python3 <skill>/scripts/scan_candidates.py --project <项目根> --top 40 --json \
  > <workspace>/other/hotspots/scan.json
```

它按下面的信号打分，输出候选函数和循环位置：

- 循环嵌套深度（越内层权重越高）
- 循环体里是否有可并行的逐元素数组访存
- 是否有跨迭代依赖（出现 `+=` 到循环外变量、`[i-1]`、指针自增解引用时降权）
- 函数被调用的位置数（调用点多的更可能是热路径）
- 是否在派发表里注册（`p.pu[LUMA_8x8].sad = sad<8,8>;` 这种形态是编码器热点的典型
  特征——函数体本身没有被直接调用，只看调用点会漏掉它们）
- 是否是固定小尺寸块模板（4x4/8x8/16x16 这类，循环开销占比高）
- 是否已经含有 intrinsic 或 `asm`（已有的说明有人优化过，降权）

扫描器只做**候选**，不做"可向量化"的证明。它的输出必须逐个读代码确认。

### B2. 人读

按分数从高到低读，每个函数回答三个问题：

1. 它是不是真的会被频繁调用？（在编码/解码/搜索的内层？还是初始化、错误处理？）
2. 它的循环总体量有多少？（迭代次数 × 每次工作量）
3. 有没有明显的向量化机会？（见 `arm-vectorization-rules.md` 第 2 节）

读完给出排序，并把**排序理由**写进 `other/hotspots/source.json` 的 `notes`。
理由很重要：用户下次会问"凭什么先改这个"。

排序时还要做一次"有没有用"的过滤：**已经有 NEON/SVE/汇编变体的 C 函数是回退路径**，
改它不会影响运行性能。查法是 grep 该函数基名在架构目录和派发表里的出现：

```bash
grep -rn "<函数基名>" <项目>/source/*aarch64*/ <项目>/source/*/asm-primitives* 2>/dev/null
```

这一步经常直接砍掉一半候选。被砍掉的目标也要在 `source.json` 里留记录和理由，
避免下一轮重复选它。

## 没有 perf 时怎么采

用户选了"以 Linux perf 为准，另提供兜底"。按可用性依次尝试：

```bash
# 1) 采样火焰图（Linux ARM 首选，需要 perf 与符号）
perf record -F 999 -g -- ./app <args>
perf report --stdio --sort symbol | head -40

# 2) 只统计总量，用来判断优化是否影响整体
perf stat -e cycles,instructions,cache-misses ./app

# 3) gprof：重编译加 -pg，跑完看 gmon.out
gcc -O3 -pg -g ... && ./app && gprof ./app gmon.out | head -40

# 4) 插桩计数：不改算法，只在函数入口/出口记时间
#    -finstrument-functions + __cyg_profile_func_enter/exit
#    或直接在候选函数外层包 clock_gettime(CLOCK_MONOTONIC)
```

在裸机 / QEMU 里没有 perf 时，可以直接用基准夹具反过来做热点判定：把候选函数逐个
放进 `assets/bench/` 计时，比较各自耗时占比。这测的不是真实调用频次，但能告诉你
"这个函数本身有多重"，配合调用点数量足以排序。

### 采集结果必须落盘

原始输出放 `other/hotspots/raw/`，不要只把结论写进对话——用户要能复核，下一轮
优化也要能复用同一份依据。命令、参数、被测二进制版本一起记下来：

```markdown
# other/hotspots/raw/perf-20260101.md
- 命令: perf record -F 999 -g -- ./x265 --input ... --preset medium
- 二进制: build/x265 (commit 9b59d45)
- 采样时长: 42s
- 覆盖率: 采样丢失 3.1%
```

## 顺序之外的例外

热点排序不是唯一依据。下面几种情况可以提前或推后：

- 热点第一名是不可向量化的算法（熵编码、状态机）→ 顺延到下一个。
- 热点靠前但改动会影响对外 API/ABI → 先做靠后的安全目标。
- 用户明确指定了函数 → 以用户为准，把热点排序写进报告的"为什么没按顺序"。
