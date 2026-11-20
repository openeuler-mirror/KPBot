/*
 * verify_crc32c_convention.c — CRC32C 加速实现的约定核验与差分验证
 *
 * 用途: 在接入任何 CRC32C 汇编/intrinsic 加速实现之前，先证明它与参考实现
 *       bit-exact，并确定前后取反约定。KAT 不过就不要往下走 —— 后面所有
 *       性能数字都建立在错误结果上。
 *
 * 构建（把待验证的 .S 一起编进来）:
 *   clang -O2 -Wall -o verify_crc32c_convention \
 *       verify_crc32c_convention.c crc32_iscsi_x6.S \
 *       -I<path/to/folly/external/include>
 *   ./verify_crc32c_convention
 *
 * .S 里通常写 #include "../include/aarch64_label.h"，若 -I 不便，可先把
 * aarch64_label.h 放到 .S 的 ../include/ 相对位置。
 *
 * 验证别的实现: 改 CANDIDATE_SYM 与 candidate_raw() 的调用即可。约定若是
 * “收发已取反的公开值”，把 EXPECT_INVERT 改成 0 重跑，两组都试一遍就能
 * 确定实际约定。
 *
 * 退出码: 0 = 全部通过; 1 = 有不一致; 2 = 参数/环境错误。
 */

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ---- 待验证的汇编符号（AAPCS64: BUF=x0, LEN=x1/w1, wCRC=w2） ---- */
#ifndef CANDIDATE_SYM
#define CANDIDATE_SYM crc32_iscsi_x6
#endif
extern uint32_t CANDIDATE_SYM(const uint8_t *data, int len, uint32_t crc);

/* 1 = 公开 API 与汇编之间差一个前后取反（ISA-L crc32_iscsi_* 的情况） */
#ifndef EXPECT_INVERT
#define EXPECT_INVERT 1
#endif

/* ---- 参考实现: 软件查表 CRC32C ----
 * CRC32C / Castagnoli 多项式 0x1EDC6F41（含隐含位 0x11EDC6F41），
 * 反射形式 0x82F63B78。公开 API 带标准的前后取反。
 */
#define CRC32C_POLY_REFLECTED 0x82F63B78u

static uint32_t g_table[256];

static void build_table(void)
{
    for (uint32_t i = 0; i < 256; i++) {
        uint32_t crc = i;
        for (int k = 0; k < 8; k++)
            crc = (crc & 1u) ? ((crc >> 1) ^ CRC32C_POLY_REFLECTED) : (crc >> 1);
        g_table[i] = crc;
    }
}

/* 内部状态推进，不做前后取反 */
static uint32_t crc32c_update(uint32_t crc, const uint8_t *data, size_t len)
{
    for (size_t i = 0; i < len; i++)
        crc = g_table[(crc ^ data[i]) & 0xFFu] ^ (crc >> 8);
    return crc;
}

/* 公开约定: 前后取反 */
static uint32_t crc32c_ref(uint32_t seed, const uint8_t *data, size_t len)
{
    return ~crc32c_update(~seed, data, len);
}

static uint32_t candidate_raw(const uint8_t *data, size_t len, uint32_t crc)
{
    if (len > (size_t)INT32_MAX) {
        fprintf(stderr, "ERROR: len %zu exceeds candidate's int length limit\n", len);
        exit(2);
    }
    return CANDIDATE_SYM(data, (int)len, crc);
}

static uint32_t candidate(uint32_t seed, const uint8_t *data, size_t len)
{
#if EXPECT_INVERT
    return ~candidate_raw(data, len, ~seed);
#else
    return candidate_raw(data, len, seed);
#endif
}

/* ---- LCG 填充，避免全零缓冲被特殊对待 ---- */
#define LCG_MULT 6364136223846793005ULL
#define LCG_ADD  1442695040888963407ULL

static void fill_lcg(uint8_t *buf, size_t len, uint64_t seed)
{
    uint64_t s = seed;
    for (size_t i = 0; i < len; i++) {
        s = s * LCG_MULT + LCG_ADD;
        buf[i] = (uint8_t)(s >> 33);
    }
}

/* 覆盖: 空 / 尾块各档 / 64B 循环门槛 / 主折叠循环门槛(1024) / 生产尺寸 / 门槛外零头 */
static const size_t kLengths[] = {
    0, 1, 2, 3, 4, 7, 8, 15, 16, 63, 64, 65,
    1023, 1024, 1025, 1048576, 1048583 /* 1MB+7 */
};
#define N_LENGTHS (sizeof(kLengths) / sizeof(kLengths[0]))

static const uint32_t kSeeds[] = {
    0x00000000u, 0xFFFFFFFFu, 0xDEADBEEFu, 0x01020304u
};
#define N_SEEDS (sizeof(kSeeds) / sizeof(kSeeds[0]))

#define MAX_LEN 1048583u
#define FILL_SEED 0x12345678ULL

