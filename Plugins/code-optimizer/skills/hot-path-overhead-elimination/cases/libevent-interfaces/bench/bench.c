/* Benchmark harness for the libevent-interfaces tier-B case.
 *
 * Three modes, each mirroring a real hot path of a Redis-protocol server built
 * on libevent:
 *
 *   append    evbuffer_add with the fragment-size mix a reply path produces
 *             (mostly 5-byte status/nil replies, some 4/6/2-byte pieces and a
 *             128-byte value).
 *   readline  build a RESP line and consume it with evbuffer_readln.
 *   drain     build reference chains with the same size mix and drain them;
 *             this is the path where allocator behaviour dominates.
 *
 * Timing is ns/op over a bounded iteration count, printed as
 *   RESULT <mode> ns_per_op=<double>
 * so the runner can alternate baseline and candidate binaries.
 *
 * The harness deliberately uses a fresh evbuffer per batch and frees it at the
 * end of the batch, so a run does not grow the heap without bound.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include <event2/buffer.h>

#define BATCH_OPS 4096
#define CHUNK_SIZE 4096

static long elapsed_ns(const struct timespec *start, const struct timespec *end)
{
	return (long)((end->tv_sec - start->tv_sec) * 1000000000L +
	    (end->tv_nsec - start->tv_nsec));
}

static void now(struct timespec *ts)
{
	clock_gettime(CLOCK_PROCESS_CPUTIME_ID, ts);
}

/* Drain length for op i: 4x2, 2x16, 2x64, 2x128, 1x1024, 1x16384. */
static size_t drain_len_for_op(long index)
{
	switch (index % 12) {
	case 0: case 1: case 2: case 3: return 2;
	case 4: case 5: return 16;
	case 6: case 7: return 64;
	case 8: case 9: return 128;
	case 10: return 1024;
	default: return 16 * 1024;
	}
}

static int prepare_references(struct evbuffer *buf, const char *chunk,
    long first, long count)
{
	long i;
	for (i = 0; i < count; ++i) {
		size_t remain = drain_len_for_op(first + i);
		while (remain > 0) {
			size_t n = remain > CHUNK_SIZE ? CHUNK_SIZE : remain;
			if (evbuffer_add_reference(buf, chunk, n, NULL, NULL) < 0)
				return -1;
			remain -= n;
		}
	}
	return 0;
}

static int bench_drain(long ops)
{
	char *chunk = malloc(CHUNK_SIZE);
	long done, total_ns = 0;

	if (!chunk)
		return -1;
	memset(chunk, 'd', CHUNK_SIZE);
	for (done = 0; done < ops; done += BATCH_OPS) {
		long i, todo = ops - done < BATCH_OPS ? ops - done : BATCH_OPS;
		struct timespec start, end;
		struct evbuffer *buf = evbuffer_new();
		if (!buf || prepare_references(buf, chunk, done, todo) < 0) {
			evbuffer_free(buf);
			free(chunk);
			return -1;
		}
		now(&start);
		for (i = 0; i < todo; ++i)
			evbuffer_drain(buf, drain_len_for_op(done + i));
		now(&end);
		total_ns += elapsed_ns(&start, &end);
		evbuffer_free(buf);
	}
	printf("RESULT drain ns_per_op=%.4f\n", (double)total_ns / (double)ops);
	free(chunk);
	return 0;
}

static int bench_add(long ops)
{
	static const char *frags[] = { "+OK\r\n", "$-1\r\n", ":1\r\n", "\r\n", "$128\r\n" };
	static const size_t lens[] = { 5, 5, 4, 2, 6 };
	char *value = malloc(128);
	long done, total_ns = 0;

	if (!value)
		return -1;
	memset(value, 'x', 128);
	for (done = 0; done < ops; done += BATCH_OPS) {
		long i, todo = ops - done < BATCH_OPS ? ops - done : BATCH_OPS;
		struct timespec start, end;
		struct evbuffer *buf = evbuffer_new();
		if (!buf) {
			free(value);
			return -1;
		}
		now(&start);
		for (i = 0; i < todo; ++i) {
			long slot = (done + i) % 10;
			const char *data;
			size_t len;
			if (slot < 3) { data = frags[0]; len = lens[0]; }
			else if (slot < 5) { data = frags[1]; len = lens[1]; }
			else if (slot < 7) { data = frags[2]; len = lens[2]; }
			else if (slot == 7) { data = frags[4]; len = lens[4]; }
			else if (slot == 8) { data = value; len = 128; }
			else { data = frags[3]; len = lens[3]; }
			if (evbuffer_add(buf, data, len) < 0) {
				evbuffer_free(buf);
				free(value);
				return -1;
			}
		}
		now(&end);
		total_ns += elapsed_ns(&start, &end);
		evbuffer_free(buf);
	}
	printf("RESULT append ns_per_op=%.4f\n", (double)total_ns / (double)ops);
	free(value);
	return 0;
}

static int bench_readline(long ops)
{
	static const char line[] = "kvrocks:key:0001\r\n";
	long done, total_ns = 0;

	/* Build a whole batch of reference chains first, then consume it: this is
	 * the shape a pipelined request stream produces, and the only shape in
	 * which many chains are alive at once. */
	for (done = 0; done < ops; done += BATCH_OPS) {
		long i, todo = ops - done < BATCH_OPS ? ops - done : BATCH_OPS;
		struct timespec start, end;
		struct evbuffer *buf = evbuffer_new();
		if (!buf)
			return -1;
		for (i = 0; i < todo; ++i) {
			if (evbuffer_add_reference(buf, line, sizeof(line) - 1,
			    NULL, NULL) < 0) {
				evbuffer_free(buf);
				return -1;
			}
		}
		now(&start);
		for (i = 0; i < todo; ++i) {
			size_t n = 0;
			char *out = evbuffer_readln(buf, &n, EVBUFFER_EOL_CRLF);
			if (out)
				free(out);
		}
		now(&end);
		total_ns += elapsed_ns(&start, &end);
		evbuffer_free(buf);
	}
	printf("RESULT readline ns_per_op=%.4f\n", (double)total_ns / (double)ops);
	return 0;
}

int main(int argc, char **argv)
{
	const char *mode = argc > 1 ? argv[1] : "append";
	long ops = argc > 2 ? atol(argv[2]) : 200000;

	if (strcmp(mode, "append") == 0)
		return bench_add(ops) == 0 ? 0 : 1;
	if (strcmp(mode, "readline") == 0)
		return bench_readline(ops) == 0 ? 0 : 1;
	if (strcmp(mode, "drain") == 0)
		return bench_drain(ops) == 0 ? 0 : 1;
	fprintf(stderr, "usage: %s <append|readline|drain> [ops]\n", argv[0]);
	return 2;
}
