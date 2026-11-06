/* Minimal buffer library used as a tier-B fixture for
 * hot-path-overhead-elimination.
 *
 * The API mirrors the shape of a real event buffer: an append interface whose
 * length is a runtime value, a line reader that consumes from the same buffer,
 * and a notify dispatch that is called after every mutation.
 *
 * The baseline implementation in mbuffer.c is deliberately naive in the way
 * that makes call-path overhead dominant: every mutation calls an out-of-line
 * notify function, and the append path carries a full general implementation
 * with no leaf fast path.
 */
#ifndef MBUFFER_H
#define MBUFFER_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef void (*mbuf_cb)(void *ctx, size_t added);
typedef void (*mbuf_del_cb)(void *ctx, size_t removed);

struct mbuf_cb_entry {
	mbuf_cb added;
	mbuf_del_cb removed;
	void *ctx;
	struct mbuf_cb_entry *next;
};

struct mbuf {
	unsigned char *data;
	size_t off;     /* read offset */
	size_t len;     /* bytes stored after off */
	size_t cap;     /* allocation size */
	struct mbuf_cb_entry *callbacks;
	unsigned long n_add_for_cb;
	unsigned long n_del_for_cb;
	int frozen;
};

/* Return codes. */
#define MBUF_OK 0
#define MBUF_EINVAL (-1)
#define MBUF_ENOMEM (-2)
#define MBUF_EFROZEN (-3)

int mbuf_init(struct mbuf *buf, size_t cap);
void mbuf_free(struct mbuf *buf);

/* Interface 1: append a runtime-length fragment. */
int mbuf_append(struct mbuf *buf, const void *data, size_t len);

/* Interface 2: copy out the next CRLF-terminated line (terminator not
 * included).  Returns 0 and sets *outlen when a line was produced, 1 when no
 * complete line is available, negative on error. */
int mbuf_readline(struct mbuf *buf, char *out, size_t outcap, size_t *outlen);

/* Interface 3: drop the first len bytes. */
int mbuf_drain(struct mbuf *buf, size_t len);

void mbuf_add_cb(struct mbuf *buf, mbuf_cb added, mbuf_del_cb removed, void *ctx);
void mbuf_set_frozen(struct mbuf *buf, int frozen);

/* Test hooks. */
size_t mbuf_length(const struct mbuf *buf);
const unsigned char *mbuf_data(const struct mbuf *buf);
unsigned long mbuf_add_count(const struct mbuf *buf);
unsigned long mbuf_del_count(const struct mbuf *buf);

#ifdef __cplusplus
}
#endif

#endif /* MBUFFER_H */
