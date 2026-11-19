#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <math.h>
#include "kernel.h"

static float next_f32(uint32_t *s) {
    *s = *s * 1664525u + 1013904223u;
    return ((float)(*s) / (float)UINT32_MAX) * 2.0f - 1.0f;
}

int main(int argc, char **argv) {
    int n = (argc > 1) ? atoi(argv[1]) : 1000000;
    int iters = (argc > 2) ? atoi(argv[2]) : 20;

    float *a = (float *)malloc(sizeof(float) * n);
    float *b = (float *)malloc(sizeof(float) * n);
    if (!a || !b) {
        fprintf(stderr, "oom\n");
        return 2;
    }

    uint32_t state = 42;
    for (int i = 0; i < n; i++) {
        a[i] = next_f32(&state);
        b[i] = next_f32(&state);
    }

    /* 独立朴素参照：单累加器真实语义 */
    float ref = 0.0f;
    for (int i = 0; i < n; i++) {
        ref += a[i] * b[i];
    }

    /* 被测一次（正确性） */
    float result = dot(a, b, n);
    double den = fabs((double)ref);
    if (den < 1.0) den = 1.0;
    double max_rel = fabs((double)result - (double)ref) / den;

    /* iters 次循环：稳定测量，计时由 verify.sh 受信方外部墙钟完成 */
    volatile float sink = 0.0f;
    for (int k = 0; k < iters; k++) {
        sink += dot(a, b, n);
    }

    /* 只输出正确性结论（CORRECT/MAX_REL_ERR/SINK），不输出 TIME_MS；
       时间由 verify.sh 用 date +%s%N 外部测量，断掉产物自报 TIME_MS 的伪造路径。 */
    int ok = (max_rel < 1e-5) ? 1 : 0;
    printf("CORRECT=%d\n", ok);
    printf("MAX_REL_ERR=%.9g\n", max_rel);
    printf("SINK=%.6f\n", (double)result + (double)sink * 0.0);

    free(a);
    free(b);
    return ok ? 0 : 1;
}