// stress_x6.c — does crc32_iscsi_x6 actually segfault on Kunpeng 920?
//
// Commit 64b1a2a2a claims: "crc32c_isa_l_pmull 对 >0x400 字节块（正常 chunk 尺寸）
// 在 920 上同样触发段错误". This test tries hard to reproduce that.
//
// Build:
//   clang -O2 -o stress_x6 stress_x6.c crc32_iscsi_x6.S -I<include dir>
// Run:
//   ./stress_x6 [rounds]

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <setjmp.h>

extern uint32_t crc32_iscsi_x6(const uint8_t *data, int len, uint32_t crc);

#define CRC32C_POLY_REFLECTED 0x82F63B78u
static uint32_t g_table[256];

static void build_table(void) {
    for (uint32_t i = 0; i < 256; i++) {
        uint32_t crc = i;
        for (int k = 0; k < 8; k++)
            crc = (crc & 1u) ? ((crc >> 1) ^ CRC32C_POLY_REFLECTED) : (crc >> 1);
        g_table[i] = crc;
    }
}

// raw state advance, no inversion — same convention as the asm
static uint32_t ref_update(uint32_t crc, const uint8_t *d, size_t n) {
    for (size_t i = 0; i < n; i++)
        crc = g_table[(crc ^ d[i]) & 0xFFu] ^ (crc >> 8);
    return crc;
}

#define LCG_MULT 6364136223846793005ULL
#define LCG_ADD  1442695040888963407ULL
static void fill_lcg(uint8_t *b, size_t n, uint64_t s) {
    for (size_t i = 0; i < n; i++) { s = s * LCG_MULT + LCG_ADD; b[i] = (uint8_t)(s >> 33); }
}

// crash trap so we can report which case died
static sigjmp_buf g_jb;
static volatile int g_caught;
static void on_sig(int s) { g_caught = s; siglongjmp(g_jb, 1); }

int main(int argc, char **argv) {
    const int rounds = (argc > 1) ? atoi(argv[1]) : 3;
    build_table();

    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = on_sig;
    sigaction(SIGSEGV, &sa, NULL);
    sigaction(SIGILL, &sa, NULL);
    sigaction(SIGBUS, &sa, NULL);

    // pool big enough for 4MB + slack for unaligned starts
    const size_t POOL = 4u * 1024u * 1024u + 4096u;
    uint8_t *pool = aligned_alloc(4096, POOL);
    if (!pool) { fprintf(stderr, "alloc failed\n"); return 2; }
    fill_lcg(pool, POOL, 0x12345678ULL);

    // lengths straddling every documented threshold:
    //   0x400 (1024) = the claimed segfault boundary and x6's main-fold gate
    //   63/64/65     = .loop_64B gate
    //   1MB, 4MB     = production chunk size and above
    static const size_t lens[] = {
        0, 1, 2, 3, 7, 8, 15, 16, 63, 64, 65,
        1023, 1024, 1025, 1026, 2047, 2048, 2049,
        4095, 4096, 4097, 65535, 65536, 65537,
        524287, 524288, 524289,
        1048575, 1048576, 1048577, 1048583,
        4194303, 4194304, 4194305
    };
    const int NL = (int)(sizeof(lens) / sizeof(lens[0]));

    // alignments: 0..63 plus a page-crossing offset
    static const size_t offs[] = {0,1,2,3,4,7,8,15,16,31,32,33,63,64,4095,4096,4097};
    const int NO = (int)(sizeof(offs) / sizeof(offs[0]));

    static const uint32_t seeds[] = {0x00000000u, 0xFFFFFFFFu, 0xDEADBEEFu, 0x01020304u, 0xA5A5A5A5u};
    const int NS = (int)(sizeof(seeds) / sizeof(seeds[0]));

    unsigned long calls = 0, mismatch = 0, crash = 0, skipped = 0;
    int first_crash_reported = 0;

    for (int r = 0; r < rounds; r++) {
        for (int li = 0; li < NL; li++) {
            for (int oi = 0; oi < NO; oi++) {
                for (int si = 0; si < NS; si++) {
                    size_t len = lens[li];
                    const uint8_t *p = pool + offs[oi];
                    if (offs[oi] + len > POOL) { skipped++; continue; }
                    uint32_t seed = seeds[si];

                    uint32_t got;
                    g_caught = 0;
                    if (sigsetjmp(g_jb, 1) == 0) {
                        got = crc32_iscsi_x6(p, (int)len, seed);
                    } else {
                        crash++;
                        if (!first_crash_reported) {
                            first_crash_reported = 1;
                            printf("!! CRASH sig=%d at len=%zu off=%zu seed=0x%08X (round %d)\n",
                                   g_caught, len, offs[oi], seed, r);
                        }
                        continue;
                    }
                    calls++;

                    uint32_t want = ref_update(seed, p, len);
                    if (got != want) {
                        mismatch++;
                        if (mismatch <= 10)
                            printf("MISMATCH len=%-8zu off=%-5zu seed=0x%08X got=0x%08X want=0x%08X\n",
                                   len, offs[oi], seed, got, want);
                    }
                }
            }
        }
        printf("  round %d done: calls=%lu mismatch=%lu crash=%lu\n", r, calls, mismatch, crash);
    }

    printf("\n=== SUMMARY ===\n");
    printf("  rounds          : %d\n", rounds);
    printf("  successful calls: %lu\n", calls);
    printf("  skipped (bounds): %lu\n", skipped);
    printf("  mismatches      : %lu\n", mismatch);
    printf("  crashes         : %lu\n", crash);
    printf("  verdict         : %s\n",
           (crash == 0 && mismatch == 0)
              ? "crc32_iscsi_x6 is CORRECT and DOES NOT CRASH on this machine"
             : (crash ? "REPRODUCED crash" : "correctness failure"));

    free(pool);
    return (crash || mismatch) ? 1 : 0;
}
