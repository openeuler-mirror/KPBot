/* 基准夹具模板：只改 EDIT 区，其余保持不动。
 * 输出协议（run_ab_bench.py 依赖，勿改键名）：
 *   CORRECT ok=<0|1> mismatches=<n> first_bad=<i>
 *   TIME ref_ns=<n> simd_ns=<n>
 *   DONE
 */
#include "bench_shim.h"

/* ================= EDIT BEGIN ================= */
#define ELEM_T          int32_t
#define BENCH_MAX       8192
#define BENCH_LEN       4096          /* 计时用的长度 */
#define ITERS           64            /* 每次计时重复调用的次数 */

/* noipa 阻止 LTO 把被测函数内联/常量传播掉，否则 64 次调用会被合并成 1 次，
 * 计时结果完全失真；被测量的是函数本身，不是调用点。 */
extern void kernel_ref(const ELEM_T *a, const ELEM_T *b, ELEM_T *o, size_t n)
    __attribute__((noipa));
extern void kernel_simd(const ELEM_T *a, const ELEM_T *b, ELEM_T *o, size_t n)
    __attribute__((noipa));

static ELEM_T g_a[BENCH_MAX];
static ELEM_T g_b[BENCH_MAX];
static ELEM_T g_ref[BENCH_MAX];
static ELEM_T g_simd[BENCH_MAX];

static void fill_inputs(void)
{
    uint32_t s = 0x12345678u;
    for (int i = 0; i < BENCH_MAX; i++) {
        s = s * 1103515245u + 12345u;
        g_a[i] = (ELEM_T)(s >> 16);
        s = s * 1103515245u + 12345u;
        g_b[i] = (ELEM_T)(s >> 16);
    }
}
/* ================== EDIT END ================== */

static volatile uint64_t g_sink;

static int run_case(size_t n, int verbose)
{
    memset(g_ref, 0, sizeof(ELEM_T) * n);
    memset(g_simd, 0, sizeof(ELEM_T) * n);
    kernel_ref(g_a, g_b, g_ref, n);
    kernel_simd(g_a, g_b, g_simd, n);
    int bad = 0;
    long first = -1;
    for (size_t i = 0; i < n; i++) {
        if (g_ref[i] != g_simd[i]) { if (!bad) first = (long)i; bad++; }
    }
    if (verbose) {
        sh_puts("CORRECT ");
        sh_put_kv("n", (uint64_t)n);
        sh_put_kv("ok", bad == 0);
        sh_put_kv("mismatches", (uint64_t)bad);
        sh_puts("first_bad=");
        sh_put_i64(first);
        sh_puts("\n");
    }
    return bad;
}

int main(void)
{
    uint64_t freq = sh_freq();

    /* 向量长度自检。编译期假设（__ARM_FEATURE_SVE_BITS）和运行时 cntb 不一致时，
     * 所有向量访存都会静默截断，测试会呈现出"随机错"，很难定位。宁可在这里
     * 直接失败，也不要带着错的 VL 去下结论。 */
    uint64_t vl = sh_vl_bytes();
    sh_puts("VL bytes=");
    sh_put_u64(vl);
    sh_puts(" compiled_bits=");
    sh_put_u64((uint64_t)__ARM_FEATURE_SVE_BITS);
    sh_puts("\n");
    if (vl * 8u != (uint64_t)__ARM_FEATURE_SVE_BITS) {
        sh_puts("FAILED vector length mismatch\n");
        sh_exit(2);
    }

    fill_inputs();

    /* 正确性：全长 + 边界 + 尾部 */
    int total_bad = 0;
    total_bad += run_case(0, 1);
    total_bad += run_case(1, 1);
    total_bad += run_case(BENCH_LEN - 1, 1);
    total_bad += run_case(31, 1);
    total_bad += run_case(32, 1);
    total_bad += run_case(33, 1);
    total_bad += run_case(BENCH_LEN, 1);

    if (total_bad != 0) {
        sh_puts("FAILED correctness\n");
        sh_exit(1);
    }

    /* 预热后再计时。默认先测 ref，定义 BENCH_SIMD_FIRST 则先测 simd：
     * 墙钟口径下顺序会带来偏差，用两份构建交替跑可以把它抵消掉。 */
    for (int i = 0; i < 4; i++) {
        kernel_ref(g_a, g_b, g_ref, BENCH_LEN);
        kernel_simd(g_a, g_b, g_simd, BENCH_LEN);
    }

#ifdef BENCH_SIMD_FIRST
    ELEM_T *first_out = g_simd;
    ELEM_T *second_out = g_ref;
#define FIRST_CALL kernel_simd
#define SECOND_CALL kernel_ref
#else
    ELEM_T *first_out = g_ref;
    ELEM_T *second_out = g_simd;
#define FIRST_CALL kernel_ref
#define SECOND_CALL kernel_simd
#endif

    /* asm 屏障：阻止编译器合并/重排两次循环，也阻止把结果相同的调用折叠掉。 */
    uint64_t t0 = sh_cycles();
    for (int it = 0; it < ITERS; it++) {
        asm volatile("" ::: "memory");
        FIRST_CALL(g_a, g_b, first_out, BENCH_LEN);
        asm volatile("" ::: "memory");
    }
    uint64_t t1 = sh_cycles();
    g_sink += (uint64_t)first_out[BENCH_LEN - 1];

    uint64_t t2 = sh_cycles();
    for (int it = 0; it < ITERS; it++) {
        asm volatile("" ::: "memory");
        SECOND_CALL(g_a, g_b, second_out, BENCH_LEN);
        asm volatile("" ::: "memory");
    }
    uint64_t t3 = sh_cycles();
    g_sink += (uint64_t)second_out[BENCH_LEN - 1];

    uint64_t first_ns = freq ? ((t1 - t0) * 1000000000ull) / freq : (t1 - t0);
    uint64_t second_ns = freq ? ((t3 - t2) * 1000000000ull) / freq : (t3 - t2);

#ifdef BENCH_SIMD_FIRST
    uint64_t ref_ns = second_ns;
    uint64_t sim_ns = first_ns;
#else
    uint64_t ref_ns = first_ns;
    uint64_t sim_ns = second_ns;
#endif

    sh_puts("TIMEBASE freq=");
    sh_put_u64(freq);
    sh_puts(" sink=");
    sh_put_u64(g_sink);
    sh_puts("\n");
    sh_puts("TIME ");
    sh_put_kv("ref_ns", ref_ns);
    sh_put_kv("simd_ns", sim_ns);
    sh_puts("\n");
    sh_puts("DONE\n");
    sh_exit(0);
    return 0;
}
