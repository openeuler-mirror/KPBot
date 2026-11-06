/* Correctness + timing harness for the append-notify tier-B fixture.
 *
 * The harness is part of the case, not of the library under test: it never
 * trusts the implementation, it re-derives the expected bytes with a small
 * reference model and compares, then times three interfaces separately using
 * process CPU time so the numbers are stable in noisy CI containers.
 *
 * Usage: bench <iters>
 * Output:  RESULT <interface> ns_per_op=<double>
 *          CHECK <name> ok|fail
 */
#define _POSIX_C_SOURCE 200809L

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "mbuffer.h"

#define BATCH 4096

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

static const char *fragment(size_t *len)
{
	static const char *frags[] = {
		"+OK\r\n", "$-1\r\n", ":1\r\n", "\r\n", "$128\r\n",
	};
	static const size_t lens[] = { 5, 5, 4, 2, 6 };
	static unsigned slot;
	size_t index = slot++ % 10;
	if (index < 3) { *len = lens[0]; return frags[0]; }
	if (index < 5) { *len = lens[1]; return frags[1]; }
	if (index < 7) { *len = lens[2]; return frags[2]; }
	if (index == 7) { *len = lens[4]; return frags[4]; }
	if (index == 8) { *len = lens[3]; return frags[3]; }
	*len = lens[0]; return frags[0];
}

/* ------------------------------------------------------------------ */
/* Correctness: replay a fixed script against the library and a model.  */
/* ------------------------------------------------------------------ */
static void test_append_model(void)
{
	struct mbuf buf;
	static unsigned char model[1 << 16];
	size_t model_len = 0;
	int ok = 1;
	int i;

	if (mbuf_init(&buf, 64) != MBUF_OK) {
		check("append.init", 0);
		return;
	}
	for (i = 0; i < 2000; ++i) {
		size_t len;
		const char *frag = fragment(&len);
		if (mbuf_append(&buf, frag, len) != MBUF_OK ||
		    model_len + len > sizeof(model)) {
			ok = 0;
			break;
		}
		memcpy(model + model_len, frag, len);
		model_len += len;
		if (model_len > 4096) {
			size_t cut = 1000;
			if (mbuf_drain(&buf, cut) != MBUF_OK) { ok = 0; break; }
			memmove(model, model + cut, model_len - cut);
			model_len -= cut;
		}
		if (mbuf_length(&buf) != model_len ||
		    memcmp(mbuf_data(&buf), model, model_len) != 0) {
			ok = 0;
			break;
		}
	}
	check("append.model", ok);
	mbuf_free(&buf);
}

static void test_readline(void)
{
	struct mbuf buf;
	char line[256];
	size_t outlen = 0;
	int ok = 1;

	if (mbuf_init(&buf, 32) != MBUF_OK) {
		check("readline.init", 0);
		return;
	}
	if (mbuf_append(&buf, "alpha\r\nbeta\n\ngamma", 18) != MBUF_OK)
		ok = 0;
	if (ok && (mbuf_readline(&buf, line, sizeof(line), &outlen) != 0 ||
		   outlen != 5 || strcmp(line, "alpha") != 0))
		ok = 0;
	if (ok && (mbuf_readline(&buf, line, sizeof(line), &outlen) != 0 ||
		   outlen != 4 || strcmp(line, "beta") != 0))
		ok = 0;
	if (ok && (mbuf_readline(&buf, line, sizeof(line), &outlen) != 0 ||
		   outlen != 0 || strcmp(line, "") != 0))
		ok = 0;
	if (ok && mbuf_readline(&buf, line, sizeof(line), &outlen) != 1)
		ok = 0;
	if (ok && (mbuf_length(&buf) != 5 ||
		   memcmp(mbuf_data(&buf), "gamma", 5) != 0))
		ok = 0;
	check("readline.model", ok);
	mbuf_free(&buf);
}

static unsigned long cb_added_total;
static void on_added(void *ctx, size_t added)
{
	(void)ctx;
	cb_added_total += added;
}

