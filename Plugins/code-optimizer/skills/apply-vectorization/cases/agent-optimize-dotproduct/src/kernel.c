#include "kernel.h"

double dot_product(const float *a, const float *b, int n) {
    double sum = 0.0;
    for (int i = 0; i < n; i++) {
        sum += (double)a[i] * b[i];
    }
    return sum;
}