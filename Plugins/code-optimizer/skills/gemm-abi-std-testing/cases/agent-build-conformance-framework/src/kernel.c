/*
 * MiniGEMM —— 待测模块（DUT）的朴素实现。
 * 本实现按 MS-1.0 全部条款实现，作为符合性框架的"合格样本"（预期 0 FAIL）。
 */

#include "kernel.h"

static long idx(MiniLayout lo, long row, long col, long ld)
{
    return (lo == MINI_ROW_MAJOR) ? (row * ld + col) : (col * ld + row);
}

/* 逻辑 A(i,p)（i in [0,m), p in [0,k)） */
static double a_get(MiniLayout lo, MiniTrans ta, const double *A, long lda,
                    long i, long p)
{
    long sr = (ta == MINI_NO_TRANS) ? i : p;
    long sc = (ta == MINI_NO_TRANS) ? p : i;
    return A[idx(lo, sr, sc, lda)];
}

/* 逻辑 B(p,j)（p in [0,k), j in [0,n)） */
static double b_get(MiniLayout lo, MiniTrans tb, const double *B, long ldb,
                    long p, long j)
{
    long sr = (tb == MINI_NO_TRANS) ? p : j;
    long sc = (tb == MINI_NO_TRANS) ? j : p;
    return B[idx(lo, sr, sc, ldb)];
}

static double c_get(MiniLayout lo, const double *C, long ldc, long i, long j)
{
    return C[idx(lo, i, j, ldc)];
}

static void c_set(MiniLayout lo, double *C, long ldc, long i, long j, double v)
{
    C[idx(lo, i, j, ldc)] = v;
}

void mini_gemm(MiniLayout layout,
               MiniTrans transA, MiniTrans transB,
               long m, long n, long k,
               double alpha,
               const double *A, long lda,
               const double *B, long ldb,
               double beta,
               double *C, long ldc)
{
    long i, j, p;

    /* MS-1 §1.1 quick return */
    if (alpha == 0.0 && beta == 1.0)
        return;

    /* MS-1 §1.4 zero dimension */
    if (m <= 0 || n <= 0)
        return;

    /* MS-1 §1.2 empty sum：C := beta * C */
    if (k <= 0) {
        for (j = 0; j < n; ++j)
            for (i = 0; i < m; ++i)
                c_set(layout, C, ldc, i, j, beta * c_get(layout, C, ldc, i, j));
        return;
    }

    /* 主计算：C := alpha * op(A) * op(B) + beta * C（不修改 A/B，见 §1.3） */
    for (j = 0; j < n; ++j) {
        for (i = 0; i < m; ++i) {
            double acc = 0.0;
            for (p = 0; p < k; ++p)
                acc += a_get(layout, transA, A, lda, i, p) *
                       b_get(layout, transB, B, ldb, p, j);
            c_set(layout, C, ldc, i, j,
                  alpha * acc + beta * c_get(layout, C, ldc, i, j));
        }
    }
}
