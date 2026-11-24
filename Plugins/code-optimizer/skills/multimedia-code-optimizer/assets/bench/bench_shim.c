#include "bench_shim.h"

static long sh_call(long op, const void *arg)
{
    register long x0 asm("x0") = op;
    register const void *x1 asm("x1") = arg;
    asm volatile("hlt #0xf000" : "+r"(x0) : "r"(x1) : "memory");
    return x0;
}

void sh_puts(const char *s) { sh_call(0x04, s); }

void sh_put_u64(uint64_t v)
{
    char b[21];
    int i = 20;
    b[i] = 0;
    if (v == 0) b[--i] = '0';
    while (v) { b[--i] = (char)('0' + (v % 10u)); v /= 10u; }
    sh_puts(&b[i]);
}

void sh_put_i64(int64_t v)
{
    if (v < 0) { sh_puts("-"); sh_put_u64((uint64_t)(-(v + 1)) + 1u); }
    else sh_put_u64((uint64_t)v);
}

void sh_put_kv(const char *key, uint64_t v)
{
    sh_puts(key); sh_puts("="); sh_put_u64(v); sh_puts(" ");
}

void sh_exit(int code)
{
    /* SYS_EXIT_EXTENDED 用参数块 {reason, subcode}；QEMU 把 subcode 作为进程退出码
     * 返回，脚本据此判断夹具成功与否。直接用 SYS_EXIT 传立即数会让 QEMU 退出码
     * 恒为 1，make/脚本会误判失败。 */
    uint32_t block[2];

    block[0] = 0x20026u;          /* ADP_Stopped_ApplicationExit */
    block[1] = (uint32_t)code;
    register long x0 asm("x0") = 0x20;
    register const void *x1 asm("x1") = (const void *)block;
    asm volatile("hlt #0xf000" : "+r"(x0) : "r"(x1) : "memory");
    for (;;) asm volatile("wfi");
}

uint64_t sh_cycles(void)
{
    uint64_t v;
    asm volatile("isb; mrs %0, cntvct_el0" : "=r"(v));
    return v;
}

uint64_t sh_freq(void)
{
    uint64_t v;
    asm volatile("mrs %0, cntfrq_el0" : "=r"(v));
    return v;
}

void *memcpy(void *d, const void *s, size_t n)
{
    unsigned char *dd = (unsigned char *)d;
    const unsigned char *ss = (const unsigned char *)s;
    while (n--) *dd++ = *ss++;
    return d;
}

void *memset(void *d, int c, size_t n)
{
    unsigned char *dd = (unsigned char *)d;
    while (n--) *dd++ = (unsigned char)c;
    return d;
}

int memcmp(const void *a, const void *b, size_t n)
{
    const unsigned char *x = (const unsigned char *)a;
    const unsigned char *y = (const unsigned char *)b;
    while (n--) { if (*x != *y) return (int)*x - (int)*y; x++; y++; }
    return 0;
}

size_t strlen(const char *s) { const char *p = s; while (*p) p++; return (size_t)(p - s); }

uint64_t sh_vl_bytes(void)
{
    uint64_t v;
    asm volatile("cntb %0" : "=r"(v));
    return v;
}
