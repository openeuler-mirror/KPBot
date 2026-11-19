// probe_hwcap.c — verify folly's crc32c_isa_l_*_supported() logic on this CPU
//
// Build: clang -O2 -o probe_hwcap probe_hwcap.c
// (deliberately NO -march=... : reproduces the baseline armv8-a build case)
//
// SVE2 verdict: this probe reports BOTH the predicate as written in folly
// (no HWCAP_SVE cross-check) and the hardened predicate recommended by
// SKILL.md 3f (adds `hwcap & HWCAP_SVE`, using the architectural invariant
// SVE2 ⊆ SVE). The dispatch outcome for the tiered revision uses the
// hardened predicate, so a machine whose HWCAP2 falsely claims SVE2 while
// HWCAP lacks SVE can never be steered into SVE2 assembly.
//
// Machine-checkable assertion (used by the fake-CPU test):
//   PROBE_HWCAP_EXPECT_SVE2=0|1  — compare against the hardened predicate;
//   mismatch prints ASSERT FAIL and exits 2.
//
// Companion fake-CPU case (forged "HWCAP2 claims SVE2, HWCAP has no SVE"):
//   clang -O2 -o probe_hwcap_fake_sve2_test probe_hwcap_fake_sve2_test.c \
//       -Wl,--wrap=getauxval
//   PROBE_HWCAP_EXPECT_SVE2=0 ./probe_hwcap_fake_sve2_test   # exit 0 = pass

#include <stdio.h>
#include <stdlib.h>
#include <sys/auxv.h>

#ifdef __linux__
#include <asm/hwcap.h>
#endif

// Reproduce folly/external/isa-l-crc/Crc32cIsaL.cpp's #ifndef fallbacks verbatim,
// but under different names so we can tell whether the system header supplied them.
#ifndef AT_HWCAP
#define AT_HWCAP 16
#define AT_HWCAP_WAS_FALLBACK 1
#else
#define AT_HWCAP_WAS_FALLBACK 0
#endif

#ifndef AT_HWCAP2
#define AT_HWCAP2 26
#define AT_HWCAP2_WAS_FALLBACK 1
#else
#define AT_HWCAP2_WAS_FALLBACK 0
#endif

#ifndef HWCAP_CRC32
#define HWCAP_CRC32 (1UL << 7)
#define HWCAP_CRC32_WAS_FALLBACK 1
#else
#define HWCAP_CRC32_WAS_FALLBACK 0
#endif

#ifndef HWCAP_ASIMD
#define HWCAP_ASIMD (1UL << 1)
#define HWCAP_ASIMD_WAS_FALLBACK 1
#else
#define HWCAP_ASIMD_WAS_FALLBACK 0
#endif

#ifndef HWCAP_PMULL
#define HWCAP_PMULL (1UL << 4)
#define HWCAP_PMULL_WAS_FALLBACK 1
#else
#define HWCAP_PMULL_WAS_FALLBACK 0
#endif

// HWCAP_SVE is not part of folly's original predicate; it is the hardening
// cross-check (SVE2 ⊆ SVE architectural invariant). Fallback per arm64 ABI.
#ifndef HWCAP_SVE
#define HWCAP_SVE (1UL << 22)
#define HWCAP_SVE_WAS_FALLBACK 1
#else
#define HWCAP_SVE_WAS_FALLBACK 0
#endif

#ifndef HWCAP2_SVE2
#define HWCAP2_SVE2 (1UL << 1)
#define HWCAP2_SVE2_WAS_FALLBACK 1
#else
#define HWCAP2_SVE2_WAS_FALLBACK 0
#endif

#ifndef HWCAP2_SVEPMULL
#define HWCAP2_SVEPMULL (1UL << 3)
#define HWCAP2_SVEPMULL_WAS_FALLBACK 1
#else
#define HWCAP2_SVEPMULL_WAS_FALLBACK 0
#endif

#ifndef HWCAP2_SVESHA3
#define HWCAP2_SVESHA3 (1UL << 5)
#define HWCAP2_SVESHA3_WAS_FALLBACK 1
#else
#define HWCAP2_SVESHA3_WAS_FALLBACK 0
#endif

