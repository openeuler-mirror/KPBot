---
name: cubesandbox-template-cache-encoding
description: Reduce repeated JSON encoding on CubeMaster template-definition cache hits while returning an independently mutable request for every sandbox create. Use when repeated creates hit the same template definition and the hit path clones the full create request through JSON.
---

# Encode once per template cache fill

Given a target CubeSandbox revision and a representative create request, trace the definition cache separately from replica lookup. Record the cache key, hit/miss boundary, value type and where the request acquires its runtime fields. The unit of reuse is the normalized, stored template definition. The unit of ownership is one caller's decoded request.

## Locate the operation

In v0.7.1, a successful cache read calls `cloneCreateRequest`, which marshals and unmarshals the cached object. The fill helper also clones the definition. `GetTemplateRequest` applies stored backend information as part of loading the definition, and calls `ensureRuntimeTemplateRequest` on a private result. Misses can share an in-flight load. Identify both template and snapshot load paths and every reader and writer of this cache on the target revision. [Cache accessors](https://github.com/TencentCloud/CubeSandbox/blob/31d911e430fdf8a8879bd062b8e78066c1a8e89d/CubeMaster/pkg/templatecenter/cache.go#L135-L163) · [Loader and return paths](https://github.com/TencentCloud/CubeSandbox/blob/31d911e430fdf8a8879bd062b8e78066c1a8e89d/CubeMaster/pkg/templatecenter/store.go#L1723-L1797) · [Clone implementation](https://github.com/TencentCloud/CubeSandbox/blob/31d911e430fdf8a8879bd062b8e78066c1a8e89d/CubeMaster/pkg/templatecenter/store.go#L2143-L2153)

## Implement the cache representation

1. At the existing fill boundary, marshal the normalized request using the target codec. Publish the completed encoding under the existing key and TTL. Keep that byte slice private to the cache. The baseline also decodes during fill. If the concrete type can emit JSON it cannot read back, retain one validation decode at fill and publish only on success. Preserve nil-input handling, invalidation and the loader's existing handling of fill errors. Using the stored database JSON directly requires proving it already includes the same normalized fields.
2. At a hit, decode the cached bytes into a new zero-valued request. Return the privately owned object to the existing runtime-initialization path. Preserve the distinction between a missing or wrong-type entry and a decode error, including the target's eviction behavior. If the request type has custom JSON methods, compare the decoded candidate with the target's full clone before choosing this representation.
3. Follow the cold-load return independently. If the in-flight group returns a shared request pointer, retain the existing per-caller clone there. If it returns the new immutable encoding instead, decode separately for the leader and every waiter. Preserve the original RequestID rules for supplied and missing values. Keep runtime initialization outside the cached definition.
4. Keep nested annotations, labels, volumes and container fields independent across calls. Preserve the target codec's behavior for omitted, empty and null fields. A template update followed by a successful reload should expose the new normalized bytes to subsequent hits.

## Reproduce and check

From this skill directory run `python3 cases/cache-hit/run.py`. The standard-library contract model compares the two cache representations using identical JSON data. It exercises fill, two independent readers, scoped mutation, replacement, invalidation and an undecodable entry. The case writes `cases/cache-hit/results/result.json`; its counters cover cache-accessor encoding operations.

For target integration, use the actual Go request type and both loaders. Compare baseline and candidate returned fields, mutate a nested map and a container slice in one caller, then read the cache again. Exercise concurrent cold callers with the Go race detector, a subsequent warm hit, an invalidated definition, and a stored backend which changes the raw request. Use the target's normal RequestID checks. Count encodes and decodes separately at fill and hit, report the number of observed hits and retain one baseline/candidate result for comparison. Deliver the edited cache accessors, unchanged lifecycle contract, per-operation counts and correctness checks.
