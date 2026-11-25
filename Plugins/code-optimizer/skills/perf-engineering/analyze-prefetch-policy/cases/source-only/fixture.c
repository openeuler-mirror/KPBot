/* Synthetic helpers; compiler version, flags and generated code are absent. */
static inline void local_read_hint(const float *p) {
    __builtin_prefetch(p, 0, 2);
}

static inline void write_hint(float *p) {
#if defined(__aarch64__)
    __asm__ volatile("prfm pstl1keep, [%0]" : : "r"(p));
#else
    (void)p;
#endif
}

float read_one(const float *values) {
    local_read_hint(values);
    return values[0];
}

void write_one(float *values, float value) {
    write_hint(values);
    values[0] = value;
}