int main(void) {
    const unsigned long hwcap  = getauxval(AT_HWCAP);
    const unsigned long hwcap2 = getauxval(AT_HWCAP2);

    printf("=== constant provenance (1 = folly's #ifndef fallback kicked in) ===\n");
    printf("  AT_HWCAP        fallback=%d  value-used=%lu\n", AT_HWCAP_WAS_FALLBACK, (unsigned long)AT_HWCAP);
    printf("  AT_HWCAP2       fallback=%d  value-used=%lu\n", AT_HWCAP2_WAS_FALLBACK, (unsigned long)AT_HWCAP2);
    printf("  HWCAP_ASIMD     fallback=%d  value-used=0x%lx\n", HWCAP_ASIMD_WAS_FALLBACK, (unsigned long)HWCAP_ASIMD);
    printf("  HWCAP_PMULL     fallback=%d  value-used=0x%lx\n", HWCAP_PMULL_WAS_FALLBACK, (unsigned long)HWCAP_PMULL);
    printf("  HWCAP_CRC32     fallback=%d  value-used=0x%lx\n", HWCAP_CRC32_WAS_FALLBACK, (unsigned long)HWCAP_CRC32);
    printf("  HWCAP_SVE       fallback=%d  value-used=0x%lx  (hardening cross-check, not in folly's predicate)\n", HWCAP_SVE_WAS_FALLBACK, (unsigned long)HWCAP_SVE);
    printf("  HWCAP2_SVE2     fallback=%d  value-used=0x%lx\n", HWCAP2_SVE2_WAS_FALLBACK, (unsigned long)HWCAP2_SVE2);
    printf("  HWCAP2_SVEPMULL fallback=%d  value-used=0x%lx\n", HWCAP2_SVEPMULL_WAS_FALLBACK, (unsigned long)HWCAP2_SVEPMULL);
    printf("  HWCAP2_SVESHA3  fallback=%d  value-used=0x%lx\n", HWCAP2_SVESHA3_WAS_FALLBACK, (unsigned long)HWCAP2_SVESHA3);

    printf("\n=== raw auxv ===\n");
    printf("  getauxval(AT_HWCAP)  = 0x%016lx\n", hwcap);
    printf("  getauxval(AT_HWCAP2) = 0x%016lx\n", hwcap2);

    printf("\n=== individual bit tests ===\n");
    printf("  hwcap  & ASIMD    = %d\n", !!(hwcap & HWCAP_ASIMD));
    printf("  hwcap  & PMULL    = %d\n", !!(hwcap & HWCAP_PMULL));
    printf("  hwcap  & CRC32    = %d\n", !!(hwcap & HWCAP_CRC32));
    printf("  hwcap  & SVE      = %d\n", !!(hwcap & HWCAP_SVE));
    printf("  hwcap2 & SVE2     = %d\n", !!(hwcap2 & HWCAP2_SVE2));
    printf("  hwcap2 & SVEPMULL = %d\n", !!(hwcap2 & HWCAP2_SVEPMULL));
    printf("  hwcap2 & SVESHA3  = %d\n", !!(hwcap2 & HWCAP2_SVESHA3));

    // folly's exact predicate, as written (no HWCAP_SVE cross-check)
    const int sve2_as_written =
        (hwcap & HWCAP_ASIMD) && (hwcap & HWCAP_PMULL) && (hwcap & HWCAP_CRC32) &&
        (hwcap2 & HWCAP2_SVE2) && (hwcap2 & HWCAP2_SVEPMULL) && (hwcap2 & HWCAP2_SVESHA3);
    // hardened predicate (SKILL.md 3f 加固①): SVE2 ⊆ SVE is an architectural
    // invariant, so a missing HWCAP_SVE vetoes any HWCAP2 SVE2 claim
    const int sve2 = sve2_as_written && (hwcap & HWCAP_SVE);
    const int pmull = (hwcap & HWCAP_ASIMD) && (hwcap & HWCAP_PMULL) && (hwcap & HWCAP_CRC32);
    const int h3fs = (hwcap & HWCAP_CRC32) != 0;

    printf("\n=== folly predicates ===\n");
    printf("  crc32c_isa_l_sve2_supported() as-written (no SVE cross-check) = %d\n", sve2_as_written);
    printf("  crc32c_isa_l_sve2_supported() hardened (+ HWCAP_SVE)          = %d\n", sve2);
    if (sve2_as_written && !sve2)
        printf("  ** HWCAP2 claims SVE2 but HWCAP lacks SVE: as-written predicate is a FALSE POSITIVE;\n"
               "     the hardened predicate correctly vetoes it. Do not enter SVE2 assembly here. **\n");
    printf("  crc32c_isa_l_pmull_supported() = %d\n", pmull);
    printf("  h3fs_crc32c_hw_supported()     = %d\n", h3fs);

    printf("\n=== dispatch outcome per folly revision ===\n");
    printf("  801edb7d4 (ISA-L only, as-written predicate)\n"
           "                           : crc32c_hw -> %s\n",
           sve2_as_written ? "SVE2 (crc32_iscsi_fusion_p8_c10_asm)"
                           : (pmull ? "PMULL (crc32_iscsi_x6)" : "throw"));
    printf("  64b1a2a2a (crc32c_m)     : crc32c_hw -> %s\n",
           h3fs ? "scalar arm_hw_crc32c (ISA-L unreachable on every platform)"
                : "throw");
    printf("  9ec5c014b (tiered, hardened predicate)\n"
           "                           : crc32c_hw -> %s\n",
           !h3fs ? "throw"
                 : (sve2 ? "SVE2 (crc32_iscsi_fusion_p8_c10_asm)"
                         : (pmull ? "PMULL (crc32_iscsi_x6)"
                                  : "scalar arm_hw_crc32c")));
    printf("\n  The tiered revision is what this kit validates: it must pick SVE2 on\n"
           "  Kunpeng-950 class parts, PMULL on Kunpeng-920 class parts, and scalar\n"
           "  elsewhere -- and, with the HWCAP_SVE cross-check now built into the\n"
           "  hardened predicate above, it must NOT pick SVE2 when HWCAP2 claims\n"
           "  SVE2 while HWCAP lacks SVE (see probe_hwcap_fake_sve2_test.c).\n");

    const char *expect = getenv("PROBE_HWCAP_EXPECT_SVE2");
    if (expect) {
        const int want = atoi(expect);
        if (want != sve2) {
            printf("\nASSERT FAIL: hardened sve2=%d, expected=%d\n", sve2, want);
            return 2;
        }
        printf("\nASSERT OK: hardened sve2=%d as expected\n", sve2);
    }

    return 0;
}
