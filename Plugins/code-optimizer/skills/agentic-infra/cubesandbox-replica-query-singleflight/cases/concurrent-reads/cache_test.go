// Copyright (c) 2026 Tencent Inc.
// SPDX-License-Identifier: Apache-2.0
//

package templatecenter

import (
	"errors"
	"strconv"
	"strings"
	"sync"
	"time"
)

// resolveResultCacheTTL bounds reuse after a successful fill. Invalidation is
// process-local; cross-master freshness still requires shared invalidation or
// the target deployment's explicitly accepted stale-metadata budget.
const resolveResultCacheTTL = 1 * time.Second

var errResolveGenerationChanged = errors.New("template resolution generation changed")

// A nil value is an in-flight placeholder. Its lifetime belongs to the query,
// not the value TTL: a DB read slower than the TTL must still be able to finish.
type resolveCacheEntry struct {
	value      interface{}
	expireAt   time.Time
	generation uint64
}

// resolveTTLCache is a tiny concurrency-safe TTL map used for the create-path
// resolution results. It has no hard capacity bound. Generation IDs are never
// recycled when entries expire or are invalidated, so old queries cannot
// publish into a replacement slot (the ABA case).
type resolveTTLCache struct {
	mu             sync.Mutex
	ttl            time.Duration
	entries        map[string]resolveCacheEntry
	nextGeneration uint64
}

func newResolveTTLCache(ttl time.Duration) *resolveTTLCache {
	return &resolveTTLCache{ttl: ttl, entries: make(map[string]resolveCacheEntry)}
}

// Lookup atomically obtains a cached value or a generation-owned miss slot.
// All callers for that slot share metadata I/O, but never the final decision.
func (c *resolveTTLCache) Lookup(key string) (interface{}, bool, uint64) {
	now := time.Now()
	c.mu.Lock()
	defer c.mu.Unlock()
	if entry, ok := c.entries[key]; ok {
		if entry.value == nil || now.Before(entry.expireAt) {
			return entry.value, entry.value != nil, entry.generation
		}
		delete(c.entries, key)
	}
	// Opportunistic expiry cleanup; pending queries retain their placeholders.
	if len(c.entries) > 256 {
		for k, v := range c.entries {
			if v.value != nil && !now.Before(v.expireAt) {
				delete(c.entries, k)
			}
		}
	}
	c.nextGeneration++
	entry := resolveCacheEntry{generation: c.nextGeneration}
	c.entries[key] = entry
	return nil, false, entry.generation
}

// Store and invalidation use the same lock: an old reader cannot refill a
// deleted/replaced slot. The value TTL starts here, after metadata I/O finishes.
func (c *resolveTTLCache) Store(key string, generation uint64, value interface{}) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	entry, ok := c.entries[key]
	if !ok || entry.generation != generation {
		return false
	}
	entry.value, entry.expireAt = value, time.Now().Add(c.ttl)
	c.entries[key] = entry
	return true
}

// Discard releases a failed query's placeholder without deleting a newer fill.
func (c *resolveTTLCache) Discard(key string, generation uint64) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	entry, ok := c.entries[key]
	if !ok || entry.generation != generation {
		return false
	}
	delete(c.entries, key)
	return true
}

// ReadCurrent rechecks an existing slot without allocating a replacement. An
// obsolete/canceled flight must not leave a new pending slot that it never owns.
func (c *resolveTTLCache) ReadCurrent(key string, generation uint64) (interface{}, bool, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	entry, ok := c.entries[key]
	if !ok || entry.generation != generation {
		return nil, false, false
	}
	if entry.value != nil && !time.Now().Before(entry.expireAt) {
		delete(c.entries, key)
		return nil, false, false
	}
	return entry.value, entry.value != nil, true
}

func (c *resolveTTLCache) Current(key string, generation uint64) bool {
	_, hit, current := c.ReadCurrent(key, generation)
	return hit && current
}

func resolveFlightKey(kind, key string, generation uint64) string {
	return kind + "\x00" + key + "\x00" + strconv.FormatUint(generation, 10)
}

// DeletePrefix removes every entry whose key starts with prefix. Used by the
// replica / template write paths to invalidate a template without touching
// unrelated templates.
func (c *resolveTTLCache) DeletePrefix(prefix string) {
	if prefix == "" {
		return
	}
	c.mu.Lock()
	for k := range c.entries {
		if strings.HasPrefix(k, prefix) {
			delete(c.entries, k)
		}
	}
	c.mu.Unlock()
}

var (
	// replicaSchedulableResultCache stores immutable ReplicaStatus metadata
	// keyed by templateID + "\x00" + nodeID, never a schedulability bool.
	// Each request must invoke isReplicaSchedulableNow with its own context.
	replicaSchedulableResultCache = newResolveTTLCache(resolveResultCacheTTL)
	// readyReplicaResultCache stores READY metadata candidates by templateID.
	// Live schedulability and preferred-node selection run on every request,
	// outside singleflight. Metadata freshness remains bounded by the TTL
	// and the target version's invalidation protocol; health is not cached.
	readyReplicaResultCache = newResolveTTLCache(resolveResultCacheTTL)
	// resolveResultFetchGroup collapses a burst of identical cache misses into
	// a single DB read (single-flight), mirroring templateRequestFetchGroup.
	resolveResultFetchGroup = &templateFetchGroup{calls: make(map[string]*templateFetchCall)}
)

func replicaSchedulableCacheKey(templateID, nodeID string) string {
	return templateID + "\x00" + nodeID
}

func readyReplicaCacheKey(templateID string) string {
	return templateID + "\x00ready"
}

// invalidateResolveResultCache drops every cached resolution result for the
// template. Called from the replica / template write paths.
func invalidateResolveResultCache(templateID string) {
	templateID = strings.TrimSpace(templateID)
	if templateID == "" {
		return
	}
	// Keys always start with templateID; DeletePrefix("tpl") could over-match
	// templates that share a prefix, so use the exact key separator form.
	replicaSchedulableResultCache.DeletePrefix(templateID + "\x00")
	readyReplicaResultCache.DeletePrefix(templateID + "\x00")
}
