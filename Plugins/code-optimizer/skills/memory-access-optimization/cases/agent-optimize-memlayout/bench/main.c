#define _POSIX_C_SOURCE 200809L
#include <stdio.h>
#include <stdlib.h>
#include <math.h>
#include <time.h>
#include "kernel.h"

static double now_s(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

int main(int argc, char **argv) {
    int N = (argc > 1) ? atoi(argv[1]) : 256;
    int iters = (argc > 2) ? atoi(argv[2]) : 5;

    float *A = (float *)malloc(sizeof(float) * N * N);
    float *B = (float *)malloc(sizeof(float) * N * N);
    float *C = (float *)malloc(sizeof(float) * N * N);
    float *Cref = (float *)malloc(sizeof(float) * N * N);
    if (!A || !B || !C || !Cref) {
        fprintf(stderr, "oom\n");
        return 2;
    }

    srand(42);
    for (int i = 0; i < N * N; i++) {
        A[i] = (float)rand() / RAND_MAX * 2.0f - 1.0f;
        B[i] = (float)rand() / RAND_MAX * 2.0f - 1.0f;
    }

    /* 独立朴素参照 */
    for (int i = 0; i < N; i++) {
        for (int j = 0; j < N; j++) {
            float s = 0.0f;
            for (int k = 0; k < N; k++) {
                s += A[i * N + k] * B[k * N + j];
            }
            Cref[i * N + j] = s;
        }
    }

    matmul(A, B, C, N);

    double max_rel = 0.0;
    for (int i = 0; i < N * N; i++) {
        double err = fabs((double)C[i] - (double)Cref[i]);
        double den = fabs((double)Cref[i]);
        if (den < 1.0) den = 1.0;
        double r = err / den;
        if (r > max_rel) max_rel = r;
    }

    double t0 = now_s();
    for (int k = 0; k < iters; k++) {
        matmul(A, B, C, N);
    }
    double t1 = now_s();
    double time_ms = (t1 - t0) * 1000.0 / iters;

    int ok = (max_rel < 1e-3) ? 1 : 0;
    printf("CORRECT=%d\n", ok);
    printf("MAX_REL_ERR=%.9g\n", max_rel);
    printf("TIME_MS=%.6f\n", time_ms);
    printf("SINK=%.6f\n", (double)C[0]);

    free(A); free(B); free(C); free(Cref);
    return ok ? 0 : 1;
}