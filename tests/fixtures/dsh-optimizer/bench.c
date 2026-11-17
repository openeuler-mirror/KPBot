#define _POSIX_C_SOURCE 200809L
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

uint64_t transform_sum(const uint32_t *, uint32_t *, size_t);
static volatile uint64_t sink;

static uint32_t sample(uint32_t *state) {
    *state ^= *state << 13;
    *state ^= *state >> 17;
    *state ^= *state << 5;
    return *state;
}

static int correctness(void) {
    const size_t sizes[] = {0, 1, 2, 3, 7, 16, 17, 31, 257, 65537};
    for (size_t c = 0; c < sizeof(sizes) / sizeof(sizes[0]); ++c) {
        size_t n = sizes[c];
        uint32_t *input = calloc(n + 1, sizeof(*input));
        uint32_t *buffer = calloc(n + 2, sizeof(*buffer));
        if (!input || !buffer) return 2;
        uint32_t state = UINT32_C(123456789);
        uint64_t expected = 0;
        for (size_t i = 0; i < n; ++i) {
            input[i] = sample(&state);
            expected += (uint32_t)(input[i] * UINT32_C(3) + UINT32_C(1));
        }
        buffer[0] = UINT32_C(0x12345678);
        buffer[n + 1] = UINT32_C(0xabcdef01);
        uint64_t actual = transform_sum(input, buffer + 1, n);
        int failed = actual != expected || buffer[0] != UINT32_C(0x12345678)
            || buffer[n + 1] != UINT32_C(0xabcdef01);
        state = UINT32_C(123456789);
        for (size_t i = 0; i < n; ++i) {
            uint32_t original = sample(&state);
            if (input[i] != original || buffer[i + 1] != original * UINT32_C(3) + UINT32_C(1)) failed = 1;
        }
        free(input);
        free(buffer);
        if (failed) { fprintf(stderr, "correctness failed n=%zu\n", n); return 1; }
    }
    if (transform_sum(NULL, NULL, 0) != 0) return 1;
    puts("{\"correctness\":\"passed\",\"cases\":11}");
    return 0;
}

static double now(void) {
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t) != 0) exit(2);
    return (double)t.tv_sec + (double)t.tv_nsec / 1e9;
}

static int benchmark(void) {
    const size_t n = 1u << 20;
    const unsigned repeats = 128;
    uint32_t *input = malloc(n * sizeof(*input));
    uint32_t *scratch = malloc(n * sizeof(*scratch));
    if (!input || !scratch) return 2;
    uint32_t state = UINT32_C(123456789);
    for (size_t i = 0; i < n; ++i) input[i] = sample(&state);
    for (unsigned i = 0; i < 8; ++i) sink ^= transform_sum(input, scratch, n);
    double start = now();
    for (unsigned i = 0; i < repeats; ++i) sink ^= transform_sum(input, scratch, n);
    double elapsed = now() - start;
    printf("{\"seconds\":%.9f,\"elements\":%zu,\"repeats\":%u,\"checksum\":%" PRIu64 "}\n", elapsed, n, repeats, sink);
    free(input);
    free(scratch);
    return elapsed > 0 ? 0 : 1;
}

int main(int argc, char **argv) {
    if (argc == 2 && strcmp(argv[1], "--test") == 0) return correctness();
    if (argc == 2 && strcmp(argv[1], "--bench") == 0) return benchmark();
    fprintf(stderr, "usage: %s --test|--bench\n", argv[0]);
    return 2;
}
