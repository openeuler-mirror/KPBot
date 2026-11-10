/* Allocation accounting probe for allocation-path-optimization.
 *
 * Link the benchmark or the library under test with:
 *
 *   cc -O2 bench.c alloc_probe.c \
 *      -Wl,--wrap=malloc -Wl,--wrap=calloc -Wl,--wrap=realloc -Wl,--wrap=free \
 *      -o bench_alloc
 *
 * and run it.  On exit the probe prints one line to stderr:
 *
 *   alloc_report bytes=<requested bytes> calls=<allocations> \
 *                frees=<frees> peak_live=<peak requested bytes live>
 *
 * "bytes" is the sum of requested sizes, which is what drives heap growth,
 * brk/trim activity and page faults; it is deterministic across runs, so it is
 * a far more stable acceptance signal than wall-clock time.
 *
 * The probe is an observer only: every call is forwarded to the real allocator
 * unchanged, no object is cached or reused, and set BENCH_ALLOC_REPORT=0 to
 * silence the report.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

void *__real_malloc(size_t size);
void *__real_calloc(size_t nmemb, size_t size);
void *__real_realloc(void *ptr, size_t size);
void __real_free(void *ptr);

static size_t g_alloc_calls;
static size_t g_alloc_bytes;
static size_t g_free_calls;
static size_t g_live_bytes;
static size_t g_peak_live;

/* Open-addressed pointer -> requested size table with tombstones.
 * Sized so that a benchmark with a few million short-lived objects per batch
 * never fills it; if it does fill, accounting degrades to under-counting
 * live bytes instead of crashing. */
#define TABLE_BITS 18
#define TABLE_SIZE (1u << TABLE_BITS)
#define TABLE_MASK (TABLE_SIZE - 1u)
#define TOMBSTONE ((void *)(size_t)1)

static void *g_key[TABLE_SIZE];
static size_t g_val[TABLE_SIZE];

static size_t hash_ptr(void *ptr)
{
	size_t x = (size_t)ptr;
	x ^= x >> 33;
	x *= 0xff51afd7ed558ccdULL;
	x ^= x >> 33;
	return (size_t)(x & TABLE_MASK);
}

static void table_put(void *ptr, size_t size)
{
	size_t index = hash_ptr(ptr);
	size_t probes;
	for (probes = 0; probes < TABLE_SIZE; ++probes) {
		void *key = g_key[index];
		if (key == NULL || key == TOMBSTONE) {
			g_key[index] = ptr;
			g_val[index] = size;
			return;
		}
		index = (index + 1) & TABLE_MASK;
	}
	/* Table full: skip live tracking rather than corrupt the counters. */
}

static size_t table_take(void *ptr)
{
	size_t index = hash_ptr(ptr);
	size_t probes;
	for (probes = 0; probes < TABLE_SIZE; ++probes) {
		void *key = g_key[index];
		if (key == NULL)
			return 0;
		if (key == ptr) {
			size_t size = g_val[index];
			g_key[index] = TOMBSTONE;
			g_val[index] = 0;
			return size;
		}
		index = (index + 1) & TABLE_MASK;
	}
	return 0;
}

static void account_alloc(void *ptr, size_t size)
{
	if (!ptr)
		return;
	g_alloc_bytes += size;
	++g_alloc_calls;
	table_put(ptr, size);
	g_live_bytes += size;
	if (g_live_bytes > g_peak_live)
		g_peak_live = g_live_bytes;
}

static void account_free(void *ptr)
{
	size_t size;
	if (!ptr)
		return;
	++g_free_calls;
	size = table_take(ptr);
	if (g_live_bytes >= size)
		g_live_bytes -= size;
}

void *__wrap_malloc(size_t size)
{
	void *ptr = __real_malloc(size);
	account_alloc(ptr, size);
	return ptr;
}

void *__wrap_calloc(size_t nmemb, size_t size)
{
	void *ptr = __real_calloc(nmemb, size);
	account_alloc(ptr, nmemb * size);
	return ptr;
}

void *__wrap_realloc(void *ptr, size_t size)
{
	void *out = __real_realloc(ptr, size);
	if (!out)
		return out;
	if (ptr)
		account_free(ptr);
	account_alloc(out, size);
	return out;
}

void __wrap_free(void *ptr)
{
	account_free(ptr);
	__real_free(ptr);
}

__attribute__((destructor)) static void alloc_probe_report(void)
{
	const char *silence = getenv("BENCH_ALLOC_REPORT");
	if (silence && silence[0] == '0')
		return;
	fprintf(stderr,
	    "alloc_report bytes=%zu calls=%zu frees=%zu peak_live=%zu\n",
	    g_alloc_bytes, g_alloc_calls, g_free_calls, g_peak_live);
}