int main(void)
{
    build_table();

    uint8_t *buf = (uint8_t *)malloc(MAX_LEN);
    if (!buf) {
        fprintf(stderr, "ERROR: malloc(%u) failed\n", MAX_LEN);
        return 2;
    }
    fill_lcg(buf, MAX_LEN, FILL_SEED);

    int fail = 0;

    /* ---- 1. 已知答案测试 (KAT): CRC32C("123456789") == 0xE3069283 ---- */
    const char *kat_in = "123456789";
    const size_t kat_len = 9;
    const uint32_t kat_expect = 0xE3069283u;

    uint32_t ref_kat  = crc32c_ref(0, (const uint8_t *)kat_in, kat_len);
    uint32_t cand_kat = candidate(0, (const uint8_t *)kat_in, kat_len);
    /* 未取反的裸调用，用于展示约定差异 */
    uint32_t raw_kat  = candidate_raw((const uint8_t *)kat_in, kat_len, 0);

    printf("=== [1] KAT: CRC32C(\"123456789\") expect 0x%08X ===\n", kat_expect);
    printf("  reference  (software table)      : 0x%08X  %s\n",
           ref_kat, ref_kat == kat_expect ? "OK" : "FAIL");
    printf("  candidate  (EXPECT_INVERT=%d)     : 0x%08X  %s\n",
           EXPECT_INVERT, cand_kat, cand_kat == kat_expect ? "OK" : "FAIL");
    printf("  candidate raw (no inversion)     : 0x%08X  %s\n",
           raw_kat, raw_kat == kat_expect ? "OK" : "(expected to differ)");
    if (ref_kat != kat_expect) {
        printf("  FATAL: reference implementation is wrong, aborting\n");
        free(buf);
        return 2;
    }
    if (cand_kat != kat_expect) {
        printf("  FAIL: convention mismatch — try rebuilding with -DEXPECT_INVERT=%d\n",
               EXPECT_INVERT ? 0 : 1);
        fail = 1;
    }
    printf("\n");

    /* ---- 2. 多种子 × 多长度差分 ---- */
    printf("=== [2] differential: %zu seeds x %zu lengths ===\n",
           N_SEEDS, N_LENGTHS);
    size_t total = 0, mismatch = 0;
    for (size_t si = 0; si < N_SEEDS; si++) {
        for (size_t li = 0; li < N_LENGTHS; li++) {
            size_t len = kLengths[li];
            uint32_t seed = kSeeds[si];
            uint32_t a = crc32c_ref(seed, buf, len);
            uint32_t b = candidate(seed, buf, len);
            total++;
            if (a != b) {
                mismatch++;
                printf("  MISMATCH seed=0x%08X len=%-8zu ref=0x%08X cand=0x%08X\n",
                       seed, len, a, b);
            }
        }
    }
    printf("  checked %zu pairs, mismatch %zu  %s\n",
           total, mismatch, mismatch == 0 ? "OK" : "FAIL");
    if (mismatch)
        fail = 1;
    printf("\n");

    /* ---- 3. 分块串联等价性（验证可否按 int 上限分块调用） ---- */
    printf("=== [3] chunked chaining equivalence (len=1MB+7, 3 chunks) ===\n");
    {
        const size_t len = 1048583u;
        const size_t c1 = 1048576u, c2 = 4u, c3 = len - c1 - c2;
        uint32_t whole = crc32c_ref(0, buf, len);
#if EXPECT_INVERT
        uint32_t s = candidate_raw(buf, c1, ~0u);
        s = candidate_raw(buf + c1, c2, s);
        s = candidate_raw(buf + c1 + c2, c3, s);
        uint32_t chained = ~s;
#else
        uint32_t s = candidate_raw(buf, c1, 0u);
        s = candidate_raw(buf + c1, c2, s);
        s = candidate_raw(buf + c1 + c2, c3, s);
        uint32_t chained = s;
#endif
        printf("  whole    : 0x%08X\n", whole);
        printf("  chained  : 0x%08X  %s\n", chained,
               chained == whole ? "OK" : "FAIL");
        if (chained != whole)
            fail = 1;
    }
    printf("\n");

    /* ---- 4. 非对齐起点抽样（生产缓冲来自 DMA，起点未必对齐） ---- */
    printf("=== [4] unaligned start offsets (len=4096) ===\n");
    {
        size_t bad = 0, n = 0;
        for (size_t off = 0; off <= 64; off++) {
            uint32_t a = crc32c_ref(0, buf + off, 4096);
            uint32_t b = candidate(0, buf + off, 4096);
            n++;
            if (a != b) {
                bad++;
                printf("  MISMATCH off=%-3zu ref=0x%08X cand=0x%08X\n", off, a, b);
            }
        }
        printf("  checked %zu offsets, mismatch %zu  %s\n",
               n, bad, bad == 0 ? "OK" : "FAIL");
        if (bad)
            fail = 1;
    }
    printf("\n");

    free(buf);

    if (fail) {
        printf("RESULT: FAIL — 约定或实现不一致，禁止进入性能测量\n");
        return 1;
    }
    printf("RESULT: PASS — bit-exact，约定为 candidate(seed,d) == %sSYM(d, len, %sseed)\n",
           EXPECT_INVERT ? "~" : "", EXPECT_INVERT ? "~" : "");
    printf("        下一步: 跨语言/跨进程一致性核验（若 checksum 由一端算、另一端校验）\n");
    return 0;
}
