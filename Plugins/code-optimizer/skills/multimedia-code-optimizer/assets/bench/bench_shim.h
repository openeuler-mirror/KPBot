#ifndef BENCH_SHIM_H
#define BENCH_SHIM_H
#include <stddef.h>
#include <stdint.h>

__attribute__((noinline,noipa,used)) void     sh_puts(const char *s);
__attribute__((noinline,noipa,used)) void     sh_put_u64(uint64_t v);
__attribute__((noinline,noipa,used)) void     sh_put_i64(int64_t v);
__attribute__((noinline,noipa,used)) void     sh_put_kv(const char *key, uint64_t v);
__attribute__((noinline,noipa,used,noreturn)) void     sh_exit(int code);

__attribute__((noinline,noipa,used)) uint64_t sh_cycles(void);
__attribute__((noinline,noipa,used)) uint64_t sh_freq(void);
__attribute__((noinline,noipa,used)) uint64_t sh_vl_bytes(void);

void    *memcpy(void *d, const void *s, size_t n);
void    *memset(void *d, int c, size_t n);
int      memcmp(const void *a, const void *b, size_t n);
size_t   strlen(const char *s);

#endif
