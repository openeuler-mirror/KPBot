/* Optimised variant for the metadata-floor tier-B fixture.
 *
 * This file is the reference output of allocation-path-optimization applied to
 * src/chain.c:
 *
 *   exact_size_allocation
 *     The reference-chain call site needs only the chain header plus the
 *     reference metadata.  The general helper's CHAIN_FLOOR exists for chains
 *     that carry data inline, so it is correct for cbuf_append and wrong for
 *     cbuf_add_reference.  A purpose-specific helper allocates exactly
 *     sizeof(struct chain) + sizeof(struct chain_ref) and keeps the general
 *     helper untouched for the data path.
 *
 * Invariants checked (see SKILL.md "分配路径不变量"):
 *   ownership        caller still owns `data`; the chain frees only metadata
 *   pairing          mm_malloc/mm_free stay the same source (malloc/free)
 *   lifetime         one chain per reference, freed exactly once
 *   cross_thread     no thread-local or shared state introduced
 *   failure_path     allocation failure still returns -1 before touching buf
 *   layout           struct chain fields unchanged; `buffer` still points at
 *                    the caller's data, extra metadata still lives adjacent
 *   refcount         refcnt initialised to 1, decremented once on free
 */
#include "chain.h"

#include <stdlib.h>
#include <string.h>

/* Exact-size allocation for metadata-only chains. */
static struct chain *
chain_new_reference(void)
{
	struct chain *chain;
	const size_t size = sizeof(struct chain) + sizeof(struct chain_ref);

	chain = (struct chain *)malloc(size);
	if (!chain)
		return NULL;
	memset(chain, 0, sizeof(*chain));
	/* buffer_len is overwritten by the caller with the referenced length;
	 * the inline data area is intentionally not allocated. */
	chain->refcnt = 1;
	return chain;
}

/* Unchanged general helper: data chains still need the floor for their
 * inline storage. */
struct chain *
chain_new(size_t size)
{
	struct chain *chain;
	size_t to_alloc;

	if (size > (size_t)-1 - sizeof(struct chain))
		return NULL;
	size += sizeof(struct chain);

	to_alloc = CHAIN_FLOOR;
	while (to_alloc < size)
		to_alloc <<= 1;

	chain = (struct chain *)malloc(to_alloc);
	if (!chain)
		return NULL;
	memset(chain, 0, sizeof(*chain));
	chain->buffer_len = to_alloc - sizeof(struct chain);
	chain->buffer = (unsigned char *)(chain + 1);
	chain->refcnt = 1;
	return chain;
}

static void chain_free(struct chain *chain)
{
	if (!chain)
		return;
	if (chain->flags & CHAIN_REFERENCE) {
		struct chain_ref *info =
		    (struct chain_ref *)((unsigned char *)chain + sizeof(*chain));
		if (info->cleanup)
			info->cleanup(chain->buffer, chain->off, info->extra);
	}
	free(chain);
}

int cbuf_init(struct cbuf *buf)
{
	if (!buf)
		return -1;
	buf->first = buf->last = NULL;
	buf->total_len = 0;
	return 0;
}

void cbuf_free(struct cbuf *buf)
{
	if (!buf)
		return;
	cbuf_clear(buf);
	buf->first = buf->last = NULL;
	buf->total_len = 0;
}

static void cbuf_insert(struct cbuf *buf, struct chain *chain)
{
	chain->next = NULL;
	if (buf->last)
		buf->last->next = chain;
	else
		buf->first = chain;
	buf->last = chain;
	buf->total_len += chain->off;
}

int cbuf_append(struct cbuf *buf, const void *data, size_t len)
{
	struct chain *chain;

	if (!buf || (!data && len))
		return -1;
	if (buf->last && !(buf->last->flags & CHAIN_REFERENCE) &&
	    buf->last->off + len <= buf->last->buffer_len) {
		memcpy(buf->last->buffer + buf->last->off, data, len);
		buf->last->off += len;
		buf->total_len += len;
		return 0;
	}
	chain = chain_new(len);
	if (!chain)
		return -1;
	memcpy(chain->buffer, data, len);
	chain->off = len;
	cbuf_insert(buf, chain);
	return 0;
}

int cbuf_add_reference(struct cbuf *buf, const void *data, size_t len,
    void (*cleanup)(const void *, size_t, void *), void *extra)
{
	struct chain *chain;
	struct chain_ref *info;

	if (!buf || (!data && len))
		return -1;
	chain = chain_new_reference();
	if (!chain)
		return -1;
	chain->flags |= CHAIN_REFERENCE;
	chain->buffer = (unsigned char *)data;
	chain->buffer_len = len;
	chain->off = len;
	info = (struct chain_ref *)((unsigned char *)chain + sizeof(*chain));
	info->cleanup = cleanup;
	info->extra = extra;
	cbuf_insert(buf, chain);
	return 0;
}

int cbuf_drain(struct cbuf *buf, size_t len)
{
	struct chain *chain, *next;

	if (!buf)
		return -1;
	if (len >= buf->total_len) {
		cbuf_clear(buf);
		return 0;
	}
	buf->total_len -= len;
	for (chain = buf->first; chain && len >= chain->off; chain = next) {
		next = chain->next;
		len -= chain->off;
		chain_free(chain);
	}
	buf->first = chain;
	if (chain) {
		chain->buffer += len;
		chain->off -= len;
	} else {
		buf->last = NULL;
	}
	return 0;
}

void cbuf_clear(struct cbuf *buf)
{
	struct chain *chain, *next;

	if (!buf)
		return;
	for (chain = buf->first; chain; chain = next) {
		next = chain->next;
		chain_free(chain);
	}
	buf->first = buf->last = NULL;
	buf->total_len = 0;
}

size_t cbuf_length(const struct cbuf *buf)
{
	return buf ? buf->total_len : 0;
}

const unsigned char *cbuf_data(const struct cbuf *buf)
{
	return (buf && buf->first) ? buf->first->buffer : NULL;
}
