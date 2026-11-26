# 工具链与验证方式

验证方式由**用户在第二问里选**，不由环境自动决定。探测只回答"能不能"，选择回答
"用不用"。

```bash
python3 <skill>/scripts/check_env.py --method native --json   # 校验原生是否可行
python3 <skill>/scripts/check_env.py --method qemu   --json   # 校验 QEMU 是否可行
```

返回里的 `feasible` / `blocking_reasons` 就是要不要停下的依据。用户选了不可行的那一
方时，**停下来把缺什么讲清楚**，不要自动改成另一方——验证方式变了，性能数字的口径也
变了，用户有权知道这件事。

两种方式都**必须让运行时 VL = 256 位（32 字节）**。VL 不对不会报错，只会让每次向量
访存少处理一半数据；本技能见过的最麻烦的调试场景都是这么来的。

## 原生：aarch64 Linux 本机

宿主就是目标架构时，性能和正确性都最有说服力，因为测到的是真实微架构行为。

**可行性前提**（三条都满足才算可行）：`uname -m` 为 `aarch64`、`uname -s` 为 `Linux`、
本机 SVE 最大向量长度 ≥ 256 位。

### 编译

```bash
CFLAGS="-O3 -march=armv9.6-a+sve2p1+sme2p2 -msve-vector-bits=256 \
        -mtune=neoverse-v3 -flto -ffreestanding"
```

`-march` 里的特性必须和目标机器的实际能力一致。`armv9.6-a+sve2p1+sme2p2` 是新一代
配置，老工具链（GCC < 15）不认这个字符串，会直接报错而不是降级。编译不通时先
`aarch64-linux-gnu-gcc -march=help`（或 `gcc --target-help`）确认支持的字符串。

`-ffreestanding` 只在做独立验证时加。**改项目真实代码时不要凭这两行去改项目的
构建系统**——项目的编译参数由项目自己决定。本技能关心的是"这段代码在这个参数下
更快"，不是"给项目换一套参数"。

### 确认运行时 VL 是 256 位

```bash
# 每个线程独立设置；参数是字节数
python3 -c "import ctypes; print(ctypes.CDLL('libc.so.6').prctl(50, 32, 0, 0, 0))"
# 或看内核对新进程的默认值
cat /proc/sys/abi/sve_default_vector_length   # 字节数；部分新内核已移除
```

代码里确认用两条路，都要**读返回值**：

```c
/* 1) 内联汇编读真实 VL；不要用 svcntb()，它在 -msve-vector-bits 下是常量 */
static inline unsigned long sve_vl_bytes(void) { unsigned long v; __asm__("cntb %0" : "=r"(v)); return v; }

/* 2) 内核视角的 VL（字节） */
#include <sys/prctl.h>
int vl = prctl(PR_SVE_GET_VL);   /* 返回字节数 */
```

**`svcntb()` 不能用来验证**——`-msve-vector-bits=256` 下它被常量折叠成 32，永远
"验证通过"，等于没验。

`PR_SVE_SET_VL` 在"内核最大 VL 小于请求值"时**不报错，只返回更小的值**。所以设置完
必须读返回值核对是不是 32，而不是假设成功。

如果目标机最大 VL 只有 128 位，256 位的代码根本跑不对。此时二选一：改用
`-msve-vector-bits=128` 重新出方案，或者放弃 SVE 方向改做 NEON——不要带着不匹配的
假设继续。

### 采样与计时

```bash
perf record -F 999 -g -- ./app <args>          # 热点
perf stat -e cycles,instructions,cache-misses ./app   # 总量
```

同类内计时用 `clock_gettime(CLOCK_MONOTONIC)`，重复多轮取中位数。`perf stat` 的
`cycles` 在容器里可能因为缺少权限拿不到，此时退回 `CLOCK_MONOTONIC`。

### 构建项目

用项目自己的构建系统（CMake / Meson / configure），只把目标文件换掉。**不要**为了
方便而新写一个简化构建——那样编过的不是项目，验证结论也就不成立。

## QEMU：交叉编译 + qemu-system-aarch64

宿主不是 aarch64 Linux、或者用户明确要求在没有目标板的情况下验证时用这一档。
它验证的是**语义和动态指令数**，不验证微架构效应。报告里必须标注这一点。

这是用户可选的正式选项，不是"原生不可用时的降级"——很多场景（CI、评审、
隔离环境）本来就该用 QEMU。

