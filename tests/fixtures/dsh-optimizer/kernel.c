#include <stddef.h>
#include <stdint.h>

/* Input and scratch are disjoint n-element arrays. Scratch is observable. */
uint64_t transform_sum(const uint32_t *input, uint32_t *scratch, size_t n) {
    for (size_t i = 0; i < n; ++i)
        scratch[i] = input[i] * UINT32_C(3) + UINT32_C(1);
    uint64_t sum = 0;
    for (size_t i = 0; i < n; ++i)
        sum += scratch[i];
    return sum;
}
