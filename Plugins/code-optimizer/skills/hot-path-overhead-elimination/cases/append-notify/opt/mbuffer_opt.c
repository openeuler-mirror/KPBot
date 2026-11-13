/* Optimised variant for the append-notify tier-B fixture.
 *
 * This file is the reference output of hot-path-overhead-elimination applied to
 * src/mbuffer.c.  Three recipes are applied, each with its evidence:
 *
 *   absent_consumer_elision   Every mutation maintains n_add/n_del counters and
 *                             calls the notify dispatch, while the dominant
 *                             state is "no observer registered": the dispatch
 *                             immediately zeroes the counters again.  The
 *                             fast path now skips both the counter update and
 *                             the dispatch, and keeps the invariant
 *                             "callbacks == NULL => both counters are zero".
 *
 *   dominant_outcome_inline   The empty-observer predicate moved into an
 *                             inline helper; the body that walks the observer
 *                             chain stays out of line as mbuf_notify_slow().
 *
 *   constant_width_copy +     The observed append lengths concentrate on
 *   hot_value_first_dispatch  {5, 4, 6, 2}; the leaf entry classifies by length
 *                             with the most frequent value first and copies
 *                             with a compile-time constant size.
 *
 *   leaf_entry_split          A leaf entry serves the dominant case (small
 *                             append, no observer, not frozen, room available)
 *                             without a stack frame; every other case tail-calls
 *                             the outlined general implementation.
 *
 * Guard completeness: the leaf entry's guards are exactly the union of the
 * conditions under which mbuf_append_general_ takes a slow branch (frozen,
 * length overflow, insufficient capacity / compaction / reallocation, observer
 * present).  If the general implementation's guards change, update the leaf in
 * lockstep.
 */
#include "mbuffer.h"

#include <stdlib.h>
#include <string.h>

/* ---------------------------------------------------------------- */
/* Outlined slow paths                                              */
/* ---------------------------------------------------------------- */

/* Models a library-internal dispatch with external linkage: kept out of line
 * so the hot path stays a true leaf. */
__attribute__((noinline)) static void mbuf_notify_slow(struct mbuf *buf)
{
	struct mbuf_cb_entry *entry;
	unsigned long added = buf->n_add_for_cb;
	unsigned long removed = buf->n_del_for_cb;

	for (entry = buf->callbacks; entry != NULL; entry = entry->next) {
		if (added && entry->added)
			entry->added(entry->ctx, (size_t)added);
		if (removed && entry->removed)
			entry->removed(entry->ctx, (size_t)removed);
	}
	buf->n_add_for_cb = 0;
	buf->n_del_for_cb = 0;
}

/* Invariant kept by every mutation in this file:
 *   buf->callbacks == NULL  ==>  n_add_for_cb == 0 && n_del_for_cb == 0
 * Verified by the case harness (test_callbacks). */
static inline int mbuf_has_observers(const struct mbuf *buf)
{
	return buf->callbacks != NULL;
}

/* Constant-width copy for the observed dominant lengths, most frequent first. */
static inline void mbuf_copy_hot(unsigned char *dst, const void *src, size_t len)
{
	switch (len) {
	case 5:
		__builtin_memcpy(dst, src, 5);
		break;
	case 4:
		__builtin_memcpy(dst, src, 4);
		break;
	case 6:
		__builtin_memcpy(dst, src, 6);
		break;
	case 2:
		__builtin_memcpy(dst, src, 2);
		break;
	default:
		memcpy(dst, src, len);
		break;
	}
}

__attribute__((noinline)) static int
mbuf_append_general_(struct mbuf *buf, const void *data, size_t len)
{
	size_t need;

	if (!buf || (!data && len))
		return MBUF_EINVAL;
	if (buf->frozen)
		return MBUF_EFROZEN;
	if (len > (size_t)-1 - buf->len)
		return MBUF_EINVAL;

	need = buf->off + buf->len + len;
	if (need > buf->cap) {
		if (buf->off && buf->len) {
			memmove(buf->data, buf->data + buf->off, buf->len);
			buf->off = 0;
			need = buf->len + len;
		}
		if (need > buf->cap) {
			size_t ncap = buf->cap ? buf->cap : 64;
			unsigned char *nd;
			while (ncap < need)
				ncap <<= 1;
			nd = (unsigned char *)realloc(buf->data, ncap);
			if (!nd)
				return MBUF_ENOMEM;
			buf->data = nd;
			buf->cap = ncap;
		}
	}

	memcpy(buf->data + buf->off + buf->len, data, len);
	buf->len += len;
	if (mbuf_has_observers(buf)) {
		buf->n_add_for_cb += len;
		mbuf_notify_slow(buf);
	}
	return MBUF_OK;
}

