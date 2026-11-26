#include "kernel.h"

void matmul(const float *A, const float *B, float *C, int N) {
    for (int i = 0; i < N; i++) {
        for (int j = 0; j < N; j++) {
            float s = 0.0f;
            for (int k = 0; k < N; k++) {
                s += A[i * N + k] * B[k * N + j];
            }
            C[i * N + j] = s;
        }
    }
}