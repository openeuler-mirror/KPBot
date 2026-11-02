/*
 * bench/main.c —— 用例前置自检：独立参照实现（oracle）与 DUT 对照。
 *
 * 目的（对照 docs/case-spec.md 中 bench/ 的定位）：
 *   提供"不信任被测函数"的独立正确性参照，用于确认 case 的 fixture 本身是
 *   自洽的（DUT 合格 => oracle 与 DUT 一致），再把结果交给最终产物（符合性
 *   测试框架）去判定。本文件不参与判定，只做 fixture 体检。
 *
 * 用法：./fixture_check            # 全部形状自检
 *       ./fixture_check -v         # 打印逐例结果
 * 输出：末行 CORRECT=1（或 0），进程退出码同 1/0。
 */

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "kernel.h"

static long idx(MiniLayout lo, long r, long c, long ld)
{
    return (lo == MINI_ROW_MAJOR) ? (r * ld + c) : (c * ld + r);
}

static long a_rows(MiniTrans ta, long m, long k) { return (ta == MINI_NO_TRANS) ? m : k; }
static long a_cols(MiniTrans ta, long m, long k) { return (ta == MINI_NO_TRANS) ? k : m; }
static long b_rows(MiniTrans tb, long k, long n) { return (tb == MINI_NO_TRANS) ? k : n; }
static long b_cols(MiniTrans tb, long k, long n) { return (tb == MINI_NO_TRANS) ? n : k; }
static long ld_of(MiniLayout lo, long rows, long cols) { return (lo == MINI_ROW_MAJOR) ? cols : rows; }

static double a_get(MiniLayout lo, MiniTrans ta, const double *A, long lda,
                    long i, long p)
{
    long sr = (ta == MINI_NO_TRANS) ? i : p;
    long sc = (ta == MINI_NO_TRANS) ? p : i;
    return A[idx(lo, sr, sc, lda)];
}

static double b_get(MiniLayout lo, MiniTrans tb, const double *B, long ldb,
                    long p, long j)
{
    long sr = (tb == MINI_NO_TRANS) ? p : j;
    long sc = (tb == MINI_NO_TRANS) ? j : p;
    return B[idx(lo, sr, sc, ldb)];
}

/* 独立参照实现：累加循环以 p 为最外层（与 DUT 的 i-j-p 顺序不同） */
static void ref_gemm(MiniLayout lo, MiniTrans ta, MiniTrans tb,
                     long m, long n, long k, double alpha,
                     const double *A, long lda, const double *B, long ldb,
                     double beta, double *C, long ldc)
{
    long i, j, p;

    if (alpha == 0.0 && beta == 1.0)
        return;
    if (m <= 0 || n <= 0)
        return;
    if (k <= 0) {
        for (j = 0; j < n; ++j)
            for (i = 0; i < m; ++i)
                C[idx(lo, i, j, ldc)] = beta * C[idx(lo, i, j, ldc)];
        return;
    }

    double *acc = (double *)calloc((size_t)(m * n), sizeof(double));
    if (!acc) {
        fprintf(stderr, "calloc failed\n");
        exit(3);
    }
    for (p = 0; p < k; ++p) {
        for (i = 0; i < m; ++i) {
            double a = a_get(lo, ta, A, lda, i, p);
            for (j = 0; j < n; ++j)
                acc[i * n + j] += a * b_get(lo, tb, B, ldb, p, j);
        }
    }
    for (j = 0; j < n; ++j)
        for (i = 0; i < m; ++i) {
            double c0 = C[idx(lo, i, j, ldc)];
            C[idx(lo, i, j, ldc)] = alpha * acc[i * n + j] + beta * c0;
        }
    free(acc);
}

/* 确定性填充：简单 LCG，避免依赖运行期随机 */
static void fill(double *x, long n, unsigned long long seed)
{
    long i;
    unsigned long long s = seed ? seed : 1ULL;
    for (i = 0; i < n; ++i) {
        s = s * 6364136223846793005ULL + 1442695040888963407ULL;
        x[i] = (double)((s >> 11) % 2001) / 1000.0 - 1.0; /* [-1, 1] */
    }
}