__attribute__((noinline)) static int
mbuf_drain_general_(struct mbuf *buf, size_t len)
{
	if (!buf)
		return MBUF_EINVAL;
	if (buf->frozen)
		return MBUF_EFROZEN;
	if (len > buf->len)
		len = buf->len;
	buf->off += len;
	buf->len -= len;
	if (buf->len == 0)
		buf->off = 0;
	if (mbuf_has_observers(buf)) {
		buf->n_del_for_cb += len;
		mbuf_notify_slow(buf);
	}
	return MBUF_OK;
}

/* ---------------------------------------------------------------- */
/* Public interface                                                 */
/* ---------------------------------------------------------------- */

int mbuf_init(struct mbuf *buf, size_t cap)
{
	if (!buf || cap == 0)
		return MBUF_EINVAL;
	buf->data = (unsigned char *)malloc(cap);
	if (!buf->data)
		return MBUF_ENOMEM;
	buf->off = 0;
	buf->len = 0;
	buf->cap = cap;
	buf->callbacks = NULL;
	buf->n_add_for_cb = 0;
	buf->n_del_for_cb = 0;
	buf->frozen = 0;
	return MBUF_OK;
}

void mbuf_free(struct mbuf *buf)
{
	struct mbuf_cb_entry *entry;
	if (!buf)
		return;
	free(buf->data);
	buf->data = NULL;
	entry = buf->callbacks;
	while (entry) {
		struct mbuf_cb_entry *next = entry->next;
		free(entry);
		entry = next;
	}
	buf->callbacks = NULL;
	buf->off = buf->len = buf->cap = 0;
}

int mbuf_append(struct mbuf *buf, const void *data, size_t len)
{
	/* Leaf entry: dominant case only.  Guards mirror every slow branch of
	 * mbuf_append_general_(), including its argument validation. */
	if (buf != NULL && data != NULL && len - 1u < 8u &&
	    buf->callbacks == NULL && !buf->frozen &&
	    len <= (size_t)-1 - buf->len &&
	    buf->off + buf->len + len <= buf->cap) {
		mbuf_copy_hot(buf->data + buf->off + buf->len, data, len);
		buf->len += len;
		return MBUF_OK;
	}
	return mbuf_append_general_(buf, data, len);
}

int mbuf_readline(struct mbuf *buf, char *out, size_t outcap, size_t *outlen)
{
	const unsigned char *p;
	size_t index, n;

	if (!buf || !out || outcap == 0)
		return MBUF_EINVAL;
	p = buf->data + buf->off;
	for (index = 0; index < buf->len; ++index) {
		if (p[index] == '\n')
			break;
	}
	if (index == buf->len)
		return 1;

	n = index;
	if (n && p[n - 1] == '\r')
		--n;
	if (n + 1 > outcap)
		return MBUF_EINVAL;
	if (n)
		memcpy(out, p, n);
	out[n] = '\0';
	if (outlen)
		*outlen = n;
	if (mbuf_drain(buf, index + 1) != MBUF_OK)
		return MBUF_EINVAL;
	return 0;
}

int mbuf_drain(struct mbuf *buf, size_t len)
{
	/* Leaf entry: drop from the front when the whole buffer empties, the
	 * dominant case for reply-style consumers. */
	if (buf != NULL && buf->callbacks == NULL && !buf->frozen) {
		if (len >= buf->len) {
			buf->off = 0;
			buf->len = 0;
			return MBUF_OK;
		}
		buf->off += len;
		buf->len -= len;
		return MBUF_OK;
	}
	return mbuf_drain_general_(buf, len);
}

void mbuf_add_cb(struct mbuf *buf, mbuf_cb added, mbuf_del_cb removed, void *ctx)
{
	struct mbuf_cb_entry *entry;

	if (!buf)
		return;
	entry = (struct mbuf_cb_entry *)malloc(sizeof(*entry));
	if (!entry)
		return;
	entry->added = added;
	entry->removed = removed;
	entry->ctx = ctx;
	entry->next = buf->callbacks;
	buf->callbacks = entry;
}

void mbuf_set_frozen(struct mbuf *buf, int frozen)
{
	if (buf)
		buf->frozen = frozen;
}

size_t mbuf_length(const struct mbuf *buf)
{
	return buf ? buf->len : 0;
}

const unsigned char *mbuf_data(const struct mbuf *buf)
{
	return buf ? buf->data + buf->off : NULL;
}

unsigned long mbuf_add_count(const struct mbuf *buf)
{
	return buf ? buf->n_add_for_cb : 0;
}

unsigned long mbuf_del_count(const struct mbuf *buf)
{
	return buf ? buf->n_del_for_cb : 0;
}
