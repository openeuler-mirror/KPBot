// probe_hwcap_fake_sve2_test.c — 伪造「HWCAP2 谎报 SVE2、HWCAP 无 SVE」的 CPU，
// 验证 probe_hwcap.c 的 SVE2 加固谓词（HWCAP_SVE 交叉校验）不产生假阳性。
//
// 背景: folly commit 64b1a2a2a 声称 HWCAP2 的 SVE2 位在鲲鹏 920 内核上误报
// supported（A 层复核未复现，见 SKILL.md「证据状态」）。无论该声称真假，
// SKILL.md 3f 加固① 要求 SVE2 探测加 HWCAP_SVE 交叉校验（SVE2 ⊆ SVE 是
// 架构不变量）。本用例把该场景变成可机器断言的回归测试。
//
// 原理: -Wl,--wrap=getauxval 链接期拦截 —— probe_hwcap.c 里对 getauxval 的
// 调用全部落到下方 __wrap_getauxval，返回伪造常量；被 include 的
// probe_hwcap.c main() 照常执行真实谓词代码（单一事实源，无逻辑复制）。
//
// 构建: clang -O2 -o probe_hwcap_fake_sve2_test probe_hwcap_fake_sve2_test.c \
//           -Wl,--wrap=getauxval
// 运行: PROBE_HWCAP_EXPECT_SVE2=0 ./probe_hwcap_fake_sve2_test
//       退出 0 且打印 ASSERT OK  = 加固谓词正确否决了谎报的 SVE2，
//                                  分派落 PMULL（预期输出 "PMULL (crc32_iscsi_x6)"）
//       退出 2 且打印 ASSERT FAIL = 谓词假阳性，回归失败
//
// 伪造用例（安全关键组合）: HWCAP 有 ASIMD/PMULL/CRC32、无 SVE；
// HWCAP2 声称 SVE2+SVEPMULL+SVESHA3。

#include <sys/auxv.h>

// arm64 ABI 固定常量（与构建架构头文件无关；x86 上编译运行同样有效，
// 因为 getauxval 已被整体拦截）
#define FAKE_AT_HWCAP        16UL
#define FAKE_AT_HWCAP2       26UL
#define FAKE_HWCAP_ASIMD     (1UL << 1)
#define FAKE_HWCAP_PMULL     (1UL << 4)
#define FAKE_HWCAP_CRC32     (1UL << 7)
// 刻意不置 (1UL << 22)：伪造「HWCAP 无 SVE」
#define FAKE_HWCAP2_SVE2     (1UL << 1)
#define FAKE_HWCAP2_SVEPMULL (1UL << 3)
#define FAKE_HWCAP2_SVESHA3  (1UL << 5)

unsigned long __wrap_getauxval(unsigned long type) {
    if (type == FAKE_AT_HWCAP)
        return FAKE_HWCAP_ASIMD | FAKE_HWCAP_PMULL | FAKE_HWCAP_CRC32;
    if (type == FAKE_AT_HWCAP2)
        return FAKE_HWCAP2_SVE2 | FAKE_HWCAP2_SVEPMULL | FAKE_HWCAP2_SVESHA3;
    return 0;
}

#include "probe_hwcap.c"
