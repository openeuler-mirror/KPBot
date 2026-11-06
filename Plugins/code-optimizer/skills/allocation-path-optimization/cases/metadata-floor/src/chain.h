/* Minimal chain library used as a tier-B fixture for
 * allocation-path-optimization.
 *
 * The shape mirrors a real event-buffer chain allocator:
 *
 *   - a general allocation helper applies a minimum size (FLOOR) because most
 *     chains carry their data inline and the floor keeps metadata overhead low;
 *   - reference chains carry *no* inline data: their `buffer` field is pointed
 *     at caller-owned memory right after creation, so the inline data area is
 *     allocated and never used;
 *   - draining a batch frees every chain, which is where the cost shows up.
 */
#ifndef CHAIN_H
#define CHAIN_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define CHAIN_FLOOR 1024

struct chain {
	struct chain *next;
	size_t buffer_len;
	size_t off;
	unsigned flags;
	int refcnt;
	unsigned char *buffer;
};

struct chain_ref {
	void (*cleanup)(const void *data, size_t len, void *extra);
	void *extra;
};

#define CHAIN_REFERENCE 0x0004

/* Buffers are singly linked chains of chain objects. */
struct cbuf {
	struct chain *first;
	struct chain *last;
	size_t total_len;
};

/* General allocation helper: applies CHAIN_FLOOR to small requests. */
struct chain *chain_new(size_t size);

int cbuf_init(struct cbuf *buf);
void cbuf_free(struct cbuf *buf);

/* Append a copy of the data into a chain with inline storage (data path). */
int cbuf_append(struct cbuf *buf, const void *data, size_t len);

/* Append a reference chain: metadata only, `data` stays caller owned. */
int cbuf_add_reference(struct cbuf *buf, const void *data, size_t len,
    void (*cleanup)(const void *, size_t, void *), void *extra);

/* Drop len bytes from the front, freeing chains that are fully consumed. */
int cbuf_drain(struct cbuf *buf, size_t len);

/* Drop everything. */
void cbuf_clear(struct cbuf *buf);

size_t cbuf_length(const struct cbuf *buf);
const unsigned char *cbuf_data(const struct cbuf *buf);

#ifdef __cplusplus
}
#endif

#endif /* CHAIN_H */