### 工具链检查

```bash
aarch64-elf-gcc --version            # 需要支持 armv9.6-a+sve2p1+sme2p2
qemu-system-aarch64 --version
```

`scripts/check_env.py` 会用 tiny probe 实际编译一次，而不是只看版本号——版本够但
配置串不对的情况很常见。

### 编译与运行

```bash
CFLAGS="-O3 -march=armv9.6-a+sve2p1+sme2p2 -msve-vector-bits=256 \
        -mtune=neoverse-v3 -flto -ffreestanding"

aarch64-elf-gcc $CFLAGS -nostdlib -ffreestanding -T link.ld \
    crt0.S bench_shim.c kernel_ref.c kernel_simd.c bench_runner.c -o bench.elf

qemu-system-aarch64 -M virt -cpu max \
    -icount shift=0 -nographic \
    -semihosting-config enable=on,target=native \
    -kernel bench.elf
```

直接用 `assets/bench/Makefile`，它已经把这些参数和分工固化了：

```bash
make -C <bench-dir> \
     REF_SRC=<ref.c> SIMD_SRC=<simd.c> \
     PROJECT_FLAGS="$CFLAGS" VL_BYTES=32
make -C <bench-dir> run
```

### 五个必须知道的坑

都在 `assets/bench/README.md` 里展开，这里只列结论：

1. **`ZCR_EL1.LEN` 必须显式设成 1**（256 位）。复位后是 128 位，和
   `-msve-vector-bits=256` 不符时**不报错**，只是每次向量访存少写一半。
2. **`CPACR_EL1.FPEN/ZEN` 必须先开**。否则第一条 SVE 指令触发 undefined instruction，
   PC 跳到零向量表，表现为"没有任何输出就挂住"。
3. **必须建一个恒等映射的 MMU**。MMU 关闭时所有数据访问按 Device 处理，要求自然对齐；
   编译器会把 16 个有效字节的 `svst1` 生成成 `str q0`，目标只满足 8 字节对齐就
   Data Abort。`crt0.S` 里用一条 1 GiB block 描述符解决。
4. **夹具不能和被测函数一起 `-flto`**。LTO 会把重复的 64 次调用合并成 1 次，
   `ref_ns` 立刻虚高 7 倍。用 `__attribute__((noipa))` + 独立编译单元隔离。
5. **`-icount shift=0` 才有可复现的计时**。不加时是宿主墙钟时间，同一二进制两次
   能差 4 倍。

排查这类"跑一半挂住"的问题：`qemu-system-aarch64 ... -d int` 会打印 `ESR` / `FAR` /
`ELR`，对着 `objdump` 看 `ELR` 那条指令就知道是哪个访存出了事。

### 它不能证明什么

- 真实缓存/预取行为；
- 分支预测与乱序执行；
- 内存带宽上限；
- NUMA 与大页效应。

所以 QEMU 这档给出的加速比只说明"指令数少了多少"。若一个函数是访存受限的，这档可能
给出乐观结论。最终报告里要把这一点写进"限制"。

## 两者都不可行时（只有静态分析）

没有目标架构、没有交叉工具链、没有 QEMU 时，仍然可以做方案设计和代码改写，但：

- `status` 必须是 `unverified`；
- 产物只放 `process/<fn>/proposed/`，**不写回项目**；
- 最终报告里列出"缺什么才能验收"（缺 qemu？缺 VL=256 的机器？缺样本数据？）。

不要用"编译通过"冒充"验证通过"。编译只证明语法和类型对，不证明结果对，更不证明更快。

## 环境快照

不管哪一档，开工后第一件事是把环境写进 `other/env.json`：

```json
{
  "host": {"os": "darwin", "arch": "arm64"},
  "profile": "cross-qemu",
  "cc": {"path": "/opt/homebrew/bin/aarch64-elf-gcc", "version": "16.2.0",
         "supports_armv9_6": true},
  "qemu": {"path": "/opt/homebrew/bin/qemu-system-aarch64", "version": "11.1.3"},
  "sve": {"compile_bits": 256, "runtime_bytes": 32, "mismatch": false},
  "perf": {"available": false},
  "notes": "QEMU -cpu max，icount shift=0，计时为动态指令数"
}
```

这份快照进最终报告的"环境"一节。同一个项目隔一个月再优化，环境不同结论就不可比。
