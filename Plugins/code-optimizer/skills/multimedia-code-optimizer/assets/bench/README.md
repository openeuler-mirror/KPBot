# 基准夹具说明

这套夹具把一个（重构后：一对）函数放进 bare-metal AArch64 镜像里跑，用 QEMU 的
semihosting 输出结果，用来回答两个问题：**结果是否一致**、**是否更快**。

它存在的理由：目标平台往往是 aarch64 Linux，而开发机可能是 x86/macOS。用
`aarch64-elf-gcc` + `qemu-system-aarch64` 可以在没有目标板的情况下把这两个问题
问清楚，代价是不测真实内存层次和分支预测——所以它只适合函数级 A/B，不适合
替代整机验收。

## 文件

| 文件 | 作用 |
| --- | --- |
| `crt0.S` | 入口：设栈、开 FP/SVE、设向量长度、清 bss、调 main、semihosting 退出 |
| `link.ld` | 链接到 `0x40000000`（QEMU virt 的 RAM 基址） |
| `bench_shim.h/.c` | semihosting 输出、`CNTVCT_EL0` 计时、`memcpy/memset/memcmp/strlen` |
| `bench_runner.c` | 夹具主体：跑正确性用例、预热、A/B 计时、打印 `TIME` 行 |
| `Makefile` | 编码"夹具不带 -flto、被测函数用项目参数"的分工 |
| `kernel_ref.c` / `kernel_simd.c` | 一个最小可跑的示例（逐元素相加的标量版与 SVE 版），换项目时直接替换这两个文件 |

## 输出协议

`run_ab_bench.py` 依赖这些行，**不要改键名**：

```
CORRECT n=<len> ok=<0|1> mismatches=<count> first_bad=<idx|-1>
TIMEBASE freq=<hz> sink=<n>
TIME ref_ns=<n> simd_ns=<n>
DONE
```

`ok=0` 时夹具直接 `SYS_EXIT(1)`，不会进入计时。

## 五个坑

这五个都是实际踩到并验证过的，跳过任何一个都会得到错误结论。

### 1. 忘了 `ZCR_EL1.LEN`，向量长度停在 128 位

复位后 SVE 向量长度是 128 位，而 `-msve-vector-bits=256` 让编译器把 VL 当编译期
常量。两者不一致时**不会报错**：每次 `str z0` 只写 16 字节，结果是 40 个元素的
循环只写进 20 个，剩下的保持原值。调 `-O2` 时看起来像"结果随机错"。

`crt0.S` 里写 `ZCR_EL1.LEN = VL/128 - 1`；`SIMD_BENCH_VL_BYTES` 由 Makefile 传入，
必须和 `-msve-vector-bits` 一致。夹具在启动后用 `cntb` 读真实 VL，不匹配就报错退出。

顺带一提：`svcntb()` 在 `-msve-vector-bits` 下会被常量折叠，用它验证 VL 是循环论证，
必须用内联汇编读 `cntb`。

### 2. 忘了 `CPACR_EL1.FPEN/ZEN`，第一条 SVE 指令就掉进零向量表

EL1 上默认 trapping 是开着的，`hlt` 之外的 SVE 指令会触发 undefined instruction，
异常向量基址是 0，于是 PC 跳到 `0x200` 执行噪声。表现是"程序没有任何输出就挂住"。

### 3. MMU 关着就做不了非对齐访存：必须建恒等映射

AArch64 在**关闭 MMU** 时把一切数据访问都当作 Device-nGnRnE 处理，而 Device 内存
要求自然对齐——这跟 `SCTLR_EL1.A` 无关，清了 A 也没用。

编译器会把"16 个有效字节"的 `svst1_u8` 直接生成成 `str q0`（16 字节）。只要目标
指针只满足 8 字节对齐（比如行距 40 的图像块第 1 行），这条指令就吃 Data Abort：
`VBAR_EL1` 是 0，PC 跳到 `0x200` 执行噪声，表现是"跑到一半突然挂住"。

`crt0.S` 因此建了一个最小恒等映射：一条 4 KiB 的一级表 + 一条覆盖映像所在 1 GiB
区域的 block 描述符，`MAIR_EL1.Attr0 = Normal WB`，然后打开 `SCTLR_EL1.M`。这样
裸机内存的行为和真实 Linux 进程一致，非对齐行距才测得了。

排查方法：`qemu-system-aarch64 ... -d int` 会打印 `ESR` / `FAR` / `ELR`。
`ESR` 的 `DFSC` 为 `0x21` 是对齐异常，`FAR` 是故障地址，`ELR` 是出错指令，
对着 `objdump` 一看就知道是哪个访存。

### 4. `-flto` 把被测调用合并掉，计时结果失真

`bench_runner.c` 里对同一个 `kernel_ref(g_a, g_b, g_ref, LEN)` 调 64 次，参数每次都
一样。开 `-flto` 时编译器把它内联后直接算一遍，剩下 63 次是空循环。实测
`ref_ns` 从 229575 掉到 32775，快了 7 倍——测的不是函数，是编译器的死代码消除。

夹具用 `__attribute__((noipa))` 声明被测函数阻断这条路径，并在计时循环里插
`asm volatile("" ::: "memory")`。同理，夹具自身用 `-O2` 且不带 `-flto` 编译：
`sh_*` 被 LTO 特化后曾经出现过 semihosting 返回地址错乱、进程挂死。

被测量的是"这个函数经过项目参数编译后有多快"，不是"整个链接单元被优化成什么样"。

### 5. 不加 `-icount` 就没有可复现的计时

不加 `-icount` 时 `CNTVCT_EL0` 跟宿主墙钟走，同一份二进制两次能差 4 倍：
实测同一个 demo 的 `ref_ns` 在 643000 / 350000 / 229575 之间跳过（差别来自宿主负载和
TB 复用）。加 `-icount shift=0` 后虚拟时间按访客指令数推进，三次重跑完全一致。

结论：门控判定只用 `-icount` 的数字；墙钟数字只作参考，不要拿去下"更快"的结论。

## 计时口径

- `CNTFRQ_EL0` 在 QEMU virt 上是 1 GHz。
- 默认用 `-icount shift=0`：虚拟时间按**访客指令数**推进，1 条指令 ≈ 1 ns，
  三次重跑数字完全一致。此时 `ref_ns/simd_ns` 实质是动态指令数，是跨版本比较
  的稳定代理量。
- 去掉 `-icount` 得到墙钟时间，受宿主负载影响极大（同一二进制两次能差 4 倍），
  不要用它做门控判定。

在真实 aarch64 Linux 上不要用这套夹具，直接用 `clock_gettime`/`perf stat`，
计时才有微架构意义。见 `references/toolchain-and-run.md`。

## 已知限制

- 只测函数本身，不含内存层次效应；对访存受限的函数，结论可能偏乐观。
- `-ffreestanding` 下没有 libc；被测函数如果调用 `malloc`、`printf`、`pthread`，
  必须换成桩实现或改用 Linux 原生档。
- QEMU `-cpu max` 不模拟具体微架构，指令级调度/端口冲突不可见。