static void test_callbacks(void)
{
	struct mbuf buf;
	int ok = 1;

	cb_added_total = 0;
	if (mbuf_init(&buf, 16) != MBUF_OK) {
		check("callback.init", 0);
		return;
	}
	if (mbuf_append(&buf, "abc", 3) != MBUF_OK)
		ok = 0;
	if (ok && mbuf_add_count(&buf) != 0)
		ok = 0; /* no observer registered: counters are discarded */
	mbuf_add_cb(&buf, on_added, NULL, NULL);
	if (ok && mbuf_append(&buf, "de", 2) != MBUF_OK)
		ok = 0;
	if (ok && cb_added_total != 2)
		ok = 0;
	mbuf_set_frozen(&buf, 1);
	if (ok && mbuf_append(&buf, "x", 1) != MBUF_EFROZEN)
		ok = 0;
	mbuf_set_frozen(&buf, 0);
	check("callback.model", ok);
	mbuf_free(&buf);
}

/* The leaf entries must keep the general implementation's argument validation:
 * a NULL buffer, or NULL data with a non-zero length, is MBUF_EINVAL rather
 * than a dereference. */
static void test_argument_validation(void)
{
	struct mbuf buf;
	int ok = 1;

	if (mbuf_init(&buf, 8) != MBUF_OK) {
		check("args.init", 0);
		return;
	}
	if (mbuf_append(&buf, NULL, 4) != MBUF_EINVAL)
		ok = 0;
	if (mbuf_append(NULL, "x", 1) != MBUF_EINVAL)
		ok = 0;
	if (mbuf_append(&buf, "x", 0) != MBUF_OK)
		ok = 0;
	if (mbuf_drain(NULL, 1) != MBUF_EINVAL)
		ok = 0;
	check("args.model", ok);
	mbuf_free(&buf);
}

/* ------------------------------------------------------------------ */
/* Timing                                                              */
/* ------------------------------------------------------------------ */
static void bench_append(long iters)
{
	struct mbuf buf;
	double start, elapsed;
	long i;

	if (mbuf_init(&buf, 8192) != MBUF_OK)
		return;
	start = now_ns();
	for (i = 0; i < iters; ++i) {
		size_t len;
		const char *frag = fragment(&len);
		mbuf_append(&buf, frag, len);
		if (mbuf_length(&buf) > BATCH) {
			mbuf_drain(&buf, mbuf_length(&buf));
		}
	}
	elapsed = now_ns() - start;
	mbuf_free(&buf);
	printf("RESULT append ns_per_op=%.4f\n", elapsed / (double)iters);
}

static void bench_readline(long iters)
{
	struct mbuf buf;
	char line[64];
	double start, elapsed;
	long i, lines = 0;

	if (mbuf_init(&buf, 8192) != MBUF_OK)
		return;
	start = now_ns();
	for (i = 0; i < iters; ++i) {
		mbuf_append(&buf, "+OK\r\n", 5);
		if (mbuf_readline(&buf, line, sizeof(line), NULL) == 0)
			++lines;
	}
	elapsed = now_ns() - start;
	mbuf_free(&buf);
	printf("RESULT readline ns_per_op=%.4f lines=%ld\n",
	    elapsed / (double)iters, lines);
}

static void bench_drain(long iters)
{
	struct mbuf buf;
	double start, elapsed;
	long i;

	if (mbuf_init(&buf, 8192) != MBUF_OK)
		return;
	start = now_ns();
	for (i = 0; i < iters; ++i) {
		mbuf_append(&buf, "+OK\r\n", 5);
		mbuf_drain(&buf, 5);
	}
	elapsed = now_ns() - start;
	mbuf_free(&buf);
	printf("RESULT drain ns_per_op=%.4f\n", elapsed / (double)iters);
}

int main(int argc, char **argv)
{
	long iters = argc > 1 ? atol(argv[1]) : 200000;

	test_append_model();
	test_readline();
	test_callbacks();
	test_argument_validation();

	bench_append(iters);
	bench_readline(iters);
	bench_drain(iters);

	printf("FAILURES %d\n", failures);
	return failures == 0 ? 0 : 1;
}
