/* Bare-metal entry for AArch64 / QEMU virt.
 *
 * Boot order matters here; each step fixes a failure mode that is otherwise
 * silent or misattributed:
 *
 *  1. SP       : main() pushes to the stack immediately.
 *  2. .bss     : the page table and every test buffer must start zeroed.
 *  3. MMU      : see below.
 *  4. CPACR    : SVE traps as undefined until ZEN is set.
 *  5. ZCR_EL1  : reset vector length is 128 bits, not the -msve-vector-bits value.
 *
 * Why the MMU is mapped at all: with translation disabled, AArch64 treats every
 * data access as Device-nGnRnE, which requires natural alignment. Compilers
 * happily turn a 16-active-byte svst1 into `str q0`; if the destination is only
 * 8-byte aligned (a non-multiple-of-16 row stride, for instance) the store takes
 * a Data Abort regardless of SCTLR_EL1.A. A one-entry identity map over the
 * image's 1 GiB block makes memory Normal, so unaligned access works the way it
 * does in a real Linux process -- which is what these benchmarks are meant to
 * model.
 */
    .section .text.boot, "ax"
    .global _start
_start:
    ldr     x0, =_stack_top
    mov     sp, x0
    bl      __clear_bss
    bl      __cpu_init
    bl      main
    mov     x0, #0x18          /* semihosting SYS_EXIT (kept for old paths) */
    mov     x1, #0
    hlt     #0xf000
1:  b       1b

/* ---------------------------------------------------------------- bss */
__clear_bss:
    ldr     x0, =__bss_start
    ldr     x1, =__bss_end
1:  cmp     x0, x1
    b.hs    2f
    str     xzr, [x0], #8
    b       1b
2:  ret

/* ------------------------------------------------------------- cpu init */
__cpu_init:
    /* MMU off by default: turn on a single 1 GiB identity block at 0x40000000
     * so that data accesses are Normal memory (unaligned vector stores OK). */
    ldr     x0, =_l1_table          /* already zeroed by __clear_bss */

    /* Block descriptor for VA 0x40000000..0x7FFFFFFF:
     * valid | block | AttrIndx0 | SH=inner | AF=1 | output addr. */
    ldr     x1, =0x40000701
    mov     x2, #1                 /* index = 0x40000000 >> 30 */
    str     x1, [x0, x2, lsl #3]

    msr     ttbr0_el1, x0
    /* T0SZ=25 (39-bit VA), IRGN0=WB, ORGN0=WB, SH0=inner, TG0=4K,
     * EPD1=1 (no TTBR1 walks), IPS=40-bit PA. */
    ldr     x1, =0x200803519
    msr     tcr_el1, x1
    isb

    /* MAIR Attr0 = Normal, inner/outer write-back, non-transient, RW alloc */
    mov     x1, #0xFF
    msr     mair_el1, x1
    isb

    /* SCTLR: M=1 (MMU), A=0 (no alignment check), C=1 (data cache), I=1 */
    mrs     x0, sctlr_el1
    bic     x0, x0, #(1 << 1)      /* A = 0 */
    orr     x0, x0, #(1 << 0)      /* M = 1 */
    orr     x0, x0, #(1 << 2)      /* C = 1 */
    orr     x0, x0, #(1 << 12)     /* I = 1 */
    msr     sctlr_el1, x0
    tlbi    vmalle1
    dsb     nsh
    isb

    /* FP/SIMD + SVE must be un-trapped before the first vector instruction. */
    mov     x0, #(3 << 20)         /* FPEN = 0b11 */
    orr     x0, x0, #(3 << 16)     /* ZEN  = 0b11 */
    msr     cpacr_el1, x0
    isb

    /* Vector length is 128 bits at reset. ZCR_EL1.LEN = VL/128 - 1. */
#if defined(SIMD_BENCH_VL_BYTES) && SIMD_BENCH_VL_BYTES != 16
    mov     x0, #((SIMD_BENCH_VL_BYTES / 16) - 1)
    msr     zcr_el1, x0
    isb
#endif
    ret
