# 派发门定位手册

配合 `SKILL.md` 使用。目标是把「运行时为什么没走最优实现」拆成编译期、能力探测、型号启发式三层，并给出每层的取证方法。

## 1. 三层门的代码形态

### 编译期

目标实现可能因为构建配置、目标平台判断或源码清单而没有编进二进制。检查顺序：

```text
1. 源码里存在实现文件（asm/、*_armv8*、*_neon*、*_sve* 等；跨平台库按目标架构过滤）
2. 构建配置包含该平台（CMake/configure/meson 的目标判断、-D 宏）
3. 产物中真的存在实现：自建未 strip 的产物用 `nm lib.so | grep <kernel_name>`、`readelf -sW` 或 `objdump -t` 查完整符号表；`nm -D` 只能看到动态导出的公开符号，内部 asm 内核通常不可见。发行版 strip 后没有 symtab 时，用 `objdump -d` 搜目标指令特征兜底
4. 构建日志没有跳过该文件，编译选项没有禁用（如库的 NO_ASM/禁用汇编开关）
```

三层门里这一层不命中，属于构建问题，不在本 skill 范围。

### 能力探测

硬件是否支持目标指令集。常见来源：

| 平台/形态 | 探测方式 | 位定义位置 |
|---|---|---|
| Linux AArch64 | `getauxval(AT_HWCAP/AT_HWCAP2)` | 库内头文件或 `asm/hwcap.h` |
| 跨平台库 | 库自己的 `*_probe`/`*_supported` | 库的 cpu feature 抽象层 |
| 运行时 JIT | 平台特性查询 API | 运行时或库内 |

能力位为假时无解，停止。

### 型号启发式

能力位之上按 CPU 型号/微架构决定「这条实现是否值得用」。常见形态：

```text
1. MIDR/part number 白名单：switch/数组里列出支持的型号
2. *_WORTH_USING 标志：能力支持之后再叠一层经验判断
3. ifunc / 多版本函数：resolver 里比较 CPU 型号或特性
4. 微架构分档表：按 implementer/part 映射到微架构代号再查表
5. 插件式派发：实现注册表按 CPU/环境选择
```

这一层不命中就是本 skill 的候选缺口，但要和「能力位未命中」严格区分。

## 2. 关键字表

在库源码里搜以下关键字，定位派发点和白名单：

```text
getauxval  AT_HWCAP  HWCAP2  hwcap
MIDR  midr  main_id  implementer  partnum  part_number  cpu_part
cpuinfo  sysctl
armcap  *_armcap  *_CAPABLE  *_SUPPORTED  is_*_supported
*_WORTH_USING  worth_using  preferred  prefer_*
ifunc  target_clones  dispatch  resolver
isa_traits  cpu_features  cpu_model  microarchitecture
```

建议从 **消费点** 反查：先通过 perf/符号找到慢实现，再搜哪个条件选择了它，最后回到底层能力位和型号表。只顺着能力位定义往下找容易漏掉分散在多个文件的条件。

## 3. MIDR 与型号

本 skill 面向 AArch64；型号白名单匹配的是 MIDR 里的字段。MIDR_EL1 包含 implementer、variant、architecture、part number、revision。常见读法：

```text
# /proc/cpuinfo（普通用户可读，最省事）
CPU implementer : 0x48
CPU part        : 0xd06
CPU variant     : 0x0

# 内联汇编（可用性依赖内核与权限，失败时回退 /proc/cpuinfo）
uint64_t midr;
asm volatile("mrs %0, midr_el1" : "=r"(midr));
# implementer = (midr >> 24) & 0xff
# part        = (midr >> 4)  & 0xfff
```

注意事项：

- 以实读 MIDR 的 implementer/part 为准；匹配型号常量时按目标库实际使用的字段查找，不要只按 CPU 名称或文档型号描述反推。
- 同一 part number 可能在头文件里有多个常量或别名；用 `grep` 列出目标树中该 part 的全部定义，确认新增或复用哪一个。
- 型号白名单可能在架构头文件、算子实现目录或派发层，按消费点定位，不按目录猜。
- 修改只加型号常量与列表项，保留能力位前置条件。

## 4. 能力位与派发结果的 probe

### 读 CPU 标识的前置条件

AArch64 上，库内缓存的 MIDR（如 OpenSSL 的 `OPENSSL_arm_midr`）通常只在 `HWCAP_CPUID` 置位时由 `_armv8_cpuid_probe()` 填充；未置位时值为 0，型号白名单比较不会命中，补丁后目标位也不会翻转。

先确认 `getauxval(AT_HWCAP) & HWCAP_CPUID`（或 `/proc/cpuinfo` 的 Features 含 `cpuid`），再采信 MIDR 白名单路径；前置不满足时保留 `needs_evidence`，不能据此判缺口。

### 读能力位/派发标志

优先使用库导出的运行时常量或诊断接口：

```c
/* 占位：把 lib_isa_flags 换成目标库导出的运行时常量或诊断接口 */
#include <stdio.h>
extern unsigned int lib_isa_flags;
int main(void) { printf("isa_flags=0x%08x\n", lib_isa_flags); return 0; }
```

符号未导出时，选择库的调试开关、日志或 `gdb` 读全局变量。不要只依赖编译期宏。若库用 ifunc、函数指针等直接派发、没有可读的派发标志，不要要求位翻转；以「命中的实现符号或入口断点」作为证据。

### 确认实际实现

```bash
perf record -g -p <pid> -- sleep 10
perf report --stdio | grep -E '<kernel_name>|<dispatch_symbol>'
```

或者在实现入口下断点/插桩，确认调用栈确实进入慢实现。若热点不在该路径，缺口的收益会被其它瓶颈淹没。

### 最小 A/B probe

同一台机器、同一构建选项，分别用原文件与带改动的文件构建：

```text
1. 记录补丁前的能力位与派发标志（位掩码），以及实际命中的实现符号或入口断点
2. 打补丁后重复读取/确认
3. 有派发标志时只允许目标位变化；无派发标志时确认命中的实现符号切换；其它可观测项变化说明改动超出预期
4. 同一次运行内确认慢实现已切换到目标实现
```

增量构建可能因 mtime/缓存跳过重编，必须用位掩码或符号确认补丁真的生效。

## 5. FIPS、合规与上游约束

- 实现文件可能出现在 FIPS 模块的源码清单或校验和中；改动会改变模块哈希，提交前确认重新认证与发布流程。
- 部分库对白名单改动有额外测试要求（算法向量、模块自检），提前查 CONTRIBUTING/安全策略。
- 上游可能已有排除某型号的理由（性能回退、勘误、认证限制）。先用注释、`git log -S`、issue 核对；有可复现理由时停止。
- 型号常量属于公开头文件的一部分，命名要与目标树已有风格一致，避免同一 part 出现两套名字。

## 6. 记录模板

```text
库/版本:
目标机器:        MIDR=0x... implementer=0x.. part=0x...
构建选项:
实现等级:        已编入的实现 / 未编入的实现
能力位:          <bit 名称>=0/1，来源
型号白名单:      文件:行，包含/不包含目标 part
消费点:          文件:行，位如何决定实现
补丁前派发结果:
补丁后派发结果:   位掩码差异（应只有目标位）
正确性:          测试/向量集命令与结果
性能:            见 shared-machine-ab.md 的报表模板
结论:            whitelist_gap / not_applicable / needs_evidence
未验证项:
```