static int one_case(MiniLayout lo, MiniTrans ta, MiniTrans tb,
                    long m, long n, long k, double alpha, double beta,
                    int verbose)
{
    long lda, ldb, ldc, na, nb, nc, i;
    double *A, *B, *C1, *C2, *C0;
    int ok = 1;

    lda = ld_of(lo, a_rows(ta, m, k), a_cols(ta, m, k));
    ldb = ld_of(lo, b_rows(tb, k, n), b_cols(tb, k, n));
    ldc = ld_of(lo, (m > 0 ? m : 1), (n > 0 ? n : 1));

    na = (lda > 0 ? lda : 1) * (lo == MINI_ROW_MAJOR ? (a_rows(ta, m, k) > 0 ? a_rows(ta, m, k) : 1)
                                                      : (a_cols(ta, m, k) > 0 ? a_cols(ta, m, k) : 1));
    nb = (ldb > 0 ? ldb : 1) * (lo == MINI_ROW_MAJOR ? (b_rows(tb, k, n) > 0 ? b_rows(tb, k, n) : 1)
                                                      : (b_cols(tb, k, n) > 0 ? b_cols(tb, k, n) : 1));
    nc = (ldc > 0 ? ldc : 1) * ((lo == MINI_ROW_MAJOR ? (m > 0 ? m : 1) : (n > 0 ? n : 1)));

    A  = (double *)malloc((size_t)na * sizeof(double));
    B  = (double *)malloc((size_t)nb * sizeof(double));
    C1 = (double *)malloc((size_t)nc * sizeof(double));
    C2 = (double *)malloc((size_t)nc * sizeof(double));
    C0 = (double *)malloc((size_t)nc * sizeof(double));
    if (!A || !B || !C1 || !C2 || !C0) {
        fprintf(stderr, "malloc failed\n");
        exit(3);
    }

    fill(A, na, 0x1234ULL + (unsigned long long)(m * 7 + n * 3 + k));
    fill(B, nb, 0x5678ULL + (unsigned long long)(m * 5 + n * 11 + k));
    fill(C0, nc, 0x9ABCULL + (unsigned long long)(m + n + k));

    memcpy(C1, C0, (size_t)nc * sizeof(double));
    memcpy(C2, C0, (size_t)nc * sizeof(double));

    ref_gemm(lo, ta, tb, m, n, k, alpha, A, lda, B, ldb, beta, C1, ldc);
    mini_gemm(lo, ta, tb, m, n, k, alpha, A, lda, B, ldb, beta, C2, ldc);

    for (i = 0; i < nc; ++i) {
        double d = fabs(C1[i] - C2[i]);
        double s = fabs(C1[i]) > 1.0 ? fabs(C1[i]) : 1.0;
        if (d > 1e-9 * s) {
            ok = 0;
            break;
        }
    }

    if (verbose || !ok)
        printf("case lo=%d ta=%d tb=%d m=%ld n=%ld k=%ld alpha=%.3f beta=%.3f -> %s\n",
               (int)lo, (int)ta, (int)tb, m, n, k, alpha, beta, ok ? "OK" : "MISMATCH");

    free(A); free(B); free(C1); free(C2); free(C0);
    return ok;
}

int main(int argc, char **argv)
{
    int verbose = (argc > 1 && strcmp(argv[1], "-v") == 0);
    int all = 1;
    MiniLayout los[2] = { MINI_ROW_MAJOR, MINI_COL_MAJOR };
    MiniTrans tas[2] = { MINI_NO_TRANS, MINI_TRANS };
    MiniTrans tbs[2] = { MINI_NO_TRANS, MINI_TRANS };
    long shapes[][3] = {
        { 4, 3, 5 }, { 1, 1, 1 }, { 8, 6, 7 },
        { 5, 1, 4 }, { 1, 5, 4 }, { 7, 2, 0 }, { 0, 3, 5 }, { 3, 0, 5 },
    };
    double alphas[] = { 1.0, 0.0, -1.5, 2.5 };
    double betas[]  = { 0.0, 1.0, 1.5, -0.5 };
    size_t li, ai, bi, si;

    for (li = 0; li < 2; ++li)
        for (ai = 0; ai < 2; ++ai)
            for (bi = 0; bi < 2; ++bi)
                for (si = 0; si < sizeof(shapes) / sizeof(shapes[0]); ++si)
                    all &= one_case(los[li], tas[ai], tbs[bi],
                                    shapes[si][0], shapes[si][1], shapes[si][2],
                                    1.0, 1.5, verbose);

    /* alpha/beta 组合（固定一个中等形状） */
    for (ai = 0; ai < sizeof(alphas) / sizeof(alphas[0]); ++ai)
        for (bi = 0; bi < sizeof(betas) / sizeof(betas[0]); ++bi)
            all &= one_case(MINI_ROW_MAJOR, MINI_NO_TRANS, MINI_NO_TRANS,
                            6, 4, 5, alphas[ai], betas[bi], verbose);

    printf("CORRECT=%d\n", all ? 1 : 0);
    return all ? 0 : 1;
}
