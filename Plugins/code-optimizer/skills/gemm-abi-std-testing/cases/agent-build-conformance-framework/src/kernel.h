#ifndef MINI_GEMM_H
#define MINI_GEMM_H

/*
 * MiniGEMM —— 待测模块（DUT）接口。
 *
 * 接口契约与语义以 std/mini_standard.md（MS-1.0）为准。
 * 本头文件为固定接口：实现只允许在本签名下实现，不得改动签名。
 *
 * 语义约定：
 *   C 为 m x n；alpha/beta 为标量。
 *   转置：transA=MINI_TRANS 时 A 以 k x m 存储；否则以 m x k 存储。
 *         transB=MINI_TRANS 时 B 以 n x k 存储；否则以 k x n 存储。
 *   前导维：
 *     row-major 时 A(i,p) 位于 A[i*lda + p]（lda >= 存储列数）；
 *     col-major 时 A(i,p) 位于 A[p*lda + i]（lda >= 存储行数）。
 *     B、C 同理（C 为 m x n，ldc）。
 */

typedef enum {
    MINI_ROW_MAJOR = 0,
    MINI_COL_MAJOR = 1
} MiniLayout;

typedef enum {
    MINI_NO_TRANS = 0,
    MINI_TRANS    = 1
} MiniTrans;

void mini_gemm(MiniLayout layout,
               MiniTrans transA, MiniTrans transB,
               long m, long n, long k,
               double alpha,
               const double *A, long lda,
               const double *B, long ldb,
               double beta,
               double *C, long ldc);

#endif /* MINI_GEMM_H */
