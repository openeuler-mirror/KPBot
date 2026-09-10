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
    int n = (argc > 1) ? atoi(argv[1]) : 1000000;
    int iters = (argc > 2) ? atoi(argv[2]) : 200;

    float *a = (float *)malloc(sizeof(float) * n);
    float *b = (float *)malloc(sizeof(float) * n);
    if (!a || !b) {
        fprintf(stderr, "oom\n");
        return 2;
    }
    for (int i = 0; i < n; i++) {
        a[i] = (float)(i % 100) * 0.001f;
        b[i] = (float)((i * 7) % 101) * 0.002f;
    }

    /* 独立标量参照 */
    double ref = 0.0;
    for (int i = 0; i < n; i++) {
        ref += (double)a[i] * b[i];
    }

    double got = dot_product(a, b, n);
    double rel_err = fabs(got - ref) / (fabs(ref) > 1e-12 ? fabs(ref) : 1.0);

    double t0 = now_s();
    double sink = 0.0;
    for (int k = 0; k < iters; k++) {
        sink += dot_product(a, b, n);
    }
    double t1 = now_s();
    double time_ms = (t1 - t0) * 1000.0 / iters;

    int ok = (rel_err < 1e-4) ? 1 : 0;
    printf("CORRECT=%d\n", ok);
    printf("REL_ERR=%.9g\n", rel_err);
    printf("TIME_MS=%.6f\n", time_ms);
    printf("SINK=%.6f\n", sink);

    free(a);
    free(b);
    return ok ? 0 : 1;
}