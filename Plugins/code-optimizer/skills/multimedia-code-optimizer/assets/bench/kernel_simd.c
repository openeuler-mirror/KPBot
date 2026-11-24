#include <stddef.h>
#include <stdint.h>
#include <arm_sve.h>
void kernel_simd(const int32_t *a, const int32_t *b, int32_t *o, size_t n)
{
    for (size_t i = 0; i < n; i += svcntw()) {
        svbool_t p = svwhilelt_b32_u64(i, n);
        svint32_t va = svld1_s32(p, a + i);
        svint32_t vb = svld1_s32(p, b + i);
        svst1_s32(p, o + i, svadd_s32_x(p, va, vb));
    }
}
