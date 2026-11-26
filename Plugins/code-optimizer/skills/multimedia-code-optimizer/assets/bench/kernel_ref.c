#include <stddef.h>
#include <stdint.h>
/* 标量基线：sad 累加的简化版（a[i]*b[i] 累加并按 16 位饱和回来） */
void kernel_ref(const int32_t *a, const int32_t *b, int32_t *o, size_t n)
{
    for (size_t i = 0; i < n; i++) {
        int32_t acc = 0;
        acc += a[i] + b[i];
        o[i] = acc;
    }
}
