---
name: cubesandbox-replica-metadata-ttl
description: Reduce repeated CubeMaster READY replica list reads for successive CubeSandbox creates that use the same template. Use when tracing shows a replica metadata query on consecutive requests and the deployment can tolerate a short-lived process-local view of replica metadata. Validate SQL call counts, request-time placement, update visibility, and end-to-end creation time.
---

# CubeSandbox READY replica metadata TTL

1. Trace `ResolveTemplateReadyReplica` through `ListReplicas` for successful creates on a fixed template. Confirm that successive requests use the same CubeMaster process, reach the resolver less than a second apart, and each queries the replica table. Record baseline SQL counts by template and creation `total_time`.
2. Cache the ordered, value-only replica metadata by template ID for one second from the start of the query. Cache only successful reads containing at least one READY replica. Bound storage to 128 templates and 64 rows per entry. Leave concurrent misses independent and propagate uncached errors.
3. Run the current target's `isReplicaSchedulableNow(ctx, replica)` and preferred-node matching on every request, including hits. Preserve list ordering and the no-ready-replica outcome. In v0.7.1 the predicate checks READY status from the replica metadata. If the target has additional live placement checks, keep them on the request path.
4. After a successful local write affecting replica metadata, invalidate the cache. Fence publication with a write epoch so an overlapping query cannot repopulate the cache with pre-write data. Verify every writer before enabling it. With multiple Masters or external writers, each process may retain metadata until its own TTL expires. Leave the feature disabled if that window violates placement requirements.
5. Validate the [reference patch](references/replica-metadata-ttl.patch) and run the [reproduction and rollout procedure](references/runbook.md). Require both lower SQL call counts under controlled reuse and correct placement and invalidation. Report end-to-end performance separately.
