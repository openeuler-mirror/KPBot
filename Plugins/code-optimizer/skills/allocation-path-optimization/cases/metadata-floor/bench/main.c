/* Correctness + timing harness for the metadata-floor tier-B fixture.
 *
 * Three measurements:
 *   addref  building reference chains (the allocation path)
 *   drain   freeing reference chains (where allocator/kernel cost shows up)
 *   append  data-bearing chains, which use the general helper legitimately
 *
 * Process CPU time is used so the numbers stay stable in shared CI machines.
 * Byte accounting comes from the allocation probe when it is linked in; the
 * harness itself stays independent of it.
 */
#define _POSIX_C_SOURCE 200809L

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "chain.h"

#define BATCH_OPS 4096
#define CHUNK 4096

/* Chain-count distribution modelled on reply/request consumption: most drains
 * consume a couple of bytes, a few consume large blocks. */
static const size_t g_lens[12] = {
	2, 2, 2, 2, 16, 16, 64, 64, 128, 128, 1024, 16384
};

static size_t len_for(long index)
{
	return g_lens[index % 12];
}

static double now_ns(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &ts);
	return (double)ts.tv_sec * 1e9 + (double)ts.tv_nsec;
}

static int failures;
static void check(const char *name, int ok)
{
	printf("CHECK %s %s\n", name, ok ? "ok" : "fail");
	if (!ok)
		++failures;
}

static char *chunk;

static int prepare(struct cbuf *buf, long first, long count)
{
	long i;
	for (i = 0; i < count; ++i) {
		size_t remain = len_for(first + i);
		while (remain > 0) {
			size_t n = remain > CHUNK ? CHUNK : remain;
			if (cbuf_add_reference(buf, chunk, n, NULL, NULL) != 0)
				return -1;
			remain -= n;
		}
	}
	return 0;
}

static unsigned long cleanup_calls;
static void on_cleanup(const void *data, size_t len, void *extra)
{
	(void)data;
	(void)len;
	(void)extra;
	++cleanup_calls;
}

static void test_model(void)
{
	struct cbuf buf;
	int ok = 1;

	if (cbuf_init(&buf) != 0) {
		check("drain.init", 0);
		return;
	}
	cleanup_calls = 0;
	if (prepare(&buf, 0, 12) != 0)
		ok = 0;
	/* 4x2 + 2x16 + 2x64 + 2x128 + 1024 + 16384 = 17832 bytes. */
	if (ok && cbuf_length(&buf) != 17832)
		ok = 0;
	if (ok) {
		int i;
		for (i = 0; i < 12; ++i)
			cbuf_drain(&buf, len_for(i));
	}
	if (ok && cbuf_length(&buf) != 0)
		ok = 0;
	cbuf_free(&buf);

	if (cbuf_init(&buf) != 0 || cbuf_add_reference(&buf, chunk, 100, on_cleanup, NULL) != 0 ||
	    cbuf_add_reference(&buf, chunk, 50, on_cleanup, NULL) != 0)
		ok = 0;
	if (ok && cbuf_length(&buf) != 150)
		ok = 0;
	if (ok && cbuf_drain(&buf, 100) != 0)
		ok = 0;
	if (ok && cleanup_calls != 1)
		ok = 0;
	if (ok && cbuf_length(&buf) != 50)
		ok = 0;
	cbuf_clear(&buf);
	if (ok && cleanup_calls != 2)
		ok = 0;
	cbuf_free(&buf);
	check("reference.model", ok);

	/* Data path: inline storage, tail reuse, partial drain. */
	if (cbuf_init(&buf) != 0) {
		check("append.model", 0);
		return;
	}
	ok = 1;
	if (cbuf_append(&buf, "hello", 5) != 0 ||
	    cbuf_append(&buf, " world", 6) != 0)
		ok = 0;
	if (ok && (cbuf_length(&buf) != 11 ||
		   memcmp(cbuf_data(&buf), "hello world", 11) != 0))
		ok = 0;
	if (ok && cbuf_drain(&buf, 6) != 0)
		ok = 0;
	if (ok && (cbuf_length(&buf) != 5 ||
		   memcmp(cbuf_data(&buf), "world", 5) != 0))
		ok = 0;
	cbuf_free(&buf);
	check("append.model", ok);
}

static void bench_drain(long iters)
{
	struct cbuf buf;
	double start, elapsed = 0.0;
	long done, todo, i;

	for (done = 0; done < iters; done += todo) {
		todo = iters - done;
		if (todo > BATCH_OPS)
			todo = BATCH_OPS;
		if (cbuf_init(&buf) != 0)
			return;
		if (prepare(&buf, done, todo) != 0) {
			cbuf_free(&buf);
			return;
		}
		start = now_ns();
		for (i = 0; i < todo; ++i)
			cbuf_drain(&buf, len_for(done + i));
		elapsed += now_ns() - start;
		cbuf_free(&buf);
	}
	printf("RESULT drain ns_per_op=%.4f\n", elapsed / (double)iters);
}

static void bench_addref(long iters)
{
	struct cbuf buf;
	double start, elapsed;
	long i;

	if (cbuf_init(&buf) != 0)
		return;
	start = now_ns();
	for (i = 0; i < iters; ++i)
		cbuf_add_reference(&buf, chunk, 8, NULL, NULL);
	elapsed = now_ns() - start;
	printf("RESULT addref ns_per_op=%.4f\n", elapsed / (double)iters);
	cbuf_free(&buf);
}

static void bench_append(long iters)
{
	struct cbuf buf;
	double start, elapsed;
	long i;

	if (cbuf_init(&buf) != 0)
		return;
	start = now_ns();
	for (i = 0; i < iters; ++i) {
		cbuf_append(&buf, "abcdefgh", 8);
		if (cbuf_length(&buf) > 4096)
			cbuf_drain(&buf, cbuf_length(&buf));
	}
	elapsed = now_ns() - start;
	printf("RESULT append ns_per_op=%.4f\n", elapsed / (double)iters);
	cbuf_free(&buf);
}

int main(int argc, char **argv)
{
	long iters = argc > 1 ? atol(argv[1]) : 200000;
	/* Optional third argument selects a single interface so each one can be
	 * measured in a fresh process without heap state leaking between them. */
	const char *only = argc > 2 ? argv[2] : NULL;

	chunk = (char *)malloc(CHUNK);
	if (!chunk) {
		perror("malloc");
		return 1;
	}
	memset(chunk, 'd', CHUNK);

	test_model();
	if (!only || strcmp(only, "drain") == 0)
		bench_drain(iters);
	if (!only || strcmp(only, "addref") == 0)
		bench_addref(iters);
	if (!only || strcmp(only, "append") == 0)
		bench_append(iters);

	printf("FAILURES %d\n", failures);
	free(chunk);
	return failures == 0 ? 0 : 1;
}
